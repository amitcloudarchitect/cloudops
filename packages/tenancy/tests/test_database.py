"""Integration tests: scope reaches PostgreSQL, stays inside its transaction, fails closed."""

from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import (
    Database,
    Role,
    ScopeClosedError,
    ScopedConnection,
    ScopeTamperingError,
    TenantScope,
    UnsafeDatabaseRoleError,
)

from .conftest import Provisioned

pytestmark = pytest.mark.integration

PROBE_TENANTS = text("SELECT tenant_id FROM cloudops.tenancy_probe")
SETTINGS = text(
    "SELECT current_setting('app.tenant_ids', true), current_setting('app.role', true), "
    "pg_backend_pid()"
)


def scope(*tenants: UUID, role: Role = Role.ENGINEER) -> TenantScope:
    return TenantScope(tenants, role, "principal:test")


async def visible_tenants(conn: ScopedConnection) -> list[UUID]:
    return sorted(r[0] for r in await conn.execute(PROBE_TENANTS))


async def settings(conn: ScopedConnection) -> tuple[str, str, int]:
    row = tuple((await conn.execute(SETTINGS)).one())
    return str(row[0]), str(row[1]), int(row[2])


class BoomError(Exception):
    pass


# --- scope is set -----------------------------------------------------------


async def test_scope_is_set_inside_the_transaction(db: Database, provisioned: Provisioned) -> None:
    s = scope(provisioned.tenant_b, provisioned.tenant_a, role=Role.FINOPS)
    async with db.transaction(s) as conn:
        tenant_ids, role, _ = await settings(conn)
        parsed = await conn.scalar(text("SELECT cloudops.current_tenant_ids()"))
    assert tenant_ids == s.tenant_ids_setting
    assert role == "finops"
    assert sorted(parsed) == sorted([provisioned.tenant_a, provisioned.tenant_b])


@pytest.mark.isolation
async def test_rls_returns_only_scoped_tenants(db: Database, provisioned: Provisioned) -> None:
    p = provisioned
    async with db.transaction(scope(p.tenant_a)) as conn:
        assert await visible_tenants(conn) == [p.tenant_a] * p.rows_per_tenant[p.tenant_a]
    async with db.transaction(scope(p.tenant_a, p.tenant_b)) as conn:
        seen = await visible_tenants(conn)
    assert set(seen) == {p.tenant_a, p.tenant_b}
    assert len(seen) == p.rows_per_tenant[p.tenant_a] + p.rows_per_tenant[p.tenant_b]


@pytest.mark.isolation
async def test_role_reaches_the_restrictive_policy(db: Database, provisioned: Provisioned) -> None:
    margin = text("SELECT count(*) FROM cloudops.tenancy_commercial_probe")
    async with db.transaction(scope(provisioned.tenant_a, role=Role.CFO)) as conn:
        assert await conn.scalar(margin) == 1
    async with db.transaction(scope(provisioned.tenant_a, role=Role.ENGINEER)) as conn:
        assert await conn.scalar(margin) == 0


@pytest.mark.isolation
async def test_write_into_another_tenant_is_rejected(
    db: Database, provisioned: Provisioned
) -> None:
    insert = text("INSERT INTO cloudops.tenancy_probe (tenant_id, label) VALUES (:t, 'x')")
    with pytest.raises(Exception, match="row-level security"):
        async with db.transaction(scope(provisioned.tenant_a)) as conn:
            await conn.execute(insert, {"t": provisioned.tenant_b})


# --- scope does not leak between transactions on a pooled connection ---------


async def server_connection_state(db: Database) -> tuple[str | None, str | None, int, int]:
    """Inspect the pooled server connection *between* transactions.

    This is what PgBouncer hands to the next client, which may be any service
    or tool. Test-only: it reaches past the package's public surface, which is
    the point — nothing outside this package can.
    """
    async with db._engine.connect() as raw:
        row = (
            await raw.execute(
                text(
                    "SELECT current_setting('app.tenant_ids', true),"
                    " current_setting('app.role', true),"
                    " (SELECT count(*) FROM cloudops.tenancy_probe),"
                    " pg_backend_pid()"
                )
            )
        ).one()
        await raw.rollback()
    return row[0], row[1], row[2], row[3]


@pytest.mark.isolation
async def test_server_connection_carries_no_scope_after_commit_or_rollback(
    db: Database, provisioned: Provisioned
) -> None:
    async with db.transaction(scope(provisioned.tenant_a, role=Role.CFO)) as conn:
        *_, pid = await settings(conn)
        assert await visible_tenants(conn)

    tenant_ids, role, rows, pid_after = await server_connection_state(db)
    assert pid_after == pid, "pool of one must hand back the same server connection"
    assert (tenant_ids or "", role or "", rows) == ("", "", 0)

    async def fail_inside() -> None:
        async with db.transaction(scope(provisioned.tenant_b, role=Role.CFO)) as conn:
            assert await visible_tenants(conn)
            raise BoomError

    with pytest.raises(BoomError):
        await fail_inside()

    tenant_ids, role, rows, pid_after = await server_connection_state(db)
    assert pid_after == pid
    assert (tenant_ids or "", role or "", rows) == ("", "", 0)


