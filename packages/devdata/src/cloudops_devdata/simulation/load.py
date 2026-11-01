"""Load the simulation's platform rows into the local database. Local profile only.

Like ``make seed``, and reusing its insert: every row goes in through
``packages/tenancy`` as the ingest login, in one transaction under the tenant's
own scope, so row-level security checks each one. Then it reads back as the
application login and checks what each scope sees.

The tenant is found, not assumed: by its short code (``AZSPONSOR``). If it is
there (onboarded in the console, or by an earlier run), its id is used; if not,
it is onboarded through ``access.onboard_tenant`` by the local platform
administrator, as the console would, as an internal tenant (ADR 0031: Azure
Sponsorship is Getronics' own, and hosts the platform in dev). ``make seed`` must have run first: it
bootstraps that administrator and loads the FX rates margin needs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Final
from uuid import UUID

from sqlalchemy import text

from cloudops_devdata.seed import SeedError, _insert
from cloudops_devdata.simulation.model import COMPANY, SHORT_CODE, Simulation
from cloudops_devdata.simulation.rows import PlatformRows, platform_rows
from cloudops_tenancy import Database, Role, TenantScope

PRINCIPAL: Final = "simulation:local-dev"


def _scope(ids: Sequence[UUID], role: Role = Role.PLATFORM_ADMIN) -> TenantScope:
    return TenantScope(ids, role, PRINCIPAL)


async def find_tenant(ingest: Database, code: str = SHORT_CODE) -> UUID | None:
    async with ingest.transaction(_scope([])) as conn:
        everyone = list(await conn.scalar(text("SELECT cloudops.finops_tenants()")) or [])
    if not everyone:
        return None
    async with ingest.transaction(_scope(everyone)) as conn:
        found = await conn.scalar(
            text("SELECT id FROM cloudops.tenant WHERE short_code = :c"), {"c": code})
    return None if found is None else UUID(str(found))


async def onboard(app: Database, ingest: Database, admin: UUID) -> UUID:
    async with app.transaction(TenantScope.for_resolution(PRINCIPAL)) as conn:
        try:
            tenant = await conn.scalar(text("SELECT access.onboard_tenant(:a, :n, :c, true)"),
                                       {"a": admin, "n": COMPANY, "c": SHORT_CODE})
        except Exception as exc:
            raise SeedError(
                "could not onboard the simulation tenant; run `make seed` first (it creates "
                "the local platform administrator)") from exc
    tid = UUID(str(tenant))
    async with ingest.transaction(_scope([tid], Role.INGEST)) as conn:
        await conn.execute(text(
            "UPDATE cloudops.tenant SET delivery_location = 'India', coverage = '8x5',"
            " support = 'Silver', contract_start = :d WHERE id = :t"),
            {"t": tid, "d": date(2026, 1, 1)})
    return tid


async def _existing(
    ingest: Database, tid: UUID, data: PlatformRows
) -> tuple[set[tuple[str, str]], UUID | None]:
    """Aliases already registered for the tenant, and its account for the
    subscription if one exists; refuses when the estate is already loaded."""
    account = data.accounts[0]
    async with ingest.transaction(_scope([tid])) as conn:
        aliases = {(str(r.source_system), str(r.alias)) for r in await conn.execute(text(
            "SELECT source_system, alias FROM cloudops.tenant_alias"))}
        found = await conn.scalar(text(
            "SELECT id FROM cloudops.cloud_account WHERE provider = 'azure'"
            " AND native_id = :n"), {"n": account.native_id})
        if found is not None and await conn.scalar(text(
                "SELECT count(*) FROM cloudops.resource WHERE account_id = :a"), {"a": found}):
            raise SeedError("the simulation is already loaded for this tenant; "
                            "`make reset` wipes the local database")
    return aliases, None if found is None else UUID(str(found))


async def load(ingest: Database, sim: Simulation, tid: UUID) -> PlatformRows:
    data = platform_rows(sim, tid)
    aliases, account = await _existing(ingest, tid, data)
    if account is not None:
        data = platform_rows(sim, tid, account_id=account)
    data.aliases = [a for a in data.aliases if (a.source_system, a.alias) not in aliases]
    tables = data.by_table()
    if account is not None:
        tables["cloudops.cloud_account"] = []
    async with ingest.transaction(_scope([tid])) as conn:
        for table, rows in tables.items():
            await _insert(conn, table, rows)
    return data


_COUNTED: Final = ("cloudops.cloud_account", "cloudops.resource", "cloudops.work_item",
                   "cloudops.cost_fact", "cloudops.billing_total", "cloudops.sow")


async def verify(app: Database, tid: UUID, data: PlatformRows, other: UUID | None) -> None:
    """The tenant's scope sees its rows; an empty scope and another tenant's see
    none of them; an engineer sees no SOW."""
    tables = data.by_table()

    async def count(scope: TenantScope, table: str) -> int:
        async with app.transaction(scope) as conn:
            return int(await conn.scalar(text(
                f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tid}))  # noqa: S608

    failures = []
    for table in _COUNTED:
        mine = await count(_scope([tid], Role.FINOPS), table)
        if table == "cloudops.cloud_account":
            if mine < 1:
                failures.append(f"{table}: own scope sees none")
        elif mine < len(tables[table]):
            failures.append(f"{table}: own scope saw {mine}, expected {len(tables[table])}")
        if await count(_scope([], Role.FINOPS), table):
            failures.append(f"{table}: empty scope sees the simulation's rows")
        if other is not None and await count(_scope([other], Role.FINOPS), table):
            failures.append(f"{table}: another tenant's scope sees the simulation's rows")
    if await count(_scope([tid], Role.ENGINEER), "cloudops.sow"):
        failures.append("cloudops.sow: an engineer sees the SOW")
    if failures:
        raise SeedError("isolation check failed:\n  " + "\n  ".join(failures))


async def fx_gap(ingest: Database, sim: Simulation) -> int:
    """Days in the window without a USD to EUR average rate (margin needs them)."""
    async with ingest.transaction(_scope([])) as conn:
        have = int(await conn.scalar(text(
            "SELECT count(DISTINCT effective_date) FROM cloudops.fx_rate"
            " WHERE from_currency = 'USD' AND to_currency = 'EUR' AND rate_type = 'average'"
            " AND effective_date BETWEEN :a AND :b"), {"a": sim.start, "b": sim.anchor}))
    return (sim.anchor - sim.start).days + 1 - have
