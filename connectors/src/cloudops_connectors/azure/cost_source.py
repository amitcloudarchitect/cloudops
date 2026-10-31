"""Azure Cost Management FOCUS exports -> ``cloudops.cost_fact`` (entity ``cost_export``, daily).

Modelled on the AWS CUR (``aws/cur_source.py``), sharing its staged swap
(``core/assembly.py``). Each export run is an *assembly*: its manifest names
the gzip CSV parts, and a later run of the same period restates it in full.

Scopes are export roots (``<container>/<root path>``, one export each), each
with its own watermark: the ``Last-Modified`` of the last run manifest swapped
in. A run walks a queue held in the cursor (JSON):

    listing   List Blobs under the root (markers followed); run manifests
              modified after ``since`` are queued, oldest first
    manifest  register the run in ``staging.cur_assembly``, resetting any
              earlier partial load of it; its parts are queued next
    data      one gzip CSV part: rows parsed in Decimal, subscription resolved
              (exact alias of ``azure``), resource linked case-insensitively,
              aggregated to the cost_fact natural key, staged
    missing   a queued part Storage does not have yet (its 404 stored as it
              came): the scope's run stops, so no later run swaps past it

When the last part loads, the same transaction swaps the run in: for each
subscription in it, the period's ``cost_fact`` rows are deleted and the run's
inserted (``source_system`` ``azure``, ``source_batch`` the run id), and the
watermark advances. Every blob is in bronze before it is parsed. Replay re-runs
the same steps without moving a watermark; a run whose period already has a
newer swapped run is superseded and loads nothing.

Known limits are the CUR's (``aws/cur_source.py``), and: a row whose
``BillingPeriodStart`` is not its run's period is rejected (the swap replaces
one period), and a manifest naming a part outside its own run folder is
refused.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.azure import focus
from cloudops_connectors.azure.blob import BlobClient, BlobNotFoundError
from cloudops_connectors.azure.mapping import COST_EXPORT, SOURCE
from cloudops_connectors.core import assembly, drift, identity, state
from cloudops_connectors.core.ingest import (
    Fetched,
    MissingRawError,
    PageContext,
    PageInfo,
    PageOutcome,
    principal,
)
from cloudops_connectors.core.raw import RawStore
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

ENTITY: Final = COST_EXPORT
LISTING: Final = "listing"
MANIFEST: Final = "manifest"
DATA: Final = "data"
MISSING: Final = "missing"

_RESOURCES: Final = text(
    "SELECT id, account_id, lower(native_id) FROM cloudops.resource"
    " WHERE account_id = ANY(CAST(:accounts AS uuid[]))"
)


def _queue(cursor: str | None) -> list[dict[str, Any]]:
    if cursor is None:
        return [{"list": None}]
    queue = json.loads(cursor)
    if not isinstance(queue, list) or not queue or not all(isinstance(q, dict) for q in queue):
        raise ValueError("bad export cursor")
    return queue


def _cursor(queue: list[dict[str, Any]]) -> str | None:
    return json.dumps(queue, sort_keys=True) if queue else None


def run_folder(manifest_name: str) -> str:
    return manifest_name.rsplit("/", 1)[0]


async def fetch(blobs: BlobClient, scope: str, cursor: str | None) -> Fetched:
    container, root = focus.split_scope(scope)
    head = _queue(cursor)[0]
    if "list" in head:
        prefix = f"{root}/"
        return Fetched(await blobs.list(container, prefix, head["list"]), ".xml",
                       {"kind": LISTING, "prefix": prefix})
    name = str(head["key"])
    try:
        blob = await blobs.get(container, name)
    except BlobNotFoundError as exc:
        # What Storage said is the page: stored like any other, then the run stops.
        return Fetched(exc.body or b"<Error><Code>BlobNotFound</Code></Error>", ".xml",
                       {"kind": MISSING, "object_key": name})
    modified = "" if blob.last_modified is None else blob.last_modified.isoformat()
    if head["kind"] == MANIFEST:
        return Fetched(blob.body, ".json", {"kind": MANIFEST, "object_key": name,
                                            "last_modified": str(head["lm"])})
    return Fetched(blob.body, ".csv.gz", {
        "kind": DATA, "object_key": name, "last_modified": modified,
        "assembly_id": str(head["assembly"]), "manifest_ref": str(head["manifest_ref"]),
        "manifest_last_modified": str(head["manifest_lm"]),
    })


async def inspect(
    raw: RawStore, scope: str, ref: str, body: bytes, cursor: str | None
) -> PageInfo:
    _, root = focus.split_scope(scope)
    queue = _queue(cursor)
    head, rest = queue[0], queue[1:]
    if "list" in head:
        since = datetime.fromisoformat((await raw.meta(ref))["since"])
        blobs, marker = focus.listing(body)
        found = [*head.get("found", []), *([b.name, b.last_modified.isoformat()]
                                           for b in focus.manifests(blobs, root, since))]
        if marker is not None:
            return PageInfo(len(blobs), _cursor([{"list": marker, "found": found}, *rest]))
        found.sort(key=lambda f: (datetime.fromisoformat(f[1]), f[0]))
        return PageInfo(len(blobs), _cursor(
            [{"kind": MANIFEST, "key": k, "lm": lm} for k, lm in found] + rest))
    meta = await raw.meta(ref)
    kind = meta.get("kind")
    if kind == MISSING:
        return PageInfo(0, None)
    if kind == MANIFEST:
        m = _checked(focus.manifest(body), meta.get("object_key", ""))
        parts = [{"kind": DATA, "key": k, "assembly": m.run_id, "manifest_ref": ref,
                  "manifest_lm": head["lm"]} for k in m.blobs]
        return PageInfo(len(parts), _cursor(parts + rest))
    _, rows = focus.table(body)
    return PageInfo(len(rows), _cursor(rest))


def _checked(m: focus.Manifest, manifest_name: str) -> focus.Manifest:
    """Parts must sit in the manifest's own run folder: a manifest is data, and
    must not point the connector at another export's blobs."""
    folder = run_folder(manifest_name)
    if not all(b.startswith(f"{folder}/") for b in m.blobs):
        raise focus.MalformedRecordError("a part is outside its run folder")
    return m