@pytest.mark.isolation
async def test_scope_does_not_survive_commit_on_a_reused_connection(
    db: Database, provisioned: Provisioned
) -> None:
    async with db.transaction(scope(provisioned.tenant_a, role=Role.CFO)) as conn:
        *_, pid_first = await settings(conn)
        assert await visible_tenants(conn)

    async with db.transaction(scope(provisioned.tenant_b)) as conn:
        tenant_ids, role, pid_second = await settings(conn)
        seen = await visible_tenants(conn)

    assert pid_second == pid_first, "pool of one must hand back the same server connection"
    assert tenant_ids == str(provisioned.tenant_b)
    assert role == "engineer"
    assert set(seen) == {provisioned.tenant_b}


@pytest.mark.isolation
async def test_scope_does_not_survive_rollback_on_a_reused_connection(
    db: Database, provisioned: Provisioned
) -> None:
    pids: list[int] = []

    async def fail_inside() -> None:
        async with db.transaction(scope(provisioned.tenant_a, role=Role.CFO)) as conn:
            pids.append((await settings(conn))[2])
            raise BoomError

    with pytest.raises(BoomError):
        await fail_inside()
    pid_first = pids[0]

    async with db.transaction(scope()) as conn:
        tenant_ids, role, pid_second = await settings(conn)
        seen = await visible_tenants(conn)

    assert pid_second == pid_first
    assert (tenant_ids, role, seen) == ("", "engineer", [])


@pytest.mark.isolation
async def test_concurrent_transactions_each_see_only_their_scope(
    provisioned: Provisioned,
) -> None:
    p = provisioned
    database = Database.create(p.app_dsn, pool_size=3, max_overflow=0)
    started = asyncio.Barrier(3)

    async def read(s: TenantScope) -> set[UUID]:
        async with database.transaction(s) as conn:
            await started.wait()  # all three transactions open at once
            return set(await visible_tenants(conn))

    try:
        results = await asyncio.gather(
            read(scope(p.tenant_a)), read(scope(p.tenant_b)), read(scope(p.tenant_c))
        )
    finally:
        await database.dispose()
    assert list(results) == [{p.tenant_a}, {p.tenant_b}, {p.tenant_c}]


@pytest.mark.isolation
def test_session_level_set_would_leak(provisioned: Provisioned) -> None:
    """Control: the hazard the tests above guard against is real on this server.

    A plain SET survives COMMIT on the same connection; SET LOCAL does not. If
    this ever fails, the leak tests above are no longer proving anything.
    """
    other = str(provisioned.tenant_a)
    with psycopg.connect(provisioned.app_dsn) as cx:
        cx.execute("SELECT set_config('app.tenant_ids', %s, true)", (other,))
        cx.commit()
        local_after = cx.execute("SELECT current_setting('app.tenant_ids', true)").fetchone()
        cx.commit()
        cx.execute("SELECT set_config('app.tenant_ids', %s, false)", (other,))
        cx.commit()
        session_after = cx.execute("SELECT current_setting('app.tenant_ids', true)").fetchone()
    assert local_after == ("",)
    assert session_after == (other,)


# --- empty scope yields zero rows, not all rows -------------------------------


@pytest.mark.isolation
async def test_empty_scope_yields_zero_rows(db: Database, provisioned: Provisioned) -> None:
    async with db.transaction(scope(role=Role.PLATFORM_ADMIN)) as conn:
        for table in ("tenancy_probe", "tenancy_commercial_probe"):
            assert await conn.scalar(text(f"SELECT count(*) FROM cloudops.{table}")) == 0


@pytest.mark.isolation
async def test_empty_scope_cannot_write(db: Database, provisioned: Provisioned) -> None:
    insert = text("INSERT INTO cloudops.tenancy_probe (tenant_id, label) VALUES (:t, 'x')")
    with pytest.raises(Exception, match="row-level security"):
        async with db.transaction(scope()) as conn:
            await conn.execute(insert, {"t": provisioned.tenant_a})


@pytest.mark.isolation
def test_app_role_without_any_scope_sees_zero_rows(provisioned: Provisioned) -> None:
    """Even a connection that bypassed this package entirely gets nothing."""
    with psycopg.connect(provisioned.app_dsn) as cx:
        count = cx.execute("SELECT count(*) FROM cloudops.tenancy_probe").fetchone()
    assert count == (0,)


# --- scope cannot be changed or escaped from inside --------------------------


