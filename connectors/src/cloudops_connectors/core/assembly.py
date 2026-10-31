"""Billing-export assemblies: staged file by file, swapped into ``cost_fact`` whole.

Shared by the AWS CUR (``aws/cur_source.py``) and Azure Cost Management
exports (``azure/cost_source.py``). Both deliver a billing period as an
*assembly* (a CUR manifest, an export run's manifest) naming several files, and
each later assembly of the same period restates it in full.

- ``register`` records the assembly in ``staging.cur_assembly`` (the table is
  named for the CUR, which came first) and clears any earlier partial load.
- ``expect`` adds a file to a registered assembly: for a source that learns its
  files as it goes (the Azure query API's pages, ``azure/daily_source.py``).
- ``load_rows`` stages one file's rows in ``staging.cost_fact_load``;
  ``mark_loaded`` records the file and the tenants and accounts it touched.
- ``complete``: when the last file is in, the same transaction deletes the
  period's ``cost_fact`` rows for the assembly's accounts, inserts the staged
  rows, marks the assembly swapped and advances the watermark. Until then
  ``cost_fact`` is untouched: no reader sees two assemblies, or half of one.
- ``superseded``: a newer assembly of the same scope and period (by the
  manifest's time in bronze metadata) is already swapped in, so this one swaps
  nothing. Replaying old bronze never puts restated figures back.

A staged row's ``raw_extra`` may carry ``_tags_snapshot`` (``TAGS_KEY``): the
swap moves it to ``cost_fact.tags_snapshot`` (the first file's, by key order)
and leaves it out of ``raw_extra``. The CUR sets no such key, so its rows are
exactly what they were before this module was factored out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.core import state
from cloudops_connectors.core.ingest import MissingRawError, PageContext
from cloudops_connectors.core.raw import RawStore
from cloudops_tenancy import ScopedConnection

TAGS_KEY: Final = "_tags_snapshot"  # spelled literally in _SWAP

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
_LOAD: Final = text(
    "INSERT INTO staging.cost_fact_load (tenant_id, account_id, assembly_id, object_key,"
    " resource_id, usage_date, invoice_period, service_native, charge_class, unblended_cost,"
    " amortized_cost, currency, raw_extra)"
    " VALUES (:tenant_id, :account_id, :assembly_id, :object_key, :resource_id, :usage_date,"
    "  :invoice_period, :service_native, CAST(:charge_class AS cloudops.charge_class),"
    "  :unblended_cost, :amortized_cost, :currency, CAST(:raw_extra AS jsonb))"
)
_EXPECT: Final = text(
    "UPDATE staging.cur_assembly SET report_keys = array_append(report_keys, :key)"
    " WHERE assembly_id = :id AND NOT (:key = ANY(report_keys))"
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
    " source_system, source_batch, raw_extra, tags_snapshot)"
    " SELECT tenant_id, account_id, resource_id, usage_date, invoice_period, service_native,"
    "  charge_class, sum(unblended_cost), sum(amortized_cost), currency, :source, assembly_id,"
    "  CASE WHEN count(*) = 1 THEN (jsonb_agg(raw_extra - '_tags_snapshot'))->0"
    "       ELSE jsonb_build_object('parts', jsonb_agg(raw_extra - '_tags_snapshot'"
    "                                                  ORDER BY object_key, id)) END,"
    "  (array_agg(raw_extra -> '_tags_snapshot' ORDER BY object_key, id)"
    "     FILTER (WHERE raw_extra ? '_tags_snapshot'))[1]"
    " FROM staging.cost_fact_load WHERE assembly_id = :id"
    " GROUP BY tenant_id, account_id, resource_id, usage_date, invoice_period, service_native,"
    "  charge_class, currency, assembly_id"
)
_SWAPPED: Final = text(
    "UPDATE staging.cur_assembly SET swapped_at = now() WHERE assembly_id = :id"
)


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


async def fetch(conn: ScopedConnection, assembly_id: str, *,
                lock: bool = False) -> Assembly | None:
    row = (await conn.execute(_ASSEMBLY_LOCKED if lock else _ASSEMBLY,
                              {"id": assembly_id})).one_or_none()
    if row is None:
        return None
    r = tuple(row)
    return Assembly(str(r[0]), str(r[1]), str(r[2]), str(r[3]), tuple(r[4]), tuple(r[5]),
                    frozenset(UUID(str(t)) for t in r[6]),
                    frozenset(UUID(str(a)) for a in r[7]), r[8])


async def register(conn: ScopedConnection, assembly_id: str, scope_key: str, period: str,
                   manifest_ref: str, keys: list[str]) -> None:
    """Record the assembly afresh: any earlier partial load of it is cleared."""
    await conn.execute(_CLEAR_LOAD, {"id": assembly_id})
    await conn.execute(_REGISTER, {"id": assembly_id, "scope": scope_key, "period": period,
                                   "ref": manifest_ref, "keys": keys})


async def expect(conn: ScopedConnection, assembly_id: str, key: str) -> None:
    """One more file before the assembly is complete (idempotent)."""
    await conn.execute(_EXPECT, {"id": assembly_id, "key": key})


async def load_rows(conn: ScopedConnection, rows: list[dict[str, Any]]) -> None:
    if rows:
        await conn.execute(_LOAD, rows)


async def mark_loaded(conn: ScopedConnection, locked: Assembly, key: str,
                      tenants: set[UUID], accounts: set[UUID]) -> Assembly:
    """The file is loaded; the assembly as it now stands."""
    row = tuple((await conn.execute(_LOADED, {
        "key": key, "id": locked.assembly_id, "tenants": sorted(tenants),
        "accounts": sorted(accounts)})).one())
    return Assembly(
        locked.assembly_id, locked.scope_key, locked.invoice_period, locked.manifest_ref,
        tuple(row[0]), tuple(row[1]), frozenset(UUID(str(t)) for t in row[2]),
        frozenset(UUID(str(x)) for x in row[3]), locked.swapped_at)


def last_modified(meta: dict[str, str], name: str) -> datetime:
    try:
        moment = datetime.fromisoformat(meta[name])
    except (KeyError, ValueError):
        raise ValueError(f"bronze metadata has no usable {name}") from None
    if moment.tzinfo is None:
        raise ValueError(f"bronze metadata {name} has no time zone")
    return moment


async def superseded(
    raw: RawStore, conn: ScopedConnection, a: Assembly, modified: datetime
) -> bool:
    """A newer assembly of the same scope and period is already swapped in."""
    for row in await conn.execute(_SIBLINGS, {"scope": a.scope_key,
                                              "period": a.invoice_period, "id": a.assembly_id}):
        try:
            theirs = last_modified(await raw.meta(str(tuple(row)[1])), "last_modified")
        except KeyError:
            raise MissingRawError("a swapped manifest is missing from bronze") from None
        if theirs > modified:
            return True
    return False


async def complete(
    conn: ScopedConnection, a: Assembly, is_superseded: bool, modified: datetime,
    ctx: PageContext, *, source: str, entity: str,
) -> None:
    """Every file is loaded: swap the assembly in (unless superseded), atomically,
    with the watermark."""
    if not is_superseded:
        await conn.execute(_DELETE_PERIOD, {"accounts": sorted(a.account_ids),
                                            "period": a.invoice_period})
        await conn.execute(_SWAP, {"id": a.assembly_id, "source": source})
        await conn.execute(_SWAPPED, {"id": a.assembly_id})
    await conn.execute(_CLEAR_LOAD, {"id": a.assembly_id})
    if ctx.advance:
        await state.advance_watermark(conn, source, entity, ctx.scope_key, modified)
