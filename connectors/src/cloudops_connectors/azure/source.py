"""Azure through the connector contract (``core/ingest.py``): five entities.

- ``resource``: Resource Graph, below.
- ``cost_export``: Cost Management FOCUS exports -> cost_fact (``cost_source.py``),
  one scope per export root.
- ``cost_total``, ``invoice``: provider totals and invoices -> billing_total
  (``billing_source.py``).
- ``cost_daily``: the query API's daily cost -> cost_fact, for a subscription that
  cannot have an export (``daily_source.py``). An account's period comes from one
  of ``cost_export`` and ``cost_daily``, never both: each swap replaces the period.

**A customer's own credential** (ADR 0003, ADR 0027). Built with a ``binding``,
the source serves one stored credential: its subscriptions resolve to its tenant
without the alias map, every call names only them (the client's ``allowed`` is the
second lock), anything else the listing shows is quarantined and never read, and
its ``instance`` names the credential. Its watermarks are its own, and a replay
skips any bronze page another instance fetched. Where the platform's own feed
reads a subscription's cost (ADR 0023), the credential reads its resources only:
``cost_binding`` leaves that subscription out of the cost entities (ADR 0031).

The rest of this docstring is the ``resource`` entity.

A run is a snapshot of the delegated estate, not an increment: ``since`` is
not used, and every run starts from the subscription listing.

fetch     cursor None (or a listing ``next``): ARM's subscription listing,
          stored as ``.subs.json``; then Resource Graph, one batch of
          subscriptions at a time, ``$skipToken`` within a batch
inspect   the next cursor (JSON): the listing's subscriptions, the batch
          index and the skip token
process   a listing page: resolve each subscription exactly (an alias of
          source ``azure``), quarantine unknown ones, keep ``cloud_account``
          in step. A Resource Graph page: normalise, resolve each row's
          subscription, upsert on (account, native id), count rows of unknown
          subscriptions into the alias quarantine, record drift.

Snapshot close. Only the run's last page, only when it advances (never on
replay), and in that page's transaction: every resource of the run's resolved
subscriptions not seen since the run's listing was fetched is marked deleted
as of that moment, then the watermark moves to the run's start. A run that
dead-letters or fails part-way never reaches its last page, so it deletes
nothing.

How the last page knows the run's subscriptions: they are in the run's own
listing pages in bronze, found by name (``…/{run_id}-NNNNN.subs.json``) over
the days the run can span. Nothing extra is carried or stored, and a listing
that is not there fails the page (``MissingRawError``) rather than closing on
a guess.

Clocks. ``last_seen`` is the time the page was fetched (bronze metadata), and
the close boundary is the time the run's first listing page was fetched: both
from the worker's clock, so a skew between Temporal's clock and the worker's,
or the database's, cannot make a resource seen in this run look unseen. An
upsert never moves ``last_seen`` back and never lets an older page overwrite a
newer one, so replaying old bronze changes nothing that is current; a page
newer than a resource's deletion brings it back.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.azure import cost_source, focus, mapping
from cloudops_connectors.azure.billing_source import AzureBilling
from cloudops_connectors.azure.blob import BlobClient
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.daily_source import AzureDaily
from cloudops_connectors.core import drift, identity, state
from cloudops_connectors.core.ingest import (
    Fetched,
    MissingRawError,
    PageContext,
    PageInfo,
    PageOutcome,
    Source,
    principal,
)
from cloudops_connectors.core.raw import RawStore, day_prefixes
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

BATCH: Final = 100  # subscriptions per Resource Graph request
SUBS_SUFFIX: Final = ".subs.json"

_UPSERT: Final = text(
    "INSERT INTO cloudops.resource (tenant_id, account_id, native_id, name, type_native,"
    " type_canonical, region, tags, operating_system, state, first_seen, last_seen,"
    " source_system, raw_extra)"
    " VALUES (:tenant_id, :account_id, :native_id, :name, :type_native, :type_canonical,"
    "         :region, CAST(:tags AS jsonb), :operating_system, :state, :seen, :seen,"
    "         :source, CAST(:raw_extra AS jsonb))"
    " ON CONFLICT (account_id, native_id) DO UPDATE SET"
    "   name = EXCLUDED.name, type_native = EXCLUDED.type_native,"
    "   type_canonical = EXCLUDED.type_canonical, region = EXCLUDED.region,"
    "   tags = EXCLUDED.tags, operating_system = EXCLUDED.operating_system,"
    "   state = EXCLUDED.state, last_seen = EXCLUDED.last_seen,"
    "   deleted_at = CASE WHEN cloudops.resource.deleted_at < EXCLUDED.last_seen THEN NULL"
    "                     ELSE cloudops.resource.deleted_at END,"
    "   source_system = EXCLUDED.source_system, raw_extra = EXCLUDED.raw_extra"
    " WHERE cloudops.resource.last_seen <= EXCLUDED.last_seen"
)
_CLOSE: Final = text(
    "UPDATE cloudops.resource SET deleted_at = :boundary"
    " WHERE account_id = ANY(CAST(:accounts AS uuid[])) AND last_seen < :boundary"
    "   AND deleted_at IS NULL"
)


def _cursor(subs: list[str], *, batch: int | None = None, skip: str | None = None,
            next_link: str | None = None) -> str:
    if next_link is not None:
        return json.dumps({"subs": subs, "next": next_link})
    return json.dumps({"subs": subs, "batch": batch, "skip": skip})


class AzureSource(Source):
    source = mapping.SOURCE
    entities = mapping.ENTITIES

    def __init__(
        self, client: AzureClient, *, batch_size: int = BATCH,
        exports: BlobClient | None = None, export_roots: Sequence[str] = (),
        billing_account: str | None = None, today: Callable[[], date] | None = None,
        entities: frozenset[str] = mapping.ENTITIES,
        binding: identity.Binding | None = None, credential: str | None = None,
        cost_binding: identity.Binding | None = None,
    ) -> None:
        """``exports`` reads the storage account the FOCUS exports land in;
        ``export_roots`` are ``<container>/<root path>``, one export each.
        ``billing_account`` names the billing account whose invoices are read.
        ``today`` is the run's day for the billing entities (UTC by default).
        ``entities`` narrows what this source serves (the live path, ADR 0023):
        any other is refused before a request is made. ``binding`` and
        ``credential`` (its inventory id): a customer credential's run.
        ``cost_binding``: the part of the binding whose cost this run reads, when the
        platform's own feed reads the rest (ADR 0031); the whole binding by default."""
        if not entities or not entities <= mapping.ENTITIES:
            raise ValueError("entities must be among the Azure entities")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        for root in export_roots:
            focus.split_scope(root)
        if export_roots and exports is None:
            raise ValueError("export roots need a blob client")
        if (binding is None) != (credential is None):
            raise ValueError("a binding and its credential go together")
        if binding is not None and client.allowed != binding.native_ids:
            raise ValueError("a bound source's client is allowed exactly the binding")
        self._client = client
        self._batch = batch_size
        self._exports = exports
        self._roots = list(export_roots)
        day = today or (lambda: datetime.now(UTC).date())
        if cost_binding is not None and (
                binding is None or cost_binding.tenant_id != binding.tenant_id
                or not cost_binding.native_ids <= binding.native_ids):
            raise ValueError("a cost binding is part of the source's binding")
        self._binding = binding
        cost = cost_binding or binding
        self._billing = AzureBilling(client, billing_account, day, cost)
        self._daily = AzureDaily(client, day, cost)
        self.instance = (client.base_url if credential is None
                         else f"{client.base_url}#credential={credential}")
        self.entities = entities

    def _check(self, entity: str) -> None:
        if entity not in self.entities:
            raise ValueError(f"unknown entity {entity!r}")

    async def scopes(self, db: Database, entity: str) -> list[str]:
        self._check(entity)
        return list(self._roots) if entity == mapping.COST_EXPORT else [""]

    def _batches(self, subs: list[str]) -> list[list[str]]:
        return [subs[i:i + self._batch] for i in range(0, len(subs), self._batch)]

    async def fetch(
        self, entity: str, scope: str, since: datetime, cursor: str | None
    ) -> Fetched:
        self._check(entity)
        if entity == mapping.COST_EXPORT:
            if self._exports is None:
                raise ValueError("no blob client for cost exports")
            return await cost_source.fetch(self._exports, scope, cursor)
        if entity in (mapping.COST_TOTAL, mapping.INVOICE):
            return await self._billing.fetch(entity, cursor)
        if entity == mapping.COST_DAILY:
            return await self._daily.fetch(cursor)
        at = json.loads(cursor) if cursor else {}
        if not at or "next" in at:
            body = await self._client.subscriptions(at.get("next"))
            return Fetched(body, SUBS_SUFFIX, {"kind": "subscriptions"})
        batch = int(at["batch"])
        body = await self._client.resources(self._batches(list(at["subs"]))[batch], at["skip"])
        return Fetched(body, ".json", {"kind": "resources", "batch": str(batch)})

    async def inspect(
        self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes, cursor: str | None
    ) -> PageInfo:
        self._check(entity)
        if entity == mapping.COST_EXPORT:
            return await cost_source.inspect(raw, scope, ref, body, cursor)
        if entity in (mapping.COST_TOTAL, mapping.INVOICE):
            return await self._billing.inspect(raw, entity, ref, body, cursor)
        if entity == mapping.COST_DAILY:
            return await self._daily.inspect(raw, ref, body, cursor)
        at = json.loads(cursor) if cursor else {}
        subs: list[str] = list(at.get("subs", []))
        if ref.endswith(SUBS_SUFFIX):
            listed = mapping.subscriptions(body)
            subs = identity.admit(self._binding, {*subs, *(s.subscription_id for s in listed)})
            link = mapping.next_link(body)
            if link is not None:
                return PageInfo(len(listed), _cursor(subs, next_link=link))
            return PageInfo(len(listed), _cursor(subs, batch=0) if subs else None)
        if mapping.truncated(body):
            # Not a whole snapshot, and no way to page on: fail before anything
            # could close on it.
            raise ValueError("Resource Graph truncated the result")
        batch, token = int(at["batch"]), mapping.skip_token(body)
        records = len(mapping.rows(body))
        if token is not None:
            return PageInfo(records, _cursor(subs, batch=batch, skip=token))
        if batch + 1 < len(self._batches(subs)):
            return PageInfo(records, _cursor(subs, batch=batch + 1))
        return PageInfo(records, None)

    async def process(
        self, db: Database, raw: RawStore, entity: str, ref: str, ctx: PageContext
    ) -> PageOutcome:
        self._check(entity)
        if (await raw.meta(ref)).get("instance", self.instance) != self.instance:
            # Another credential's page (a replay lists every page of the day).
            return PageOutcome(state.RunCounts())
        if entity == mapping.COST_EXPORT:
            return await cost_source.process(db, raw, ref, ctx)
        if entity in (mapping.COST_TOTAL, mapping.INVOICE):
            return await self._billing.process(db, raw, entity, ref, ctx)
        if entity == mapping.COST_DAILY:
            return await self._daily.process(db, raw, ref, ctx)
        body = await raw.get(ref)
        if ref.endswith(SUBS_SUFFIX):
            return await self._listing(db, raw, ref, ctx, body)
        seen = datetime.fromisoformat((await raw.meta(ref))["fetched_at"])
        counts = state.RunCounts(pages=1)
        records = mapping.rows(body)
        good: list[mapping.Resource] = []
        for row in records:
            try:
                good.append(mapping.normalise(row))
            except mapping.MalformedRecordError:
                counts.rejected += 1
        counts.records = len(records)
        close = await self._close(raw, ref, ctx)
        tenant_of = await self._resolve(
            db, {r.subscription_id for r in good} | (close.subs if close else set()))
        mine = [r for r in good if tenant_of[r.subscription_id] is not None]
        unknown = Counter(r.subscription_id for r in good
                          if tenant_of[r.subscription_id] is None)

        async with db.transaction(self._scope(tenant_of)) as conn:
            accounts = await self._accounts(conn, {r.subscription_id for r in mine}, tenant_of)
            if mine:
                await conn.execute(_UPSERT, [
                    _row(r, tenant_of[r.subscription_id], accounts[r.subscription_id], seen)
                    for r in mine])
            await state.quarantine_aliases(conn, self.source, unknown, ref)
            await drift.observe(conn, self.source, entity,
                                drift.fields_of(r for r in records if isinstance(r, dict)),
                                mapped=mapping.MAPPED, expected=mapping.EXPECTED,
                                records=len(records), raw_ref=ref)
            if close is not None:
                await self._snapshot(conn, entity, ctx, close, tenant_of)
        counts.written = len(mine)
        counts.quarantined = sum(unknown.values())
        return PageOutcome(counts)

    async def _listing(
        self, db: Database, raw: RawStore, ref: str, ctx: PageContext, body: bytes
    ) -> PageOutcome:
        listed = mapping.subscriptions(body)
        close = await self._close(raw, ref, ctx)
        tenant_of = await self._resolve(
            db, {s.subscription_id for s in listed} | (close.subs if close else set()))
        unknown = Counter(s.subscription_id for s in listed
                          if tenant_of[s.subscription_id] is None)
        async with db.transaction(self._scope(tenant_of)) as conn:
            known = {k: v for k, v in tenant_of.items() if v is not None}
            await identity.upsert_accounts(conn, self.source, known, (
                identity.AccountSeen(s.subscription_id, s.display_name) for s in listed))
            await state.quarantine_aliases(conn, self.source, unknown, ref)
            if close is not None:
                await self._snapshot(conn, mapping.ENTITY, ctx, close, tenant_of)
        quarantined = sum(unknown.values())
        return PageOutcome(state.RunCounts(pages=1, records=len(listed),
                                           written=len(listed) - quarantined,
                                           quarantined=quarantined))

    def _scope(self, tenant_of: Mapping[str, UUID | None]) -> TenantScope:
        """Exactly the tenants the page resolved to."""
        return TenantScope({t for t in tenant_of.values() if t is not None}, Role.INGEST,
                           principal(self.source))

    async def _resolve(self, db: Database, subs: set[str]) -> dict[str, UUID | None]:
        async with db.transaction(TenantScope((), Role.INGEST, principal(self.source))) as conn:
            return await identity.resolve_ids(conn, self.source, subs, self._binding)

    async def _close(self, raw: RawStore, ref: str, ctx: PageContext) -> _Close | None:
        """When this page closes the snapshot: the run's subscriptions and when
        its listing was fetched, both from the run's listing pages in bronze."""
        if not (ctx.advance and ctx.final):
            return None
        day = datetime.strptime("/".join(ref.split("/")[2:5]), "%Y/%m/%d").date()
        start = min(day, ctx.run_started.date()) - timedelta(days=1)
        listings = [r for prefix in day_prefixes(self.source, mapping.ENTITY, start, day)
                    for r in await raw.list(prefix)
                    if r.rsplit("/", 1)[-1].startswith(f"{ctx.run_id}-")
                    and r.endswith(SUBS_SUFFIX)]
        if not listings:
            raise MissingRawError(f"no subscription listing for run {ctx.run_id}")
        subs: set[str] = set()
        fetched: list[datetime] = []
        for listing in listings:
            subs |= {s.subscription_id for s in mapping.subscriptions(await raw.get(listing))}
            fetched.append(datetime.fromisoformat((await raw.meta(listing))["fetched_at"]))
        return _Close(subs, min(fetched))

    async def _accounts(
        self, conn: ScopedConnection, subs: set[str], tenant_of: Mapping[str, UUID | None],
    ) -> dict[str, UUID]:
        """Account ids for resolved subscriptions; created here when the
        listing has not created them (a later page replayed on its own)."""
        wanted = {s: t for s in subs if (t := tenant_of.get(s)) is not None}
        have = {k: v[0] for k, v in (await identity.known_accounts(
            conn, self.source, wanted)).items()}
        missing = set(wanted) - set(have)
        if missing:
            have |= await identity.upsert_accounts(
                conn, self.source, {s: t for s, t in tenant_of.items() if t is not None},
                (identity.AccountSeen(s, None) for s in missing))
        return have

    async def _snapshot(
        self, conn: ScopedConnection, entity: str, ctx: PageContext, close: _Close,
        tenant_of: Mapping[str, UUID | None],
    ) -> None:
        """Mark what the run did not see as deleted, then move the watermark."""
        accounts = await self._accounts(conn, close.subs, tenant_of)
        if accounts:
            await conn.execute(_CLOSE, {"accounts": sorted(accounts.values()),
                                        "boundary": close.boundary})
        await state.advance_watermark(conn, self.source, entity, ctx.scope_key,
                                      ctx.run_started)


@dataclass(frozen=True)
class _Close:
    subs: set[str]
    boundary: datetime  # when the run's subscription listing was fetched


def _row(
    r: mapping.Resource, tenant: UUID | None, account: UUID, seen: datetime
) -> dict[str, Any]:
    return {
        "tenant_id": tenant, "account_id": account, "native_id": r.native_id, "name": r.name,
        "type_native": r.type_native, "type_canonical": r.type_canonical,
        "region": r.region, "tags": json.dumps(r.tags, sort_keys=True),
        "operating_system": r.operating_system, "state": r.state, "seen": seen,
        "source": mapping.SOURCE,
        "raw_extra": json.dumps(r.raw_extra, sort_keys=True) if r.raw_extra else None,
    }