# --- process -------------------------------------------------------------------------------


def _who() -> str:
    return principal(SOURCE)


async def process(db: Database, raw: RawStore, ref: str, ctx: PageContext) -> PageOutcome:
    meta = await raw.meta(ref)
    kind = meta.get("kind")
    if kind in (LISTING, MISSING):
        return PageOutcome(state.RunCounts(pages=1))
    if kind == MANIFEST:
        return await _manifest(db, raw, ref, meta, ctx)
    if kind == DATA:
        return await _data(db, raw, ref, meta, ctx)
    raise ValueError("bronze page is not an export listing, manifest or part")


async def _manifest(
    db: Database, raw: RawStore, ref: str, meta: dict[str, str], ctx: PageContext
) -> PageOutcome:
    m = _checked(focus.manifest(await raw.get(ref)), meta.get("object_key", ""))
    modified = assembly.last_modified(meta, "last_modified")
    async with db.transaction(TenantScope((), Role.INGEST, _who())) as conn:
        existing = await assembly.fetch(conn, m.run_id)
    if existing is not None and existing.swapped_at is not None and ctx.advance:
        return PageOutcome(state.RunCounts(pages=1))  # in cost_fact already
    tenants = existing.tenant_ids if existing is not None else frozenset()
    async with db.transaction(TenantScope(tenants, Role.INGEST, _who())) as conn:
        await assembly.register(conn, m.run_id, ctx.scope_key, m.invoice_period, ref,
                                list(m.blobs))
        if not m.blobs:  # an empty run: nothing to wait for
            registered = await assembly.fetch(conn, m.run_id, lock=True)
            if registered is not None:
                superseded = await assembly.superseded(raw, conn, registered, modified)
                await assembly.complete(conn, registered, superseded, modified, ctx,
                                        source=SOURCE, entity=ENTITY)
    return PageOutcome(state.RunCounts(pages=1))


async def accounts(
    conn: ScopedConnection, tenant_of: Mapping[str, UUID]
) -> dict[str, UUID]:
    """``cloud_account`` ids for resolved subscriptions, created on first sight.
    A subscription held by a different tenant than its alias names is an
    inconsistency for a person, never resolved here."""
    known = await identity.known_accounts(conn, SOURCE, tenant_of)
    for native, (_, tenant) in known.items():
        if tenant_of[native] != tenant:
            raise ValueError("a subscription is held by a different tenant than its alias")
    out = {native: account for native, (account, _) in known.items()}
    missing = {s: t for s, t in tenant_of.items() if s not in known}
    if missing:
        out.update(await identity.upsert_accounts(
            conn, SOURCE, missing, [identity.AccountSeen(s, None) for s in missing]))
    return out


async def _index(conn: ScopedConnection, account_ids: set[UUID]) -> dict[tuple[UUID, str], UUID]:
    """(account, lowercased ARM id) -> resource, where that is unique."""
    hits: dict[tuple[UUID, str], set[UUID]] = defaultdict(set)
    if account_ids:
        for row in await conn.execute(_RESOURCES, {"accounts": sorted(account_ids)}):
            r = tuple(row)
            hits[(UUID(str(r[1])), str(r[2]))].add(UUID(str(r[0])))
    return {k: next(iter(v)) for k, v in hits.items() if len(v) == 1}


