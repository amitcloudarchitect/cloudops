"""CoreStack through the connector contract (``core/ingest.py``), ADR 0028.

scopes    the configured CoreStack tenants
fetch     the tenant whole (``mapping.snapshot``): one page per run
inspect   after the page is in bronze: how many recommendations
process   normalise, resolve the customer, link account and resource within
          that tenant only, upsert cost findings into ``cost_finding`` and
          security and compliance findings into ``security_finding``; then mark
          resolved every open finding of this CoreStack tenant the snapshot no
          longer holds, and advance the watermark

CoreStack's findings sit *beside* the raw billing exports, never instead of them:
``cost_fact`` is loaded from the providers' exports by their own connectors (HLD).

- **Customer**: ``tenant_name`` through ``state.resolve_aliases`` (exact, then
  normalised); otherwise quarantined with a count and nothing of it written.
- **Account**: the subscription in each resource's id, linked among the resolved
  tenant's ``cloud_account`` rows only. An account the tenant does not hold but
  another tenant's alias claims rejects the finding, counted, kept in bronze:
  nothing carrying one customer's identifiers is written under another's. The
  platform's own subscription, shared with one customer (ADR 0027), is held by
  that customer too, so its findings link under the customer.
- **Resource**: the native id, compared case-insensitively (CoreStack lower-cases
  it) within the tenant. Unlinked resources go to the resource quarantine; the
  finding is still written, with NULL links.
- **Resolution**: CoreStack has no change feed. Each run's snapshot is whole, so
  an open finding of this CoreStack tenant (``source_scope``) that it no longer
  holds is marked resolved, in the same transaction. A recommendation listed but
  rejected as malformed resolves nothing of its own. Replay never resolves
  anything (an old snapshot is not the current state), and an older snapshot
  never overwrites a newer one (``updated_at`` is the snapshot's time).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
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


def _upsert(table: str, columns: tuple[str, ...]) -> Any:
    key = ("source_system", "finding_id")
    # tenant_id is not updated: a finding re-pointed to another tenant fails its page
    # (the old row is invisible under the new scope) and is dead-lettered for a person.
    updated = tuple(c for c in columns if c not in (*key, "tenant_id"))
    return text(
        f"INSERT INTO cloudops.{table} ({', '.join(columns)}) VALUES ("  # noqa: S608
        + ", ".join(f"CAST(:{c} AS jsonb)" if c == "raw_extra" else f":{c}" for c in columns)
        + f") ON CONFLICT ({', '.join(key)}) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in updated)
        + ", ingested_at = now()"
        + f" WHERE cloudops.{table}.updated_at <= EXCLUDED.updated_at"
    )


_COMMON: Final = (
    "tenant_id", "account_id", "resource_id", "source_system", "source_scope", "finding_id",
    "category", "title", "status", "customer_raw", "account_native_id", "resource_native_id",
    "detected_at", "updated_at", "raw_extra",
)
_COST: Final = (*_COMMON, "recommendation")
_SECURITY: Final = (*_COMMON, "subcategory", "severity", "policy_name", "detector",
                    "resolved_at")
_UPSERT: Final = {"cost": _upsert("cost_finding", _COST),
                  "security": _upsert("security_finding", _SECURITY)}
_RESOLVE_COST: Final = text(
    "UPDATE cloudops.cost_finding SET status = 'resolved', updated_at = :at, ingested_at = now()"
    " WHERE source_system = :source AND source_scope = :scope AND status = 'open'"
    "   AND updated_at < :at AND NOT (finding_id = ANY(CAST(:seen AS text[])))"
    "   AND NOT (split_part(finding_id, ':', 1) = ANY(CAST(:held AS text[])))"
)
_RESOLVE_SECURITY: Final = text(
    "UPDATE cloudops.security_finding SET status = 'resolved', resolved_at = :at,"
    " updated_at = :at, ingested_at = now()"
    " WHERE source_system = :source AND source_scope = :scope AND status = 'open'"
    "   AND updated_at < :at AND NOT (finding_id = ANY(CAST(:seen AS text[])))"
    "   AND NOT (split_part(finding_id, ':', 1) = ANY(CAST(:held AS text[])))"
)
_RESOURCES: Final = text(
    "SELECT id, lower(native_id) FROM cloudops.resource"
    " WHERE lower(native_id) = ANY(CAST(:ids AS text[])) AND deleted_at IS NULL"
)


class CoreStackSource(Source):
    source = mapping.SOURCE
    entities = mapping.ENTITIES

    def __init__(self, client: CoreStackClient, tenants: tuple[str, ...]) -> None:
        self._client = client
        self._tenants = tuple(sorted(set(tenants)))
        self.instance = client.base_url

    async def scopes(self, db: Database, entity: str) -> list[str]:
        return list(self._tenants)

    async def fetch(
        self, entity: str, scope: str, since: datetime, cursor: str | None
    ) -> Fetched:
        if entity != mapping.ENTITY:
            raise ValueError(f"unknown entity {entity!r}")
        if scope not in self._tenants:
            raise ValueError("not a configured CoreStack tenant")
        return Fetched(await mapping.snapshot(self._client, scope, datetime.now(UTC)),
                       metadata={"corestack_tenant": scope})

    async def inspect(
        self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes, cursor: str | None
    ) -> PageInfo:
        return PageInfo(len(mapping.parse(body).recommendations), None)

    async def process(
        self, db: Database, raw: RawStore, entity: str, ref: str, ctx: PageContext
    ) -> PageOutcome:
        snap = mapping.parse(await raw.get(ref))
        counts = state.RunCounts(pages=1, records=len(snap.recommendations))
        found: list[mapping.Finding] = []
        held: list[str] = []  # listed but unreadable: neither written nor resolved
        for rec in snap.recommendations:
            try:
                found.extend(mapping.findings(rec))
            except mapping.MalformedRecordError:
                counts.rejected += 1  # stays in bronze
                if isinstance(rec.get("id"), str):
                    held.append(rec["id"])

        scope0 = TenantScope((), Role.INGEST, principal(self.source))
        async with db.transaction(scope0) as conn:
            resolved = await state.resolve_aliases(conn, self.source,
                                                   {f.customer_raw for f in found})
            owners = await _account_owners(conn, found)
        by_tenant: dict[UUID, list[mapping.Finding]] = defaultdict(list)
        unmatched: Counter[str] = Counter()
        for f in found:
            tenant = resolved[f.customer_raw].tenant_id
            if tenant is None:
                unmatched[f.customer_raw] += 1
            else:
                by_tenant[tenant].append(f)
        counts.quarantined = sum(unmatched.values())

        unlinked: Counter[identity.Unlinked] = Counter()
        rows: dict[str, list[dict[str, Any]]] = {"cost": [], "security": []}
        seen: list[str] = []
        for tenant, fs in sorted(by_tenant.items(), key=lambda i: str(i[0])):
            # One tenant per lookup: an account or resource is only ever searched
            # for among the rows of the tenant the name resolved to.
            async with db.transaction(TenantScope([tenant], Role.INGEST,
                                                  principal(self.source))) as conn:
                accounts = await _accounts(conn, fs)
                found_rows = [tuple(r) for r in await conn.execute(
                    _RESOURCES, {"ids": sorted({f.resource_native_id.lower() for f in fs
                                                if f.resource_native_id})})]
                resources = {str(r[1]): UUID(str(r[0])) for r in found_rows}
            for f in fs:
                key = (f.provider, f.account_native_id)
                account = accounts.get(key)
                owner = owners.get(key)
                if account is None and owner is not None and owner != tenant:
                    counts.rejected += 1  # another customer's subscription
                    continue
                resource = None
                if f.resource_native_id:
                    resource = resources.get(f.resource_native_id.lower())
                    if resource is None:
                        unlinked[identity.Unlinked(tenant, f.resource_native_id,
                                                   f.resource_name, account)] += 1
                rows[f.table].append(_row(f, tenant, account, resource, snap))
                seen.append(f.finding_id)

        async with db.transaction(TenantScope(by_tenant, Role.INGEST,
                                              principal(self.source))) as conn:
            for table, batch in rows.items():
                if batch:
                    await conn.execute(_UPSERT[table], batch)
            if ctx.advance and by_tenant:
                # The snapshot is whole: what it no longer holds was resolved.
                params = {"source": self.source, "scope": snap.tenant, "seen": sorted(seen),
                          "held": sorted(held), "at": snap.fetched_at}
                await conn.execute(_RESOLVE_COST, params)
                await conn.execute(_RESOLVE_SECURITY, params)
            await state.quarantine_aliases(conn, self.source, unmatched, ref)
            await identity.quarantine(conn, self.source, unlinked, ref)
            await drift.observe(conn, self.source, entity,
                                drift.fields_of(snap.recommendations), mapped=mapping.MAPPED,
                                expected=mapping.EXPECTED, records=len(snap.recommendations),
                                raw_ref=ref)
            if ctx.advance:
                await state.advance_watermark(conn, self.source, entity, ctx.scope_key,
                                              snap.fetched_at)
        counts.written = sum(len(b) for b in rows.values())
        counts.ci_unlinked = sum(unlinked.values())
        return PageOutcome(counts, snap.fetched_at.isoformat())


async def _account_owners(
    conn: ScopedConnection, findings: list[mapping.Finding]
) -> dict[tuple[str | None, str | None], UUID]:
    """(provider, native id) -> the tenant the account id is an approved alias of
    (exact matches only, through ``cloudops.resolve_tenant_alias``; no other
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
    """(provider, native id) -> account id, among the accounts of the one tenant in
    scope (so a subscription shared with the platform is this tenant's row)."""
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


def _row(f: mapping.Finding, tenant: UUID, account: UUID | None, resource: UUID | None,
         snap: mapping.Snapshot) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tenant_id": tenant, "account_id": account, "resource_id": resource,
        "source_system": mapping.SOURCE, "source_scope": snap.tenant,
        "finding_id": f.finding_id, "category": f.category, "title": f.title,
        "status": f.status, "customer_raw": f.customer_raw,
        "account_native_id": f.account_native_id, "resource_native_id": f.resource_native_id,
        "detected_at": f.detected_at, "updated_at": snap.fetched_at,
        "raw_extra": mapping.extra_json(f.raw_extra),
    }
    if f.table == "cost":
        row["recommendation"] = f.recommendation
    else:
        row.update({"subcategory": f.subcategory, "severity": f.severity,
                    "policy_name": f.policy_name, "detector": f.detector,
                    "resolved_at": snap.fetched_at if f.status == "resolved" else None})
    return row