@pytest.mark.isolation
@pytest.mark.parametrize(
    "statement",
    [
        "SET app.tenant_ids = '{tenant}'",
        "SET LOCAL app.role = 'cfo'",
        "SELECT set_config('app.tenant_ids', '{tenant}', true)",
        "RESET app.tenant_ids",
        "COMMIT",
        "SET ROLE postgres",
    ],
)
async def test_statements_that_change_scope_are_refused_before_they_run(
    db: Database, provisioned: Provisioned, statement: str
) -> None:
    sql = statement.format(tenant=provisioned.tenant_b)
    async with db.transaction(scope(provisioned.tenant_a)) as conn:
        with pytest.raises(ScopeTamperingError):
            await conn.execute(text(sql))
        # Refused at the statement, so the transaction and its scope are intact.
        assert set(await visible_tenants(conn)) == {provisioned.tenant_a}


@pytest.mark.isolation
async def test_scope_changed_behind_the_guard_is_caught_at_commit(
    db: Database, provisioned: Provisioned
) -> None:
    """Second layer: if anything ever slips past the guard, the commit refuses."""
    from cloudops_tenancy.database import _INTERNAL

    widen = text("SELECT set_config('app.tenant_ids', :t, true)")
    insert = text("INSERT INTO cloudops.tenancy_probe (tenant_id, label) VALUES (:t, 'x')")

    async def widen_then_write() -> None:
        async with db.transaction(scope(provisioned.tenant_a)) as conn:
            await conn._conn.execute(
                widen, {"t": str(provisioned.tenant_b)}, execution_options={_INTERNAL: True}
            )
            await conn.execute(insert, {"t": provisioned.tenant_b})

    with pytest.raises(ScopeTamperingError):
        await widen_then_write()
    async with db.transaction(scope(provisioned.tenant_b)) as conn:
        assert len(await visible_tenants(conn)) == provisioned.rows_per_tenant[provisioned.tenant_b]


async def test_account_scope_is_set_and_widening_it_is_caught_at_commit(
    db: Database, provisioned: Provisioned
) -> None:
    """app.account_ids is applied with the rest of the scope, and re-verified."""
    from cloudops_tenancy.database import _INTERNAL

    account = UUID("00000000-0000-4000-8000-0000000000ac")
    restricted = TenantScope([provisioned.tenant_a], Role.CUSTOMER, "p", account_ids=[account])
    read = text("SELECT current_setting('app.account_ids', true)")
    async with db.transaction(restricted) as conn:
        assert await conn.scalar(read) == str(account)
    async with db.transaction(scope(provisioned.tenant_a)) as conn:
        assert await conn.scalar(read) == "all"

    async def widen() -> None:
        async with db.transaction(restricted) as conn:
            await conn._conn.execute(
                text("SELECT set_config('app.account_ids', 'all', true)"),
                execution_options={_INTERNAL: True},
            )

    with pytest.raises(ScopeTamperingError):
        await widen()


async def test_connection_is_dead_after_its_transaction(
    db: Database, provisioned: Provisioned
) -> None:
    async with db.transaction(scope(provisioned.tenant_a)) as conn:
        pass
    with pytest.raises(ScopeClosedError):
        await conn.execute(PROBE_TENANTS)


async def test_scoped_connection_exposes_no_transaction_control(
    db: Database, provisioned: Provisioned
) -> None:
    async with db.transaction(scope(provisioned.tenant_a)) as conn:
        for name in ("commit", "rollback", "begin", "connection", "exec_driver_sql", "_engine"):
            assert not hasattr(conn, name)
        with pytest.raises(AttributeError):
            conn.anything = 1  # type: ignore[attr-defined]


async def test_raw_strings_are_refused(db: Database, provisioned: Provisioned) -> None:
    async with db.transaction(scope(provisioned.tenant_a)) as conn:
        with pytest.raises(TypeError):
            await conn.execute("SELECT 1")  # type: ignore[arg-type]


async def test_transaction_requires_a_tenant_scope(db: Database) -> None:
    for bogus in (None, {"tenant_ids": [], "role": "cfo"}, "tenant-a"):
        with pytest.raises(TypeError):
            async with db.transaction(bogus):  # type: ignore[arg-type]
                pass


def test_database_cannot_be_built_around_an_engine() -> None:
    with pytest.raises(TypeError):
        Database(object())  # type: ignore[arg-type]


def test_only_postgresql_is_accepted() -> None:
    with pytest.raises(Exception, match="PostgreSQL only"):
        Database.create("sqlite:///:memory:")


# --- the role itself must not be able to bypass RLS --------------------------


@pytest.mark.isolation
@pytest.mark.parametrize("which", ["admin_dsn", "owner_member_dsn"])
async def test_unsafe_roles_are_refused(provisioned: Provisioned, which: str) -> None:
    database = Database.create(getattr(provisioned, which), pool_size=1, max_overflow=0)
    try:
        with pytest.raises(UnsafeDatabaseRoleError):
            async with database.transaction(scope(provisioned.tenant_a)):
                pass
    finally:
        await database.dispose()