_Key = tuple[UUID, UUID, Any, UUID | None, str | None, str, str]


async def _data(
    db: Database, raw: RawStore, ref: str, meta: dict[str, str], ctx: PageContext
) -> PageOutcome:
    key, run_id = meta.get("object_key", ""), meta.get("assembly_id", "")
    modified = assembly.last_modified(meta, "manifest_last_modified")
    header, rows = focus.table(await raw.get(ref))
    parsed, bad = focus.lines(rows)

    async with db.transaction(TenantScope((), Role.INGEST, _who())) as conn:
        a = await assembly.fetch(conn, run_id)
        if a is None:
            raise MissingRawError("the export part's run is not registered: replay its manifest")
        if key not in a.report_keys:
            raise ValueError("the export part is not in its run's manifest")
        if key in a.loaded_keys:
            return PageOutcome(state.RunCounts(pages=1))  # loaded already
        found = [x for x in parsed if x.invoice_period == a.invoice_period]
        bad += len(parsed) - len(found)  # another period's row: the swap cannot hold it
        resolved = await identity.resolve_ids(conn, SOURCE, {x.subscription for x in found})
        superseded = await assembly.superseded(raw, conn, a, modified)
    counts = state.RunCounts(pages=1, records=len(rows), rejected=bad)
    tenant_of = {s: t for s, t in resolved.items() if t is not None}
    unmatched = Counter(x.subscription for x in found if x.subscription not in tenant_of)
    counts.quarantined = sum(unmatched.values())
    tenants = set(tenant_of.values()) | a.tenant_ids

    async with db.transaction(TenantScope(tenants, Role.INGEST, _who())) as conn:
        locked = await assembly.fetch(conn, run_id, lock=True)
        if locked is None or key in locked.loaded_keys:
            return PageOutcome(state.RunCounts(pages=1))
        if not locked.tenant_ids <= tenants:
            raise ValueError("the export run changed while its part was read")
        account_of = await accounts(conn, tenant_of) if tenant_of else {}
        index = await _index(conn, set(account_of.values()))
        totals: dict[_Key, focus.Aggregate] = defaultdict(focus.Aggregate)
        unlinked: Counter[identity.Unlinked] = Counter()
        mine = [x for x in found if x.subscription in tenant_of]
        for x in mine:
            tenant, account = tenant_of[x.subscription], account_of[x.subscription]
            resource = None if x.resource is None else index.get((account, x.resource.lower()))
            if x.resource is not None and resource is None:
                name = x.resource.rstrip("/").rsplit("/", 1)[-1]
                unlinked[identity.Unlinked(tenant, x.resource, name, account)] += 1
            totals[(tenant, account, x.usage_date, resource, x.service, x.charge_class,
                    x.currency)].add(x)
        loads = [] if superseded else [_load(k, agg, run_id, key, locked.invoice_period)
                                       for k, agg in totals.items()]
        await assembly.load_rows(conn, loads)
        now = await assembly.mark_loaded(conn, locked, key, {k[0] for k in totals},
                                         {k[1] for k in totals})
        await state.quarantine_aliases(conn, SOURCE, unmatched, ref)
        await identity.quarantine(conn, SOURCE, unlinked, ref)
        await drift.observe(conn, SOURCE, ENTITY, set(header), mapped=focus.KNOWN,
                            expected=focus.EXPECTED, records=len(rows), raw_ref=ref)
        if not now.tenant_ids <= tenants:  # pragma: no cover - checked above
            raise ValueError("the export run's tenants are outside this transaction")
        if now.complete:
            await assembly.complete(conn, now, superseded, modified, ctx, source=SOURCE,
                                    entity=ENTITY)
    counts.written = 0 if superseded else len(mine)
    counts.ci_unlinked = sum(unlinked.values())
    return PageOutcome(counts)


def _load(k: _Key, agg: focus.Aggregate, run_id: str, key: str, period: str) -> dict[str, Any]:
    tenant, account, usage_date, resource, service, charge_class, currency = k
    return {
        "tenant_id": tenant, "account_id": account, "assembly_id": run_id,
        "object_key": key, "resource_id": resource, "usage_date": usage_date,
        "invoice_period": period, "service_native": service, "charge_class": charge_class,
        "unblended_cost": focus.stored(agg.unblended),
        "amortized_cost": focus.stored(agg.amortized), "currency": currency,
        "raw_extra": json.dumps(agg.raw_extra(assembly.TAGS_KEY), sort_keys=True),
    }
