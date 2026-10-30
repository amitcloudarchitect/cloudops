"""Load the synthetic dataset into the local database. Local profile only.

    CLOUDOPS_ENV=local python -m cloudops_devdata.seed [--anchor 2026-10-27] [--seed 7]

Writes through ``packages/tenancy`` as the ingest login, one transaction per
tenant under that tenant's scope, so row-level security checks every row on the
way in exactly as it will for a connector. Nothing here is superuser or owner.

Afterwards it reads back as the application login and checks what each scope
can see: each tenant only its own rows, an empty scope nothing, an engineer no
commercial rows. A seed that loads but leaks fails here, loudly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, fields
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID, uuid5

from sqlalchemy import text

from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_devdata.synthetic import NAMESPACE, Dataset, as_json, generate
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

PRINCIPAL: Final = "seed:local-dev"

# Parameters that need a cast the driver cannot infer.
_CASTS: Final = {
    "tags": "jsonb",
    "tags_snapshot": "jsonb",
    "ip_addresses": "inet[]",
    "ip_address": "inet",
}
# Dataclass fields that are not columns.
_NOT_COLUMNS: Final = frozenset({"billing_currency"})


class SeedError(Exception):
    pass


def _params(row: Any) -> dict[str, Any]:
    out = {k: v for k, v in asdict(row).items() if k not in _NOT_COLUMNS}
    for key in ("tags", "tags_snapshot"):
        if key in out and out[key] is not None:
            out[key] = as_json(out[key])
    return out


async def _insert(conn: ScopedConnection, table: str, rows: Sequence[Any]) -> None:
    if not rows:
        return
    cols = [f.name for f in fields(rows[0]) if f.name not in _NOT_COLUMNS]
    values = ", ".join(
        f"CAST(:{c} AS {_CASTS[c]})" if c in _CASTS else f":{c}" for c in cols
    )
    # Table and column names come from this module's own dataclasses, never input.
    statement = text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({values})")  # noqa: S608
    await conn.execute(statement, [_params(r) for r in rows])


def _scope(tenant_ids: Sequence[UUID], role: Role = Role.PLATFORM_ADMIN) -> TenantScope:
    return TenantScope(tenant_ids, role, PRINCIPAL)


async def load(db: Database, ds: Dataset) -> None:
    ids = [t.id for t in ds.tenants]
    async with db.transaction(_scope(ids)) as conn:
        if await conn.scalar(text("SELECT count(*) FROM cloudops.tenant")):
            raise SeedError("already seeded; `make reset` wipes the database and seeds again")
    for tenant in ds.tenants:
        rows = ds.for_tenant(tenant.id)
        async with db.transaction(_scope([tenant.id])) as conn:
            await _insert(conn, "cloudops.tenant", [tenant])
            for table, table_rows in rows.items():
                await _insert(conn, table, table_rows)
        print(f"  {tenant.short_code} {tenant.id}: " + ", ".join(
            f"{len(r)} {t.split('.')[1]}" for t, r in rows.items() if r))
    # Reference data and the alias quarantine carry no tenant_id.
    async with db.transaction(_scope([])) as conn:
        await _insert(conn, "cloudops.fx_rate", ds.fx_rates)
        await _insert(conn, "staging.tenant_alias_quarantine", ds.alias_quarantine)
    print(f"  global: {len(ds.fx_rates)} fx_rate, "
          f"{len(ds.alias_quarantine)} tenant_alias_quarantine")


_VISIBLE: Final = ("cloudops.tenant", "cloudops.cloud_account", "cloudops.resource",
                   "cloudops.work_item", "cloudops.vulnerability", "cloudops.cost_fact")


async def _counts(db: Database, scope: TenantScope) -> dict[str, int]:
    async with db.transaction(scope) as conn:
        return {
            t: int(await conn.scalar(text(f"SELECT count(*) FROM {t}")))  # noqa: S608
            for t in (*_VISIBLE, "cloudops.sow")
        }


def _expected(ds: Dataset, tenant_ids: set[UUID]) -> dict[str, int]:
    def n(rows: Sequence[Any], key: str = "tenant_id") -> int:
        return sum(1 for r in rows if getattr(r, key) in tenant_ids)

    return {
        "cloudops.tenant": n(ds.tenants, "id"),
        "cloudops.cloud_account": n(ds.accounts),
        "cloudops.resource": n(ds.resources),
        "cloudops.work_item": n(ds.work_items),
        "cloudops.vulnerability": n(ds.vulnerabilities),
        "cloudops.cost_fact": n(ds.costs),
        "cloudops.sow": n(ds.sows),
    }


async def verify(app: Database, ds: Dataset) -> None:
    """Read back as the application login and check every scope sees exactly its share."""
    failures: list[str] = []

    def check(label: str, got: Mapping[str, int], want: Mapping[str, int]) -> None:
        for table, expected in want.items():
            if got[table] != expected:
                failures.append(f"{label}: {table} saw {got[table]}, expected {expected}")

    empty = await _counts(app, _scope([], Role.FINOPS))
    check("empty scope", empty, dict.fromkeys(empty, 0))
    for t in ds.tenants:
        want = _expected(ds, {t.id})
        check(f"{t.short_code} finops", await _counts(app, _scope([t.id], Role.FINOPS)), want)
        engineer = await _counts(app, _scope([t.id], Role.ENGINEER))
        check(f"{t.short_code} engineer", engineer, {**want, "cloudops.sow": 0})
    pair = {ds.tenants[0].id, ds.tenants[1].id}
    check("two-tenant scope", await _counts(app, _scope(sorted(pair), Role.FINOPS)),
          _expected(ds, pair))
    if failures:
        raise SeedError("isolation check failed:\n  " + "\n  ".join(failures))
    print("  isolation check: each tenant scope sees only its rows; empty scope sees none; "
          "engineers see no SOW rows")


# Demo principals, created through the real access functions by the dev admin.
# (label, kind, role, tenant short codes, all tenants, restrict to first subscription)
DEMO_PRINCIPALS: Final[tuple[tuple[str, str, str, tuple[str, ...], bool, bool], ...]] = (
    ("finops-all", "staff", "FinOps", (), True, False),
    ("engineer-nwl-crl", "staff", "Engineer", ("NWL", "CRL"), False, False),
    ("customer-nwl", "customer", "Customer viewer", ("NWL",), False, False),
    ("customer-fab-one-sub", "customer", "Customer viewer", ("FAB",), False, True),
)


def demo_oid(label: str) -> UUID:
    return uuid5(NAMESPACE, f"principal:{label}")


async def principals(app: Database, ds: Dataset, admin: UUID) -> list[tuple[str, UUID]]:
    """Bootstrap the dev admin (D14) and create the demo principals."""
    by_code = {t.short_code: t.id for t in ds.tenants}
    first_account = {t.id: next(a.id for a in ds.accounts if a.tenant_id == t.id)
                     for t in ds.tenants}
    created = [("platform-admin", admin)]
    async with app.transaction(TenantScope.for_resolution("seed:local-dev")) as conn:
        await conn.execute(
            text("SELECT access.bootstrap_admin(:o, 'Local platform admin')"), {"o": admin}
        )
        for label, kind, role, codes, all_tenants, one_sub in DEMO_PRINCIPALS:
            oid = demo_oid(label)
            await conn.execute(
                text("SELECT access.set_principal(:a, :o, :k, :n, :r, :all)"),
                {"a": admin, "o": oid, "k": kind, "n": label, "r": role, "all": all_tenants},
            )
            for code in codes:
                await conn.execute(
                    text("SELECT access.grant_tenant(:a, :o, :t)"),
                    {"a": admin, "o": oid, "t": by_code[code]},
                )
            if one_sub:
                tenant = by_code[codes[0]]
                await conn.execute(
                    text("SELECT access.restrict_accounts(:a, :o, :t, CAST(:ids AS uuid[]))"),
                    {"a": admin, "o": oid, "t": tenant, "ids": [first_account[tenant]]},
                )
            created.append((label, oid))
    return created


# Triage results for a few open incidents, so the console's triage panel has
# something to review locally. Shaped like record_outcome's rows; never produced
# by a model, and the reasons say so.
SEEDED_TRIAGE: Final = 12
_TRIAGE_SHAPES: Final[tuple[tuple[str, int, list[str], str, str, str], ...]] = (
    ("human", 2, ["confidence 0.55 below 0.70"], "disk_space", "B", "low"),
    ("ready_for_planning", 3, [], "service_down", "C", "medium"),
    ("human", 3, ["action class D needs a person"], "decommission", "D", "high"),
)


async def triage(app: Database, ds: Dataset) -> int:
    open_incidents = [w for w in ds.work_items
                      if w.kind == "incident" and w.closed_at is None and w.resource_id]
    picked = open_incidents[:SEEDED_TRIAGE]
    for i, item in enumerate(picked):
        route, stage, reasons, intent, action_class, risk = _TRIAGE_SHAPES[i % 3]
        classification = {
            "intent": intent, "affected_ci": item.configuration_item or "unknown",
            "customer": item.company_raw, "action_class": action_class, "risk": risk,
            "rationale": "Seeded for the local console; not a model's output.",
            "confidence": 0.55 if route == "human" and stage == 2 else 0.86,
        }
        scope = TenantScope([item.tenant_id], Role.ENGINEER, "seed:local-dev")
        async with app.transaction(scope) as conn:
            await conn.execute(text(
                "INSERT INTO cloudops.triage_result (tenant_id, number, resource_id, route,"
                " stage, reasons, classification, enrichment, cost, cost_currency, workflow_id)"
                " VALUES (:t, :n, :r, :route, :stage, :reasons, CAST(:c AS jsonb),"
                " CAST(:e AS jsonb), :cost, 'USD', :wf)"),
                {"t": item.tenant_id, "n": item.number, "r": item.resource_id, "route": route,
                 "stage": stage, "reasons": ["seeded, not a model's output", *reasons],
                 "c": json.dumps(classification),
                 "e": None if stage < 3 else json.dumps({"similar_incidents": 2}),
                 "cost": Decimal("0.000840"), "wf": f"seed-triage-{item.number}"})
    return len(picked)


async def run(anchor: date, seed: int) -> None:
    settings = load_settings(
        os.environ, require={Component.DATABASE_INGEST, Component.DATABASE_APP}
    )
    if settings.environment is not Environment.LOCAL:
        raise SeedError("the synthetic seed is local-only; it must never touch Azure")
    ds = generate(anchor, seed)
    print(f"seeding synthetic data: cost {anchor - timedelta(days=89)} .. {anchor}, seed {seed}")
    ingest = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=1)
    app = Database.create(settings.database_dsn(DbLogin.APP), pool_size=1)
    admin = UUID(settings.secrets.get("platform-admin-oid"))
    try:
        await load(ingest, ds)
        await verify(app, ds)
        print(f"  triage results for the console: {await triage(app, ds)}")
        print("  principals (mint a token: make token OID=<oid>):")
        for label, oid in await principals(app, ds, admin):
            print(f"    {label:<22} {oid}")
    finally:
        await ingest.dispose()
        await app.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_devdata.seed")
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    parser.add_argument("--anchor", type=date.fromisoformat, default=yesterday,
                        help="last cost day (default: yesterday, UTC)")
    parser.add_argument("--seed", type=int, default=7, help="random seed (default 7)")
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined,unused-ignore]
    try:
        asyncio.run(run(args.anchor, args.seed))
    except (ConfigurationError, SeedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
