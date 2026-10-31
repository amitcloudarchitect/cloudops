"""Process one bronze page: normalise, resolve identity, commit (contract steps
5 to 8). The same function serves incremental runs and replay, so a replay is
the ingest path itself, fed from bronze instead of the source.

    counts = await process_page(db, raw, scope_key, entity, ref)

1. Read the page from bronze. Parsing starts here, never before it is stored.
2. Normalise each record; a record without its keys is rejected and counted.
3. Resolve each distinct company with ``cloudops.resolve_tenant_alias``, in
   a transaction with no tenant scope: the function, not the connector, sees
   the aliases.
4. In one transaction scoped to exactly the tenants this page resolved to:
   link tickets to resources (CI sys_id, then name_norm if unique in the
   tenant, else unlinked); upsert tickets on (number, opened_at); quarantine
   unmatched companies and unlinked CIs with counts; advance the watermark.
   All or nothing: a failure leaves the watermark where it was.

Each ticket written gets a span in its own trace (ADR 0022): ``ticket`` at first
sighting, whose traceparent is stored on the row, then ``ticket.updated`` or
``ticket.closed`` on later pages, so a ticket from first sighting through triage
to closure is one trace. Each span links to the page's batch span.

A ticket whose company moves to a different tenant cannot be upserted under
the new tenant's scope (the existing row belongs to the old one and RLS hides
it), so the page fails loudly and is dead-lettered for a human, rather than
the platform guessing which tenant is right.

``reprocess_page`` is the same path narrowed to one company name, for an
approved mapping's held records (ADR 0015): only that name's records, only if
the name still resolves exactly to the tenant the request was approved for
(checked on every page, at run time), nothing quarantined for other names and
no watermark moved. It returns the keys of the records it newly inserted, so
the request reports exactly how many records moved.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.core import state
from cloudops_connectors.core.raw import RawStore
from cloudops_connectors.servicenow.mapping import (
    SOURCE,
    MalformedRecordError,
    Ticket,
    normalise,
    updated_at,
)
from cloudops_observability import ticket_span, traceparent
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

PRINCIPAL: Final = "connector:servicenow"

# Ticket fields that are work_item columns, in insert order.
_COLUMNS: Final = (
    "kind", "number", "company_raw", "opened_at", "closed_at", "priority", "state",
    "assigned_to", "assignment_workgroup", "short_description", "configuration_item",
    "change_request", "change_type", "resolve_sla", "response_sla", "made_sla",
    "sla_breached", "duration_seconds", "resolve_time_seconds", "pending_total_seconds",
    "planned_hours", "actual_hours", "closure_code", "close_notes",
)
_INSERT_COLUMNS: Final = ("tenant_id", *_COLUMNS, "resource_id", "raw_extra", "trace_parent")
_UPSERT: Final = text(
    f"INSERT INTO cloudops.work_item ({', '.join(_INSERT_COLUMNS)}) VALUES ("  # noqa: S608
    + ", ".join(f"CAST(:{c} AS jsonb)" if c == "raw_extra" else f":{c}" for c in _INSERT_COLUMNS)
    + ") ON CONFLICT (number, opened_at) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in (*_COLUMNS, "resource_id", "raw_extra"))
    + ", ingested_at = now()"
    # A ticket's trace is fixed at first sighting; a later page never replaces it.
    + ", trace_parent = coalesce(cloudops.work_item.trace_parent, EXCLUDED.trace_parent)"
)
# The trace each ticket already has, and whether it was already closed.
_TRACES: Final = text(
    "SELECT number, opened_at, trace_parent, closed_at IS NOT NULL FROM cloudops.work_item"
    " WHERE (number, opened_at) IN (SELECT * FROM unnest(CAST(:numbers AS text[]),"
    " CAST(:opened AS timestamptz[])))"
)
# Re-process: which of these tickets are there already, read in the transaction
# that upserts them, so "moved" counts only records that were not.
_EXISTING: Final = text(
    "SELECT number, opened_at FROM cloudops.work_item"
    " WHERE (number, opened_at) IN (SELECT * FROM unnest(CAST(:numbers AS text[]),"
    " CAST(:opened AS timestamptz[])))"
)
_CANDIDATES: Final = text(
    "SELECT id, tenant_id, servicenow_ci_sys_id, name_norm FROM cloudops.resource"
    " WHERE servicenow_ci_sys_id = ANY(CAST(:sys_ids AS text[]))"
    "    OR name_norm = ANY(CAST(:norms AS text[]))"
)


def _name_norm(ci: str) -> str:
    """As cloudops.resource.name_norm computes it."""
    return ci.split(".", 1)[0].lower()


def _link(
    ticket: Ticket, tenant: UUID, candidates: list[tuple[UUID, UUID, str | None, str]]
) -> UUID | None:
    """LLD, Identity resolution (resource): external key, then name_norm; a
    name that matches two resources in the tenant links to neither."""
    mine = [c for c in candidates if c[1] == tenant]
    if ticket.ci_sys_id:
        by_key = {c[0] for c in mine if c[2] == ticket.ci_sys_id}
        if len(by_key) == 1:
            return next(iter(by_key))
    if ticket.configuration_item:
        by_name = {c[0] for c in mine if c[3] == _name_norm(ticket.configuration_item)}
        if len(by_name) == 1:
            return next(iter(by_name))
    return None


def _row(ticket: Ticket, tenant: UUID, resource: UUID | None) -> dict[str, Any]:
    values = {f.name: getattr(ticket, f.name) for f in fields(ticket) if f.name in _COLUMNS}
    return {
        **values,
        "tenant_id": tenant,
        "resource_id": resource,
        "raw_extra": json.dumps(ticket.raw_extra, sort_keys=True) if ticket.raw_extra else None,
    }


async def process_page(
    db: Database,
    raw: RawStore,
    scope_key: str,
    entity: str,
    ref: str,
    *,
    advance_watermark: bool = True,
) -> state.RunCounts:
    body = await raw.get(ref)
    records: list[dict[str, Any]] = json.loads(body)["result"]
    counts = state.RunCounts(pages=1, records=len(records))

    tickets: list[Ticket] = []
    for record in records:
        try:
            tickets.append(normalise(entity, record))
        except MalformedRecordError:
            counts.rejected += 1
    moments = [m for m in (updated_at(r) for r in records) if m is not None]
    high_water: datetime | None = max(moments) if moments else None

    async with db.transaction(TenantScope((), Role.INGEST, PRINCIPAL)) as conn:
        resolved = await state.resolve_aliases(conn, SOURCE, (t.company_raw for t in tickets))

    by_tenant: dict[UUID, list[Ticket]] = defaultdict(list)
    unmatched: Counter[str] = Counter()
    for t in tickets:
        r = resolved[t.company_raw]
        if r.tenant_id is None:
            unmatched[t.company_raw] += 1
        else:
            by_tenant[r.tenant_id].append(t)
    counts.quarantined = sum(unmatched.values())

    rows, unlinked = await _commit(db, by_tenant, unmatched, ref, entity, scope_key,
                                   high_water if advance_watermark else None)
    counts.written = len(rows)
    counts.ci_unlinked = sum(unlinked.values())
    return counts


async def _commit(
    db: Database,
    by_tenant: Mapping[UUID, list[Ticket]],
    unmatched: Counter[str],
    ref: str,
    entity: str,
    scope_key: str,
    watermark: datetime | None,
    *,
    returning: list[tuple[str, datetime, bool]] | None = None,
) -> tuple[list[dict[str, Any]], Counter[tuple[UUID, str, str]]]:
    """Step 4: one transaction scoped to exactly these tenants."""
    rows: list[dict[str, Any]] = []
    unlinked: Counter[tuple[UUID, str, str]] = Counter()
    async with db.transaction(TenantScope(by_tenant, Role.INGEST, PRINCIPAL)) as conn:
        if by_tenant:
            mine = [t for ts in by_tenant.values() for t in ts]
            candidates: list[tuple[UUID, UUID, str | None, str]] = [
                (r[0], r[1], r[2], r[3])
                for r in (tuple(x) for x in await conn.execute(
                    _CANDIDATES,
                    {
                        "sys_ids": sorted({t.ci_sys_id for t in mine if t.ci_sys_id}),
                        "norms": sorted({
                            _name_norm(t.configuration_item)
                            for t in mine if t.configuration_item
                        }),
                    },
                ))
            ]
            for tenant, ts in by_tenant.items():
                for t in ts:
                    resource = _link(t, tenant, candidates)
                    if resource is None and (t.ci_sys_id or t.configuration_item):
                        key = f"ci:{t.ci_sys_id}" if t.ci_sys_id else f"ci:{t.configuration_item}"
                        unlinked[(tenant, key, t.configuration_item or "")] += 1
                    rows.append(_row(t, tenant, resource))
            if returning is not None:
                existing = {(str(r.number), r.opened_at) for r in await conn.execute(_EXISTING, {
                    "numbers": [r["number"] for r in rows],
                    "opened": [r["opened_at"] for r in rows]})}
                returning.extend((r["number"], r["opened_at"],
                                  (r["number"], r["opened_at"]) not in existing) for r in rows)
            await _trace_tickets(conn, rows)
            await conn.execute(_UPSERT, rows)
        await state.quarantine_aliases(conn, SOURCE, unmatched, ref)
        await state.quarantine_resources(conn, SOURCE, unlinked, ref)
        if watermark is not None:
            await state.advance_watermark(conn, SOURCE, entity, scope_key, watermark)
    return rows, unlinked


async def _trace_tickets(conn: ScopedConnection, rows: list[dict[str, Any]]) -> None:
    """One span per ticket, in the ticket's trace; sets each row's trace_parent."""
    result = await conn.execute(_TRACES, {
        "numbers": [r["number"] for r in rows], "opened": [r["opened_at"] for r in rows]})
    known: dict[tuple[str, datetime], tuple[str | None, bool]] = {
        (str(r[0]), r[1]): (r[2], bool(r[3])) for r in (tuple(x) for x in result)}
    for row in rows:
        parent, was_closed = known.get((row["number"], row["opened_at"]), (None, False))
        stage = ("ingested" if parent is None
                 else "closed" if row["closed_at"] is not None and not was_closed
                 else "updated")
        with ticket_span(stage, parent, {"cloudops.ticket.kind": row["kind"],
                                         "cloudops.ticket.state": row["state"] or "none",
                                         "cloudops.ticket.linked": row["resource_id"] is not None,
                                         "cloudops.source": SOURCE}) as span:
            row["trace_parent"] = parent or traceparent(span)


class ReprocessRefusedError(Exception):
    """The name no longer resolves exactly to the approved tenant. Never retried:
    the request ends refused, and nothing on this page was written."""


@dataclass
class ReprocessCounts:
    pages: int = 0
    matched: int = 0  # records on the page under this name
    inserted: list[str] = field(default_factory=list)  # keys of rows that were not there


async def reprocess_page(
    db: Database, raw: RawStore, entity: str, ref: str, alias: str, tenant: UUID,
) -> ReprocessCounts:
    body = await raw.get(ref)
    records: list[dict[str, Any]] = json.loads(body)["result"]
    tickets: list[Ticket] = []
    for record in records:
        try:
            ticket = normalise(entity, record)
        except MalformedRecordError:
            continue  # counted by the run that first read it; it has no company to match
        if ticket.company_raw == alias:
            tickets.append(ticket)
    counts = ReprocessCounts(pages=1, matched=len(tickets))
    if not tickets:
        return counts
    async with db.transaction(TenantScope((), Role.INGEST, PRINCIPAL)) as conn:
        resolved = (await state.resolve_aliases(conn, SOURCE, [alias]))[alias]
    if resolved.outcome != "exact" or resolved.tenant_id != tenant:
        raise ReprocessRefusedError("mapping_changed")
    written: list[tuple[str, datetime, bool]] = []
    await _commit(db, {tenant: tickets}, Counter(), ref, entity, "", None, returning=written)
    counts.inserted = sorted({f"{n}|{at.isoformat()}" for n, at, new in written if new})
    return counts
