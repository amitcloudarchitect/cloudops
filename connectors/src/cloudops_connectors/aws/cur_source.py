"""The AWS Cost and Usage Report -> ``cloudops.cost_fact`` (entity ``cur``, daily).

CUR settles late and restates: AWS rewrites a billing period's files several
times, each time as a new *assembly* (a manifest naming its gzip CSV files),
until the period closes. Every assembly is a complete statement of the period,
so it replaces, rather than adds to, what the last one said.

Scopes are report prefixes (``<prefix>/<report>``), each with its own
watermark: the ``LastModified`` of the last manifest swapped in. A run walks a
queue held in the cursor (JSON):

    listing   ListObjectsV2 under the prefix (continuation tokens followed);
              period manifests modified after ``since`` are queued, oldest first
    manifest  register the assembly in ``staging.cur_assembly``, resetting any
              earlier partial load of it; its report keys are queued next
    data      one gzip CSV file: lines parsed in Decimal, usage account resolved
              (exact alias of ``aws``), resource linked, aggregated to the
              cost_fact natural key, loaded into ``staging.cost_fact_load``
    missing   a queued object S3 does not have yet (404 stored as it came): the
              scope's run stops here, so no later manifest is swapped past an
              incomplete one and the watermark stays before it

When the last file of an assembly loads, the same transaction swaps it in: for
each account in the assembly, the period's ``cost_fact`` rows are deleted and
the assembly's rows inserted (``source_system`` ``aws``, ``source_batch`` the
assembly id), the load rows deleted, ``swapped_at`` set and the watermark
advanced to the manifest's ``LastModified``. Until then ``cost_fact`` is
untouched: no reader ever sees two assemblies, or half of one. The transaction
is scoped to every tenant of the assembly, read first from ``cur_assembly``.

Every object is stored in bronze before it is parsed, with ``object_key`` and
``last_modified`` in its metadata. Replay re-runs the same steps without moving
a watermark. An assembly whose period already has a newer swapped assembly
(by manifest ``LastModified``) is *superseded*: it loads nothing and swaps
nothing, so replaying old bronze never puts restated figures back.

Known limits:
- An account that disappears from a restating assembly keeps its rows from the
  earlier one (only the accounts an assembly names are replaced).
- The swap replaces every row of the account and period, whatever wrote it:
  for an AWS account the CUR is the authority on its bill.
- A swapped assembly re-listed in the five-minute overlap is downloaded again
  (each file is then skipped as already loaded).
- Rows are rounded to six places per file and key before the swap sums them.
- A replayed date range that holds only part of an assembly's pages leaves a
  partial load in staging (cost_fact unaffected); replay whole runs.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.aws import mapping
from cloudops_connectors.aws.client import NoSuchKeyError, S3Client
from cloudops_connectors.aws.config_source import accounts
from cloudops_connectors.core import drift, identity, state
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

LISTING: Final = "listing"
MANIFEST: Final = "manifest"
DATA: Final = "data"
MISSING: Final = "missing"

_ASSEMBLY_COLUMNS: Final = (
    "SELECT assembly_id, scope_key, invoice_period, manifest_ref, report_keys, loaded_keys,"
    " tenant_ids, account_ids, swapped_at FROM staging.cur_assembly WHERE assembly_id = :id"
)
_ASSEMBLY: Final = text(_ASSEMBLY_COLUMNS)
_ASSEMBLY_LOCKED: Final = text(_ASSEMBLY_COLUMNS + " FOR UPDATE")
_REGISTER: Final = text(
    "INSERT INTO staging.cur_assembly (assembly_id, scope_key, invoice_period, manifest_ref,"
    " report_keys) VALUES (:id, :scope, :period, :ref, CAST(:keys AS text[]))"
    " ON CONFLICT (assembly_id) DO UPDATE SET scope_key = EXCLUDED.scope_key,"
    "   invoice_period = EXCLUDED.invoice_period, manifest_ref = EXCLUDED.manifest_ref,"
    "   report_keys = EXCLUDED.report_keys, loaded_keys = '{}', tenant_ids = '{}',"
    "   account_ids = '{}'"
)
_CLEAR_LOAD: Final = text("DELETE FROM staging.cost_fact_load WHERE assembly_id = :id")
_SIBLINGS: Final = text(
    "SELECT assembly_id, manifest_ref FROM staging.cur_assembly"
    " WHERE scope_key = :scope AND invoice_period = :period AND assembly_id <> :id"
    "   AND swapped_at IS NOT NULL"
)
_RESOURCES: Final = text(
    "SELECT id, account_id, native_id FROM cloudops.resource"
    " WHERE account_id = ANY(CAST(:accounts AS uuid[]))"
)
_LOAD: Final = text(
    "INSERT INTO staging.cost_fact_load (tenant_id, account_id, assembly_id, object_key,"
    " resource_id, usage_date, invoice_period, service_native, charge_class, unblended_cost,"
    " amortized_cost, currency, raw_extra)"
    " VALUES (:tenant_id, :account_id, :assembly_id, :object_key, :resource_id, :usage_date,"
    "  :invoice_period, :service_native, CAST(:charge_class AS cloudops.charge_class),"
    "  :unblended_cost, :amortized_cost, :currency, CAST(:raw_extra AS jsonb))"
)
_LOADED: Final = text(
    "UPDATE staging.cur_assembly SET loaded_keys = array_append(loaded_keys, :key),"
    " tenant_ids = ARRAY(SELECT DISTINCT t FROM unnest(tenant_ids || CAST(:tenants AS uuid[]))"
    "                    AS t ORDER BY t),"
    " account_ids = ARRAY(SELECT DISTINCT a FROM unnest(account_ids"
    "                     || CAST(:accounts AS uuid[])) AS a ORDER BY a)"
    " WHERE assembly_id = :id RETURNING report_keys, loaded_keys, tenant_ids, account_ids"
)
_DELETE_PERIOD: Final = text(
    "DELETE FROM cloudops.cost_fact"
    " WHERE account_id = ANY(CAST(:accounts AS uuid[])) AND invoice_period = :period"
)
_SWAP: Final = text(
    "INSERT INTO cloudops.cost_fact (tenant_id, account_id, resource_id, usage_date,"
    " invoice_period, service_native, charge_class, unblended_cost, amortized_cost, currency,"
    " source_system, source_batch, raw_extra)"
    " SELECT tenant_id, account_id, resource_id, usage_date, invoice_period, service_native,"
    "  charge_class, sum(unblended_cost), sum(amortized_cost), currency, :source, assembly_id,"
    "  CASE WHEN count(*) = 1 THEN (jsonb_agg(raw_extra))->0"
    "       ELSE jsonb_build_object('parts', jsonb_agg(raw_extra ORDER BY object_key, id)) END"
    " FROM staging.cost_fact_load WHERE assembly_id = :id"
    " GROUP BY tenant_id, account_id, resource_id, usage_date, invoice_period, service_native,"
    "  charge_class, currency, assembly_id"
)
_SWAPPED: Final = text(
    "UPDATE staging.cur_assembly SET swapped_at = now() WHERE assembly_id = :id"
)


# --- the queue in the cursor ----------------------------------------------------------


def _queue(cursor: str | None) -> list[dict[str, Any]]:
    if cursor is None:
        return [{"list": None}]
    queue = json.loads(cursor)
    if not isinstance(queue, list) or not queue or not all(isinstance(q, dict) for q in queue):
        raise ValueError("bad CUR cursor")
    return queue


def _cursor(queue: list[dict[str, Any]]) -> str | None:
    return json.dumps(queue, sort_keys=True) if queue else None


async def fetch(s3: S3Client, scope: str, cursor: str | None) -> Fetched:
    head = _queue(cursor)[0]
    if "list" in head:
        prefix = f"{scope}/"
        return Fetched(await s3.list(prefix, head["list"]), ".xml",
                       {"kind": LISTING, "prefix": prefix})
    key = str(head["key"])
    try:
        obj = await s3.get(key)
    except NoSuchKeyError as exc:
        # What S3 said is the page: stored like any other, then the run stops.
        return Fetched(exc.body or b"<Error><Code>NoSuchKey</Code></Error>", ".xml",
                       {"kind": MISSING, "object_key": key})
    modified = "" if obj.last_modified is None else obj.last_modified.isoformat()
    if head["kind"] == MANIFEST:
        return Fetched(obj.body, ".json", {"kind": MANIFEST, "object_key": key,
                                           "last_modified": str(head["lm"])})
    return Fetched(obj.body, ".csv.gz", {
        "kind": DATA, "object_key": key, "last_modified": modified,
        "assembly_id": str(head["assembly"]), "manifest_ref": str(head["manifest_ref"]),
        "manifest_last_modified": str(head["manifest_lm"]),
    })


async def inspect(
    raw: RawStore, scope: str, ref: str, body: bytes, cursor: str | None
) -> PageInfo:
    queue = _queue(cursor)
    head, rest = queue[0], queue[1:]
    if "list" in head:
        since = datetime.fromisoformat((await raw.meta(ref))["since"])
        objects, token = mapping.listing(body)
        found = [*head.get("found", []), *([o.key, o.last_modified.isoformat()]
                                           for o in mapping.manifests(objects, scope, since))]
        if token is not None:
            return PageInfo(len(objects), _cursor([{"list": token, "found": found}, *rest]))
        found.sort(key=lambda f: (datetime.fromisoformat(f[1]), f[0]))
        return PageInfo(len(objects), _cursor(
            [{"kind": MANIFEST, "key": k, "lm": lm} for k, lm in found] + rest))
    kind = (await raw.meta(ref)).get("kind")
    if kind == MISSING:
        return PageInfo(0, None)
    if kind == MANIFEST:
        m = mapping.manifest(body)
        files = [{"kind": DATA, "key": k, "assembly": m.assembly_id, "manifest_ref": ref,
                  "manifest_lm": head["lm"]} for k in m.report_keys]
        return PageInfo(len(files), _cursor(files + rest))
    _, rows = mapping.cur_table(body)
    return PageInfo(len(rows), _cursor(rest))


# --- process -------------------------------------------------------------------------


@dataclass(frozen=True)
class Assembly:
    assembly_id: str
    scope_key: str
    invoice_period: str
    manifest_ref: str
    report_keys: tuple[str, ...]
    loaded_keys: tuple[str, ...]
    tenant_ids: frozenset[UUID]
    account_ids: frozenset[UUID]
    swapped_at: datetime | None

    @property
    def complete(self) -> bool:
        return set(self.report_keys) <= set(self.loaded_keys)


async def _assembly(conn: ScopedConnection, assembly_id: str, *,
                    lock: bool = False) -> Assembly | None:
    row = (await conn.execute(_ASSEMBLY_LOCKED if lock else _ASSEMBLY,
                              {"id": assembly_id})).one_or_none()
    if row is None:
        return None
    r = tuple(row)
    return Assembly(str(r[0]), str(r[1]), str(r[2]), str(r[3]), tuple(r[4]), tuple(r[5]),
                    frozenset(UUID(str(t)) for t in r[6]),
                    frozenset(UUID(str(a)) for a in r[7]), r[8])


def _who() -> str:
    return principal(mapping.SOURCE)


async def process(db: Database, raw: RawStore, ref: str, ctx: PageContext) -> PageOutcome:
    meta = await raw.meta(ref)
    kind = meta.get("kind")
    if kind in (LISTING, MISSING):
        return PageOutcome(state.RunCounts(pages=1))
    if kind == MANIFEST:
        return await _manifest(db, raw, ref, meta, ctx)
    if kind == DATA:
        return await _data(db, raw, ref, meta, ctx)
    raise ValueError("bronze page is not a CUR listing, manifest or data file")


def _last_modified(meta: dict[str, str], name: str) -> datetime:
    try:
        moment = datetime.fromisoformat(meta[name])
    except (KeyError, ValueError):
        raise ValueError(f"bronze metadata has no usable {name}") from None
    if moment.tzinfo is None:
        raise ValueError(f"bronze metadata {name} has no time zone")
    return moment


async def _manifest(
    db: Database, raw: RawStore, ref: str, meta: dict[str, str], ctx: PageContext
) -> PageOutcome:
    m = mapping.manifest(await raw.get(ref))
    modified = _last_modified(meta, "last_modified")
    async with db.transaction(TenantScope((), Role.INGEST, _who())) as conn:
        existing = await _assembly(conn, m.assembly_id)
    if existing is not None and existing.swapped_at is not None and ctx.advance:
        return PageOutcome(state.RunCounts(pages=1))  # in cost_fact already; files skip too
    tenants = existing.tenant_ids if existing is not None else frozenset()
    async with db.transaction(TenantScope(tenants, Role.INGEST, _who())) as conn:
        await conn.execute(_CLEAR_LOAD, {"id": m.assembly_id})
        await conn.execute(_REGISTER, {
            "id": m.assembly_id, "scope": ctx.scope_key, "period": m.invoice_period,
            "ref": ref, "keys": list(m.report_keys)})
        if not m.report_keys:  # nothing to wait for
            registered = await _assembly(conn, m.assembly_id, lock=True)
            if registered is not None:
                superseded = await _superseded(raw, conn, registered, modified)
                await _complete(conn, registered, superseded, modified, ctx)
    return PageOutcome(state.RunCounts(pages=1))


async def _superseded(
    raw: RawStore, conn: ScopedConnection, a: Assembly, modified: datetime
) -> bool:
    """A newer assembly of the same report and period is already swapped in."""
    for row in await conn.execute(_SIBLINGS, {"scope": a.scope_key,
                                              "period": a.invoice_period, "id": a.assembly_id}):
        try:
            theirs = _last_modified(await raw.meta(str(tuple(row)[1])), "last_modified")
        except KeyError:
            raise MissingRawError("a swapped CUR manifest is missing from bronze") from None
        if theirs > modified:
            return True
    return False


async def _complete(
    conn: ScopedConnection, a: Assembly, superseded: bool, modified: datetime,
    ctx: PageContext,
) -> None:
    """Every file is loaded: swap the assembly in (unless superseded), atomically."""
    if not superseded:
        await conn.execute(_DELETE_PERIOD, {"accounts": sorted(a.account_ids),
                                            "period": a.invoice_period})
        await conn.execute(_SWAP, {"id": a.assembly_id, "source": mapping.SOURCE})
        await conn.execute(_SWAPPED, {"id": a.assembly_id})
    await conn.execute(_CLEAR_LOAD, {"id": a.assembly_id})
    if ctx.advance:
        await state.advance_watermark(conn, mapping.SOURCE, mapping.CUR, ctx.scope_key,
                                      modified)


@dataclass(frozen=True)
class _Index:
    exact: dict[tuple[UUID, str], UUID]
    tail: dict[tuple[UUID, str], set[UUID]]

    def link(self, account: UUID, resource: str) -> UUID | None:
        """Native id, then the ARN's or path's last segment if unique in the
        account (``i-0abc`` against ``arn:...:instance/i-0abc``)."""
        hit = self.exact.get((account, resource))
        if hit is not None:
            return hit
        hits = self.tail.get((account, mapping.resource_tail(resource)), set())
        return next(iter(hits)) if len(hits) == 1 else None


async def _index(conn: ScopedConnection, account_ids: set[UUID]) -> _Index:
    exact: dict[tuple[UUID, str], UUID] = {}
    tail: dict[tuple[UUID, str], set[UUID]] = defaultdict(set)
    if account_ids:
        for row in await conn.execute(_RESOURCES, {"accounts": sorted(account_ids)}):
            r = tuple(row)
            rid, account, native = UUID(str(r[0])), UUID(str(r[1])), str(r[2])
            exact[(account, native)] = rid
            tail[(account, mapping.resource_tail(native))].add(rid)
    return _Index(exact, dict(tail))


_Key = tuple[UUID, UUID, Any, UUID | None, str | None, str, str]


async def _data(
    db: Database, raw: RawStore, ref: str, meta: dict[str, str], ctx: PageContext
) -> PageOutcome:
    key, assembly_id = meta.get("object_key", ""), meta.get("assembly_id", "")
    modified = _last_modified(meta, "manifest_last_modified")
    header, rows = mapping.cur_table(await raw.get(ref))
    found, bad = mapping.lines(rows)
    counts = state.RunCounts(pages=1, records=len(rows), rejected=bad)

    async with db.transaction(TenantScope((), Role.INGEST, _who())) as conn:
        a = await _assembly(conn, assembly_id)
        if a is None:
            raise MissingRawError("the CUR file's assembly is not registered: replay its manifest")
        if key not in a.report_keys:
            raise ValueError("the CUR file is not in its assembly's manifest")
        if key in a.loaded_keys:
            return PageOutcome(state.RunCounts(pages=1))  # loaded already (a retry, a re-list)
        resolved = await identity.resolve_ids(conn, mapping.SOURCE, {x.account for x in found})
        superseded = await _superseded(raw, conn, a, modified)
    tenant_of = {acct: t for acct, t in resolved.items() if t is not None}
    unmatched = Counter(x.account for x in found if x.account not in tenant_of)
    counts.quarantined = sum(unmatched.values())
    tenants = set(tenant_of.values()) | a.tenant_ids

    async with db.transaction(TenantScope(tenants, Role.INGEST, _who())) as conn:
        locked = await _assembly(conn, assembly_id, lock=True)
        if locked is None or key in locked.loaded_keys:
            return PageOutcome(state.RunCounts(pages=1))
        if not locked.tenant_ids <= tenants:
            raise ValueError("the CUR assembly changed while its file was read")
        account_of = await accounts(conn, tenant_of, {}) if tenant_of else {}
        index = await _index(conn, set(account_of.values()))
        totals: dict[_Key, mapping.Aggregate] = defaultdict(mapping.Aggregate)
        unlinked: Counter[identity.Unlinked] = Counter()
        mine = [x for x in found if x.account in tenant_of]
        for x in mine:
            tenant, account = tenant_of[x.account], account_of[x.account]
            resource = None if x.resource is None else index.link(account, x.resource)
            if x.resource is not None and resource is None:
                unlinked[identity.Unlinked(tenant, x.resource, mapping.resource_tail(x.resource),
                                           account)] += 1
            totals[(tenant, account, x.usage_date, resource, x.service, x.charge_class,
                    x.currency)].add(x)
        loads = [] if superseded else [_load(k, agg, assembly_id, key, locked.invoice_period)
                                       for k, agg in totals.items()]
        if loads:
            await conn.execute(_LOAD, loads)
        row = tuple((await conn.execute(_LOADED, {
            "key": key, "id": assembly_id, "tenants": sorted({k[0] for k in totals}),
            "accounts": sorted({k[1] for k in totals})})).one())
        await state.quarantine_aliases(conn, mapping.SOURCE, unmatched, ref)
        await identity.quarantine(conn, mapping.SOURCE, unlinked, ref)
        await drift.observe(conn, mapping.SOURCE, mapping.CUR, set(header),
                            mapped=mapping.CUR_MAPPED, expected=mapping.CUR_EXPECTED,
                            records=len(rows), raw_ref=ref)
        now = Assembly(
            locked.assembly_id, locked.scope_key, locked.invoice_period, locked.manifest_ref,
            tuple(row[0]), tuple(row[1]), frozenset(UUID(str(t)) for t in row[2]),
            frozenset(UUID(str(x)) for x in row[3]), locked.swapped_at)
        if not now.tenant_ids <= tenants:  # pragma: no cover - checked above
            raise ValueError("the CUR assembly's tenants are outside this transaction")
        if now.complete:
            await _complete(conn, now, superseded, modified, ctx)
    counts.written = 0 if superseded else len(mine)
    counts.ci_unlinked = sum(unlinked.values())
    return PageOutcome(counts)


def _load(k: _Key, agg: mapping.Aggregate, assembly_id: str, key: str,
          period: str) -> dict[str, Any]:
    tenant, account, usage_date, resource, service, charge_class, currency = k
    return {
        "tenant_id": tenant, "account_id": account, "assembly_id": assembly_id,
        "object_key": key, "resource_id": resource, "usage_date": usage_date,
        "invoice_period": period, "service_native": service, "charge_class": charge_class,
        "unblended_cost": mapping.stored(agg.unblended),
        "amortized_cost": mapping.stored(agg.amortized), "currency": currency,
        "raw_extra": json.dumps(agg.raw_extra(), sort_keys=True),
    }
