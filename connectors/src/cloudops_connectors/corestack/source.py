"""CoreStack through the connector contract (``core/ingest.py``).

fetch     one findings page (the offset is the cursor)
inspect   after the page is in bronze: how many findings, and the next offset
process   normalise, resolve the customer name, link account and resource
          within that tenant only, upsert on (source_system, finding_id),
          quarantine, record drift; then the watermark

This connector writes ``cost_finding`` and nothing else. CoreStack's findings
sit *beside* the raw billing exports, never instead of them: ``cost_fact`` is
loaded from the providers' exports by their own connectors (HLD).

- **Customer**: ``tenant_name`` through ``state.resolve_aliases`` (exact, then
  normalised); otherwise quarantined with a count and nothing written for it.
- **Account**: first, the account id is resolved as an alias of its provider
  (``identity.resolve_ids``, exact only). If it belongs to a tenant other than
  the one the name resolved to, the finding is rejected and counted (kept in
  bronze): nothing carrying one customer's identifiers is written, or
  quarantined, under another's. Otherwise it is linked among the resolved
  tenant's ``cloud_account`` rows only, under that tenant's scope; an unmapped
  account id stays unlinked.
- **Resource**: the native id within the tenant (``identity.link`` on the
  external key alone; no name matching, CoreStack sends full ids). Unlinked
  resources are counted into the resource quarantine; the finding is still
  written, with NULL links.

Pages come in ``updated_at`` order, so the watermark moves with each committed
page to that page's highest update time (like ServiceNow): a run that stops
part-way restarts from the last committed page, minus the overlap.

Upserts keep the newest version: a row is replaced only by a finding updated
at or after it, so replaying old bronze cannot roll a status back.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.core import drift, identity, state
from cloudops_connectors.core.ingest import (
    Fetched,
    PageContext,
    PageInfo,
    PageOutcome,
    Source,
    principal,
)
from cloudops_connectors.core.raw import RawStore
from cloudops_connectors.corestack import mapping
from cloudops_connectors.corestack.client import CoreStackClient
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

_COLUMNS: Final = (
    "tenant_id", "account_id", "resource_id", "source_system", "finding_id", "category",
    "title", "recommendation", "status", "estimated_saving", "saving_currency",
    "customer_raw", "account_native_id", "resource_native_id", "detected_at", "updated_at",
    "raw_extra",
)
_KEY: Final = ("source_system", "finding_id")
# tenant_id is not updated: a finding re-pointed to another tenant fails its page
# (the old row is invisible under the new scope) and is dead-lettered for a person.
_UPDATED: Final = tuple(c for c in _COLUMNS if c not in (*_KEY, "tenant_id"))
_UPSERT: Final = text(
    f"INSERT INTO cloudops.cost_finding ({', '.join(_COLUMNS)}) VALUES ("  # noqa: S608
    + ", ".join(f"CAST(:{c} AS jsonb)" if c == "raw_extra" else f":{c}" for c in _COLUMNS)
    + f") ON CONFLICT ({', '.join(_KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATED)
    + ", ingested_at = now()"
    + " WHERE cloudops.cost_finding.updated_at <= EXCLUDED.updated_at"
)
_CANDIDATES: Final = text(
    "SELECT id, tenant_id, native_id, name_norm, ip_addresses FROM cloudops.resource"
    " WHERE native_id = ANY(CAST(:ids AS text[]))"
)


class CoreStackSource(Source):
    source = mapping.SOURCE
    entities = mapping.ENTITIES

    def __init__(self, client: CoreStackClient) -> None:
        self._client = client
        self.instance = client.base_url

    async def fetch(
        self, entity: str, scope: str, since: datetime, cursor: str | None
    ) -> Fetched:
        if entity != mapping.ENTITY:
            raise ValueError(f"unknown entity {entity!r}")
        return Fetched(await self._client.findings(since, cursor))

    async def inspect(
        self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes, cursor: str | None
    ) -> PageInfo:
        return PageInfo(len(mapping.records(body)), mapping.next_offset(body))

    async def process(
        self, db: Database, raw: RawStore, entity: str, ref: str, ctx: PageContext
    ) -> PageOutcome:
        recs = mapping.records(await raw.get(ref))
        counts = state.RunCounts(pages=1, records=len(recs))
        findings: list[mapping.Finding] = []
        for r in recs:
            try:
                findings.append(mapping.normalise(r))
            except mapping.MalformedRecordError:
                counts.rejected += 1  # stays in bronze
        moments = mapping.updated_times(recs)
        high = max(moments) if moments else None

        scope0 = TenantScope((), Role.INGEST, principal(self.source))
        async with db.transaction(scope0) as conn:
            resolved = await state.resolve_aliases(
                conn, self.source, (f.customer_raw for f in findings))
            owners = await _account_owners(conn, findings)
        by_tenant: dict[UUID, list[mapping.Finding]] = defaultdict(list)
        unmatched: Counter[str] = Counter()
        for f in findings:
            tenant = resolved[f.customer_raw].tenant_id
            if tenant is None:
                unmatched[f.customer_raw] += 1
                continue
            owner = owners.get((f.provider, f.account_native_id))
            if owner is not None and owner != tenant:
                # The name says one customer, the account another's: nothing of
                # it is written under either. Kept in bronze for a person.
                counts.rejected += 1
                continue
            by_tenant[tenant].append(f)
        counts.quarantined = sum(unmatched.values())

        unlinked: Counter[identity.Unlinked] = Counter()
        rows: list[dict[str, Any]] = []
        for tenant, fs in sorted(by_tenant.items(), key=lambda i: str(i[0])):
            # One tenant per lookup: an account or resource is only ever
            # searched for among the rows of the tenant the name resolved to.
            async with db.transaction(TenantScope([tenant], Role.INGEST,
                                                  principal(self.source))) as conn:
                accounts = await _accounts(conn, fs)
                candidates = identity.candidate_rows(tuple(r) for r in await conn.execute(
                    _CANDIDATES, {"ids": sorted({f.resource_native_id for f in fs
                                                 if f.resource_native_id})}))
            for f in fs:
                account = accounts.get((f.provider, f.account_native_id))
                resource = None
                if f.resource_native_id:
                    resource = identity.link(tenant, f.resource_native_id, None, None,
                                             candidates)
                    if resource is None:
                        unlinked[identity.Unlinked(tenant, f.resource_native_id,
                                                   f.resource_name, account)] += 1
                rows.append(_row(f, tenant, account, resource))

        async with db.transaction(TenantScope(by_tenant, Role.INGEST,
                                              principal(self.source))) as conn:
            if rows:
                await conn.execute(_UPSERT, rows)
            await state.quarantine_aliases(conn, self.source, unmatched, ref)
            await identity.quarantine(conn, self.source, unlinked, ref)
            await drift.observe(conn, self.source, entity, drift.fields_of(recs),
                                mapped=mapping.MAPPED, expected=mapping.EXPECTED,
                                records=len(recs), raw_ref=ref)
            if ctx.advance and high is not None:
                await state.advance_watermark(conn, self.source, entity, ctx.scope_key, high)
        counts.written = len(rows)
        counts.ci_unlinked = sum(unlinked.values())
        return PageOutcome(counts, None if high is None else high.isoformat())


async def _account_owners(
    conn: ScopedConnection, findings: list[mapping.Finding]
) -> dict[tuple[str | None, str | None], UUID]:
    """(provider, native id) -> the tenant the account id is an approved alias
    of (exact matches only, through ``cloudops.resolve_tenant_alias``; no other
    tenant's rows are read). Unmapped ids are absent."""
    wanted: dict[str, set[str]] = defaultdict(set)
    for f in findings:
        if f.provider and f.account_native_id:
            wanted[f.provider].add(f.account_native_id)
    out: dict[tuple[str | None, str | None], UUID] = {}
    for provider, ids in sorted(wanted.items()):
        for native, tenant in (await identity.resolve_ids(conn, provider, ids)).items():
            if tenant is not None:
                out[(provider, native)] = tenant
    return out


async def _accounts(
    conn: ScopedConnection, findings: list[mapping.Finding]
) -> dict[tuple[str | None, str | None], UUID]:
    """(provider, native id) -> account id, among the accounts in scope."""
    wanted: dict[str, set[str]] = defaultdict(set)
    for f in findings:
        if f.provider and f.account_native_id:
            wanted[f.provider].add(f.account_native_id)
    out: dict[tuple[str | None, str | None], UUID] = {}
    for provider, ids in sorted(wanted.items()):
        for native, (account, _tenant) in (
                await identity.known_accounts(conn, provider, ids)).items():
            out[(provider, native)] = account
    return out


def _row(
    f: mapping.Finding, tenant: UUID, account: UUID | None, resource: UUID | None
) -> dict[str, Any]:
    return {
        "tenant_id": tenant, "account_id": account, "resource_id": resource,
        "source_system": mapping.SOURCE, "finding_id": f.finding_id, "category": f.category,
        "title": f.title, "recommendation": f.recommendation, "status": f.status,
        "estimated_saving": f.saving, "saving_currency": f.currency,
        "customer_raw": f.customer_raw, "account_native_id": f.account_native_id,
        "resource_native_id": f.resource_native_id, "detected_at": f.detected_at,
        "updated_at": f.updated_at, "raw_extra": mapping.extra_json(f.raw_extra),
    }
