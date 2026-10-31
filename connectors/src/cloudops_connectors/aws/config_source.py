"""AWS Config -> ``cloudops.resource`` (entity ``config``, every 15 minutes).

fetch     one advanced-query page (``NextToken`` is the cursor), items captured
          at or after the watermark minus five minutes
inspect   how many results, and the next token
process   normalise, resolve each item's account (exact alias of source
          ``aws``), keep ``cloud_account`` in step, upsert on
          (account, native id = ARN), quarantine unknown accounts, record
          drift; then the watermark

Results come in no particular order, so the watermark moves only on the run's
last page, to the highest capture time the run saw (as Qualys): a run that
stops part-way starts again from the same place, and the upsert makes the
repeat harmless. The upsert never moves a resource back to an older capture
(``last_seen``), so an overlap or a replay of older bronze cannot undo a newer
state.

LLD concurrency for AWS Config is "4 across accounts": an aggregator answers
for every account in one query stream, so one run reads one stream; the worker
bounds parallel runs.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.aws import mapping
from cloudops_connectors.core import drift, identity, state
from cloudops_connectors.core.ingest import PageContext, PageInfo, PageOutcome, principal
from cloudops_connectors.core.raw import RawStore
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

_COLUMNS: Final = (
    "tenant_id", "account_id", "native_id", "name", "ip_addresses", "type_native",
    "type_canonical", "region", "operating_system", "tags", "state", "first_seen",
    "last_seen", "deleted_at", "source_system", "raw_extra",
)
_CASTS: Final = {"ip_addresses": "inet[]", "tags": "jsonb", "raw_extra": "jsonb"}
_KEEP_IF_NULL: Final = frozenset({"ip_addresses", "operating_system", "state", "region"})
_UPDATED: Final = tuple(c for c in _COLUMNS
                        if c not in ("tenant_id", "account_id", "native_id", "first_seen"))
_UPSERT: Final = text(
    f"INSERT INTO cloudops.resource ({', '.join(_COLUMNS)}) VALUES ("  # noqa: S608
    + ", ".join(f"CAST(:{c} AS {_CASTS[c]})" if c in _CASTS else f":{c}" for c in _COLUMNS)
    + ") ON CONFLICT (account_id, native_id) DO UPDATE SET "
    + ", ".join(
        f"{c} = coalesce(EXCLUDED.{c}, cloudops.resource.{c})" if c in _KEEP_IF_NULL
        else f"{c} = EXCLUDED.{c}" for c in _UPDATED)
    + " WHERE cloudops.resource.last_seen <= EXCLUDED.last_seen"
)


def inspect(body: bytes) -> PageInfo:
    results, token = mapping.config_page(body)
    return PageInfo(len(results), token)


async def accounts(
    conn: ScopedConnection, tenant_of: dict[str, UUID], regions: dict[str, str | None]
) -> dict[str, UUID]:
    """``cloud_account`` ids for resolved accounts, created on first sight. An
    account already held by a different tenant than its alias names is an
    inconsistency for a person, never resolved here."""
    known = await identity.known_accounts(conn, mapping.SOURCE, tenant_of)
    for native, (_, tenant) in known.items():
        if tenant_of[native] != tenant:
            raise ValueError("an AWS account is held by a different tenant than its alias")
    out = {native: account for native, (account, _) in known.items()}
    missing = {a: t for a, t in tenant_of.items() if a not in known}
    if missing:
        # Only new accounts: an upsert would overwrite the onboarded display name.
        out.update(await identity.upsert_accounts(
            conn, mapping.SOURCE, missing,
            [identity.AccountSeen(a, None, regions.get(a)) for a in missing]))
    return out


async def process(
    db: Database, raw: RawStore, ref: str, ctx: PageContext
) -> PageOutcome:
    results, _ = mapping.config_page(await raw.get(ref))
    records, bad = mapping.config_records(results)
    counts = state.RunCounts(pages=1, records=len(results), rejected=bad)
    items: list[mapping.ConfigItem] = []
    for r in records:
        try:
            items.append(mapping.normalise_config(r))
        except mapping.MalformedRecordError:
            counts.rejected += 1
    moments = mapping.capture_times(records)
    high = max(moments) if moments else None

    who = principal(mapping.SOURCE)
    async with db.transaction(TenantScope((), Role.INGEST, who)) as conn:
        resolved = await identity.resolve_ids(conn, mapping.SOURCE, {i.account for i in items})
    tenant_of = {a: t for a, t in resolved.items() if t is not None}
    by_tenant: dict[UUID, list[mapping.ConfigItem]] = defaultdict(list)
    unmatched: Counter[str] = Counter()
    for i in items:
        tenant = tenant_of.get(i.account)
        if tenant is None:
            unmatched[i.account] += 1
        else:
            by_tenant[tenant].append(i)
    counts.quarantined = sum(unmatched.values())

    watermark = None
    if ctx.advance and ctx.final:
        marks = [m for m in (ctx.run_high_water, high) if m is not None]
        watermark = max(marks) if marks else None
    rows: list[dict[str, Any]] = []
    async with db.transaction(TenantScope(by_tenant, Role.INGEST, who)) as conn:
        if by_tenant:
            regions = {i.account: i.region for ds in by_tenant.values() for i in ds}
            account_of = await accounts(conn, tenant_of, regions)
            rows = [_row(i, tenant, account_of[i.account])
                    for tenant, ds in by_tenant.items() for i in ds]
            await conn.execute(_UPSERT, rows)
        await state.quarantine_aliases(conn, mapping.SOURCE, unmatched, ref)
        await drift.observe(conn, mapping.SOURCE, mapping.CONFIG, drift.fields_of(records),
                            mapped=mapping.CONFIG_MAPPED, expected=mapping.CONFIG_EXPECTED,
                            records=len(records), raw_ref=ref)
        if watermark is not None:
            await state.advance_watermark(conn, mapping.SOURCE, mapping.CONFIG, ctx.scope_key,
                                          watermark)
    counts.written = len(rows)
    return PageOutcome(counts, None if high is None else high.isoformat())


def _row(i: mapping.ConfigItem, tenant: UUID, account: UUID) -> dict[str, Any]:
    return {
        "tenant_id": tenant, "account_id": account, "native_id": i.arn, "name": i.name,
        "ip_addresses": [i.ip] if i.ip else None, "type_native": i.type_native,
        "type_canonical": i.type_canonical, "region": i.region,
        "operating_system": i.operating_system, "tags": json.dumps(i.tags, sort_keys=True),
        "state": i.state, "first_seen": i.created_at or i.captured_at,
        "last_seen": i.captured_at, "deleted_at": i.captured_at if i.deleted else None,
        "source_system": mapping.SOURCE,
        "raw_extra": json.dumps(i.raw_extra, sort_keys=True) if i.raw_extra else None,
    }
