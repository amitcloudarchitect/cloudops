"""The Cloud Services audit entities through the connector contract (ADR 0038).

A customer credential's run only (ADR 0027): the source is bound, every request
names only its subscriptions, and nothing else is read. Each run is a snapshot of
one kind of check over the binding's subscriptions; ``since`` is not used.

fetch     Resource Graph (``audit.QUERIES``), one batch of subscriptions at a
          time, ``$skipToken`` within a batch; Defender, one subscription at a time,
          ``nextLink`` within one
inspect   the next cursor (JSON)
process   normalise each row into checks (a malformed row is rejected, counted and
          left in bronze), resolve its subscription (the binding; anything else is
          quarantined), link the checked resource where inventory holds it, upsert
          on (account, kind, native id), and record each change in
          ``cloud_check_event``

Snapshot close, as the ``resource`` entity's: on the run's last page, only when
it advances (never on replay), in that page's transaction, every open check of
the kind in the binding's accounts not seen since the run's first page was
fetched is resolved, with an event, and the watermark moves to the run's start.
A run that fails part-way resolves nothing.

History. A check first seen as an issue is ``opened``; one whose state, severity,
status, title or detail changes is ``changed`` with the fields named; a resolved
one seen again is ``reopened``. A page older than what a check already holds
changes nothing, so a replay cannot rewrite history.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.azure import audit, mapping
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.core import drift, identity, state
from cloudops_connectors.core.ingest import (
    Fetched,
    MissingRawError,
    PageContext,
    PageInfo,
    PageOutcome,
    principal,
)
from cloudops_connectors.core.raw import RawStore, day_prefixes
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

BATCH: Final = 100  # subscriptions per Resource Graph request, as the resource entity

_EXISTING: Final = text(
    "SELECT native_id, id, state, severity, fingerprint, detail, title, source_status,"
    " last_seen, resolved_at FROM cloudops.cloud_check"
    " WHERE account_id = :account AND kind = :kind AND native_id = ANY(CAST(:ids AS text[]))"
)
_RESOURCES: Final = text(
    "SELECT lower(native_id), id FROM cloudops.resource"
    " WHERE account_id = :account AND lower(native_id) = ANY(CAST(:ids AS text[]))"
)
_UPSERT: Final = text(
    "INSERT INTO cloudops.cloud_check (id, tenant_id, account_id, kind, native_id, resource_id,"
    " resource_native_id, resource_type, resource_group, region, category, rule, title, state,"
    " severity, source_status, observed_at, detail, fingerprint, first_seen, last_seen,"
    " source_system, raw_ref)"
    " VALUES (:id, :tenant_id, :account_id, :kind, :native_id, :resource_id,"
    " :resource_native_id, :resource_type, :resource_group, :region, :category, :rule, :title,"
    " :state, :severity, :source_status, :observed_at, CAST(:detail AS jsonb), :fingerprint,"
    " :seen, :seen, :source, :raw_ref)"
    " ON CONFLICT (account_id, kind, native_id) DO UPDATE SET"
    "   resource_id = EXCLUDED.resource_id, resource_native_id = EXCLUDED.resource_native_id,"
    "   resource_type = EXCLUDED.resource_type, resource_group = EXCLUDED.resource_group,"
    "   region = EXCLUDED.region, category = EXCLUDED.category, rule = EXCLUDED.rule,"
    "   title = EXCLUDED.title, state = EXCLUDED.state, severity = EXCLUDED.severity,"
    "   source_status = EXCLUDED.source_status, observed_at = EXCLUDED.observed_at,"
    "   detail = EXCLUDED.detail, fingerprint = EXCLUDED.fingerprint,"
    "   last_seen = EXCLUDED.last_seen, resolved_at = NULL, raw_ref = EXCLUDED.raw_ref"
    " WHERE cloudops.cloud_check.last_seen <= EXCLUDED.last_seen"
)
_EVENT: Final = text(
    "INSERT INTO cloudops.cloud_check_event (tenant_id, account_id, check_id, at, change,"
    " state_from, state_to, severity_from, severity_to, changed, raw_ref)"
    " VALUES (:tenant_id, :account_id, :check_id, :at, :change, :state_from, :state_to,"
    " :severity_from, :severity_to, CAST(:changed AS text[]), :raw_ref)"
)
_CLOSE: Final = text(
    "UPDATE cloudops.cloud_check SET resolved_at = :boundary"
    " WHERE account_id = ANY(CAST(:accounts AS uuid[])) AND kind = :kind"
    "   AND last_seen < :boundary AND resolved_at IS NULL"
    " RETURNING id, tenant_id, account_id, state, severity"
)


@dataclass(frozen=True)
class _Prior:
    id: UUID
    state: str
    severity: str
    fingerprint: str
    detail: dict[str, Any]
    title: str
    source_status: str | None
    last_seen: datetime
    resolved_at: datetime | None


def _when(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _changed(prior: _Prior, check: audit.Check) -> list[str]:
    fields = [name for name, old, new in (
        ("state", prior.state, check.state), ("severity", prior.severity, check.severity),
        ("title", prior.title, check.title),
        ("source_status", prior.source_status, check.source_status)) if old != new]
    old, new = prior.detail, check.detail
    if "settings" in old or "settings" in new:  # a configuration: name the setting
        os_, ns = old.get("settings") or {}, new.get("settings") or {}
        fields += [f"settings.{k}" for k in sorted(set(os_) | set(ns)) if os_.get(k) != ns.get(k)]
        old = {k: v for k, v in old.items() if k != "settings"}
        new = {k: v for k, v in new.items() if k != "settings"}
    fields += [f"detail.{k}" for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)]
    return fields


def _normaliser(entity: str, sub: str | None) -> Callable[[Any], list[audit.Check]]:
    if entity != audit.DEFENDER:
        return audit.NORMALISE[entity]

    def assessment(row: Any) -> list[audit.Check]:
        if sub is None:
            raise MissingRawError("an assessment page names no subscription")
        return audit.defender(row, sub)

    return assessment


class AzureAudit:
    """The five audit entities of one bound Azure source."""

    def __init__(self, client: AzureClient, binding: identity.Binding | None,
                 *, batch_size: int = BATCH) -> None:
        self._client = client
        self._binding = binding
        self._batch = batch_size

    def _subs(self) -> list[str]:
        if self._binding is None:
            raise ValueError("the audit entities read a customer's own credential")
        return sorted(self._binding.native_ids)

    def _batches(self) -> list[list[str]]:
        subs = self._subs()
        return [subs[i:i + self._batch] for i in range(0, len(subs), self._batch)]

    async def fetch(self, entity: str, cursor: str | None) -> Fetched:
        at = json.loads(cursor) if cursor else {}
        if entity == audit.DEFENDER:
            i = int(at.get("sub", 0))
            sub = self._subs()[i]
            body = await self._client.assessments(sub, at.get("next"))
            return Fetched(body, ".json", {"kind": entity, "subscription": sub})
        batch = int(at.get("batch", 0))
        body = await self._client.graph(self._batches()[batch], audit.QUERIES[entity],
                                        at.get("skip"))
        return Fetched(body, ".json", {"kind": entity, "batch": str(batch)})

    async def inspect(self, entity: str, body: bytes, cursor: str | None) -> PageInfo:
        at = json.loads(cursor) if cursor else {}
        if entity == audit.DEFENDER:
            i = int(at.get("sub", 0))
            link = audit.defender_next(body)
            records = len(audit.defender_page(body))
            if link is not None:
                return PageInfo(records, json.dumps({"sub": i, "next": link}))
            if i + 1 < len(self._subs()):
                return PageInfo(records, json.dumps({"sub": i + 1}))
            return PageInfo(records, None)
        if mapping.truncated(body):
            raise ValueError("Resource Graph truncated the result")
        batch, token = int(at.get("batch", 0)), mapping.skip_token(body)
        records = len(mapping.rows(body))
        if token is not None:
            return PageInfo(records, json.dumps({"batch": batch, "skip": token}))
        if batch + 1 < len(self._batches()):
            return PageInfo(records, json.dumps({"batch": batch + 1}))
        return PageInfo(records, None)

    async def process(self, db: Database, raw: RawStore, entity: str, ref: str,
                      ctx: PageContext) -> PageOutcome:
        body = await raw.get(ref)
        meta = await raw.meta(ref)
        seen = datetime.fromisoformat(meta["fetched_at"])
        sub = mapping.subscription_id(meta.get("subscription"))
        if entity == audit.DEFENDER and sub is None:
            raise MissingRawError(f"{ref} names no subscription")
        records = audit.defender_page(body) if entity == audit.DEFENDER else mapping.rows(body)
        normalise = _normaliser(entity, sub)
        counts = state.RunCounts(pages=1, records=len(records))
        checks: list[audit.Check] = []
        for row in records:
            try:
                checks.extend(normalise(row))
            except mapping.MalformedRecordError:
                counts.rejected += 1
        close = await self._close(raw, entity, ref, ctx)
        subs = {c.subscription_id for c in checks} | set(self._subs() if close else ())
        tenant_of = await self._resolve(db, subs)
        mine = [c for c in checks if tenant_of[c.subscription_id] is not None]
        unknown = Counter(c.subscription_id for c in checks
                          if tenant_of[c.subscription_id] is None)
        kind = audit.KIND[entity]
        async with db.transaction(self._scope(tenant_of)) as conn:
            accounts = await self._accounts(conn, {c.subscription_id for c in mine} | (
                set(self._subs()) if close else set()), tenant_of)
            written = 0
            for sub in sorted({c.subscription_id for c in mine}):
                batch = [c for c in mine if c.subscription_id == sub]
                written += await self._write(conn, kind, tenant_of[sub], accounts[sub], batch,
                                             seen, ref)
            await state.quarantine_aliases(conn, mapping.SOURCE, unknown, ref)
            await drift.observe(conn, mapping.SOURCE, entity,
                                drift.fields_of(r for r in records if isinstance(r, dict)),
                                mapped=audit.MAPPED[entity], expected=audit.EXPECTED[entity],
                                records=len(records), raw_ref=ref)
            if close is not None:
                await self._snapshot(conn, kind, entity, ctx, close, accounts, ref)
        counts.written = written
        counts.quarantined = sum(unknown.values())
        return PageOutcome(counts)

    async def _write(self, conn: ScopedConnection, kind: str, tenant: UUID | None,
                     account: UUID, checks: list[audit.Check], seen: datetime,
                     ref: str) -> int:
        # The last of a native id on a page wins (Resource Graph sends each once).
        by_id = {c.native_id: c for c in checks}
        ids = sorted(by_id)
        prior = {str(r[0]): _Prior(UUID(str(r[1])), str(r[2]), str(r[3]), str(r[4]),
                                   dict(r[5] or {}), str(r[6]), r[7], r[8], r[9])
                 for r in map(tuple, await conn.execute(
                     _EXISTING, {"account": account, "kind": kind, "ids": ids}))}
        targets = sorted({c.resource_native_id for c in by_id.values()
                          if c.resource_native_id is not None})
        resource = {str(r[0]): r[1] for r in map(tuple, await conn.execute(
            _RESOURCES, {"account": account, "ids": targets}))} if targets else {}
        rows: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for native in ids:
            c = by_id[native]
            before = prior.get(native)
            if before is not None and before.last_seen > seen:
                continue  # an older page, replayed: what is held is newer
            check_id = before.id if before is not None else uuid.uuid4()
            rows.append({
                "id": check_id, "tenant_id": tenant, "account_id": account, "kind": kind,
                "native_id": native, "resource_id": resource.get(c.resource_native_id or ""),
                "resource_native_id": c.resource_native_id, "resource_type": c.resource_type,
                "resource_group": c.resource_group, "region": c.region,
                "category": c.category, "rule": c.rule, "title": c.title, "state": c.state,
                "severity": c.severity, "source_status": c.source_status,
                "observed_at": _when(c.observed_at),
                "detail": json.dumps(c.detail, sort_keys=True, default=str),
                "fingerprint": c.fingerprint, "seen": seen, "source": mapping.SOURCE,
                "raw_ref": ref})
            event: dict[str, Any] = {
                "tenant_id": tenant, "account_id": account, "check_id": check_id, "at": seen,
                "state_to": c.state, "severity_to": c.severity, "raw_ref": ref,
                "state_from": None, "severity_from": None, "changed": []}
            if before is None:
                if c.state == "issue":
                    events.append({**event, "change": "opened"})
            elif before.resolved_at is not None:
                events.append({**event, "change": "reopened", "state_from": before.state,
                               "severity_from": before.severity,
                               "changed": _changed(before, c)})
            elif before.fingerprint != c.fingerprint:
                events.append({**event, "change": "changed", "state_from": before.state,
                               "severity_from": before.severity,
                               "changed": _changed(before, c)})
        if rows:
            await conn.execute(_UPSERT, rows)
        if events:
            await conn.execute(_EVENT, events)
        return len(rows)

    def _scope(self, tenant_of: Mapping[str, UUID | None]) -> TenantScope:
        return TenantScope({t for t in tenant_of.values() if t is not None}, Role.INGEST,
                           principal(mapping.SOURCE))

    async def _resolve(self, db: Database, subs: set[str]) -> dict[str, UUID | None]:
        async with db.transaction(TenantScope((), Role.INGEST,
                                              principal(mapping.SOURCE))) as conn:
            return await identity.resolve_ids(conn, mapping.SOURCE, subs, self._binding)

    async def _accounts(self, conn: ScopedConnection, subs: set[str],
                        tenant_of: Mapping[str, UUID | None]) -> dict[str, UUID]:
        wanted = {s: t for s in subs if (t := tenant_of.get(s)) is not None}
        have = {k: v[0] for k, v in (await identity.known_accounts(
            conn, mapping.SOURCE, wanted)).items()}
        missing = set(wanted) - set(have)
        if missing:
            have |= await identity.upsert_accounts(
                conn, mapping.SOURCE, wanted, (identity.AccountSeen(s, None) for s in missing))
        return have

    async def _close(self, raw: RawStore, entity: str, ref: str,
                     ctx: PageContext) -> datetime | None:
        """When this page closes the snapshot: when the run's first page was fetched."""
        if not (ctx.advance and ctx.final):
            return None
        day = datetime.strptime("/".join(ref.split("/")[2:5]), "%Y/%m/%d").date()
        start = min(day, ctx.run_started.date()) - timedelta(days=1)
        pages = [r for prefix in day_prefixes(mapping.SOURCE, entity, start, day)
                 for r in await raw.list(prefix)
                 if r.rsplit("/", 1)[-1].startswith(f"{ctx.run_id}-")]
        if not pages:
            raise MissingRawError(f"no pages for run {ctx.run_id}")
        return min([datetime.fromisoformat((await raw.meta(p))["fetched_at"]) for p in pages])

    async def _snapshot(self, conn: ScopedConnection, kind: str, entity: str,
                        ctx: PageContext, boundary: datetime, accounts: Mapping[str, UUID],
                        ref: str) -> None:
        if accounts:
            closed = list(map(tuple, await conn.execute(_CLOSE, {
                "accounts": sorted(accounts.values()), "kind": kind, "boundary": boundary})))
            if closed:
                await conn.execute(_EVENT, [{
                    "tenant_id": r[1], "account_id": r[2], "check_id": r[0], "at": boundary,
                    "change": "resolved", "state_from": r[3], "state_to": None,
                    "severity_from": r[4], "severity_to": None, "changed": [],
                    "raw_ref": ref} for r in closed])
        await state.advance_watermark(conn, mapping.SOURCE, entity, ctx.scope_key,
                                      ctx.run_started)
