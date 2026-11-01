"""Project task isolation (migration 0036, ADR 0038).

``cloudops.project_task`` carries a price: tenant isolation, the restrictive
commercial policy, and per-customer scope only. It is the one commercial table a
connector writes, so the policy admits the ingest role class, but only on a login
that is a member of ``cloudops_ingest``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Seeded
from .support import raw_scope, scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

COMMERCIAL = {Role.CEO, Role.CFO, Role.FINOPS, Role.DELIVERY_LEAD, Role.COMMERCIAL,
              Role.AUDITOR, Role.PLATFORM_ADMIN}
SELECT = "SELECT number, price, price_currency FROM cloudops.project_task"


async def _rows(db: Database, s: TenantScope, sql: str = SELECT,
                **params: Any) -> list[tuple[Any, ...]]:
    async with db.transaction(s) as conn:
        return [tuple(r) for r in await conn.execute(text(sql), params)]


def _ingest(*tenants: Any) -> TenantScope:
    return TenantScope([t.id for t in tenants], Role.INGEST, "connector:servicenow")


async def test_a_commercial_role_sees_its_own_tenant_only(app_db: Database,
                                                          seeded: Seeded) -> None:
    rows = await _rows(app_db, scope(seeded.a, role=Role.FINOPS))
    assert rows == [(f"PRJTASK-{seeded.a.name}", rows[0][1], seeded.a.currency)]
    numbers = {r[0] for r in await _rows(app_db, scope(seeded.a, seeded.b, role=Role.CFO))}
    assert numbers == {f"PRJTASK-{seeded.a.name}", f"PRJTASK-{seeded.b.name}"}


@pytest.mark.parametrize("role", [r for r in Role
                                  if r not in (Role.UNRESOLVED, Role.CUSTOMER)])
async def test_only_commercial_roles_see_a_price(app_db: Database, seeded: Seeded,
                                                 role: Role) -> None:
    """Engineers, SecOps, and the application login claiming the ingest role
    class: no rows, though the tenant is in scope."""
    rows = await _rows(app_db, scope(seeded.a, role=role))
    assert len(rows) == (1 if role in COMMERCIAL else 0)


async def test_a_customer_scope_is_refused_the_table(app_db: Database, seeded: Seeded) -> None:
    """The first lock (ADR 0018): a customer scope runs as cloudops_customer,
    which holds no grant on the table at all."""
    with pytest.raises(Exception, match="permission denied"):
        await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER))


@pytest.mark.parametrize("role", ["customer", "customer_admin", "ingest", "", "FINOPS"])
def test_unknown_customer_and_spoofed_roles_see_nothing(seeded: Seeded, role: str) -> None:
    with raw_scope(seeded.app_dsn, str(seeded.a.id), role) as cx:
        assert cx.execute(SELECT).fetchall() == []
        assert cx.execute("SELECT count(*) FROM cloudops.resource").fetchone() == (1,)


def test_a_subscription_limited_scope_sees_nothing(seeded: Seeded) -> None:
    """Per customer, never per subscription, like margin."""
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "finops",
                   account_ids=str(seeded.a.account_id)) as cx:
        assert cx.execute(SELECT).fetchall() == []


async def test_the_ingest_login_reads_and_writes_its_scoped_tenant_only(
    ingest_db: Database, seeded: Seeded
) -> None:
    assert [r[0] for r in await _rows(ingest_db, _ingest(seeded.a))] == [
        f"PRJTASK-{seeded.a.name}"]
    now = datetime.now(UTC)
    insert = ("INSERT INTO cloudops.project_task (tenant_id, sys_id, number, company_raw,"
              " status, source_updated_at) VALUES (:t, :s, 'PRJTASK-X', 'X', 'open', :now)")
    with pytest.raises(Exception, match="row-level security"):
        async with ingest_db.transaction(_ingest(seeded.a)) as conn:
            await conn.execute(text(insert), {"t": seeded.b.id, "s": uuid4().hex, "now": now})


async def test_the_ingest_login_cannot_move_a_task_to_another_tenant(
    ingest_db: Database, seeded: Seeded
) -> None:
    with pytest.raises(Exception, match="row-level security"):
        async with ingest_db.transaction(_ingest(seeded.a)) as conn:
            await conn.execute(text(
                "UPDATE cloudops.project_task SET tenant_id = :b WHERE tenant_id = :a"),
                {"a": seeded.a.id, "b": seeded.b.id})


def test_the_application_cannot_write(seeded: Seeded) -> None:
    with (raw_scope(seeded.app_dsn, str(seeded.a.id), "platform_admin") as cx,
          pytest.raises(psycopg.errors.InsufficientPrivilege)):
        cx.execute("UPDATE cloudops.project_task SET price = 0")


def test_the_source_status_says_one_word_and_nothing_else(seeded: Seeded) -> None:
    with psycopg.connect(seeded.app_dsn) as cx:
        assert cx.execute("SELECT state FROM cloudops.project_task_source()").fetchone() in {
            ("never_synced",), ("available",), ("source_absent",)}


# --- cloudops.project_services: the revenue table's read (ADR 0038) -----------------

SERVICES = ("SELECT tenant_id, currency, delivered, tasks, delivered_on"
            " FROM cloudops.project_services(:a, :b)")


def _today() -> dict[str, Any]:
    day = datetime.now(UTC).date()
    return {"a": day, "b": day}


async def test_project_services_sums_each_tenant_in_its_own_currency(
    app_db: Database, seeded: Seeded
) -> None:
    """Invoker's rights: the caller's scope and role decide every row."""
    rows = await _rows(app_db, scope(seeded.a, seeded.b, role=Role.CFO), SERVICES, **_today())
    assert {(r[0], r[1], r[3]) for r in rows} == {
        (seeded.a.id, seeded.a.currency, 1), (seeded.b.id, seeded.b.currency, 1)}
    assert {r[2] for r in rows} == {12500}
    assert {r[4] for r in rows} == {_today()["a"]}  # per day: converted at that day's rate
    flags = await _rows(app_db, scope(seeded.a, role=Role.FINOPS),
                        "SELECT provisional FROM cloudops.project_services(:a, :b)", **_today())
    assert flags == [(True,)]


@pytest.mark.parametrize("role", [Role.ENGINEER, Role.SECOPS, Role.INGEST])
async def test_project_services_is_empty_for_non_commercial_roles(
    app_db: Database, seeded: Seeded, role: Role
) -> None:
    assert await _rows(app_db, scope(seeded.a, role=role), SERVICES, **_today()) == []


async def test_project_services_is_refused_to_a_customer_scope(
    app_db: Database, seeded: Seeded
) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER), SERVICES, **_today())


def test_project_services_is_invoker_and_only_the_application_may_run_it(
    seeded: Seeded
) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        definer = cx.execute("SELECT prosecdef FROM pg_proc WHERE oid ="
                             " 'cloudops.project_services(date, date)'::regprocedure").fetchone()
        grants = cx.execute(
            "SELECT r FROM unnest(ARRAY['cloudops_app', 'cloudops_customer', 'cloudops_ingest'])"
            " r WHERE has_function_privilege(r, 'cloudops.project_services(date, date)',"
            " 'EXECUTE')").fetchall()
    assert definer == (False,)
    assert grants == [("cloudops_app",)]
