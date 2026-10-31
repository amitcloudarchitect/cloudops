"""audit.platform_action_log (migration 0020): where a request with no tenant in
scope is recorded. Written only from an empty scope, read only by auditors,
append-only for everyone; and the API records an empty-scope request there, once,
instead of failing to record it at all."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_api.access import Access
from cloudops_api.auth import Identity
from cloudops_api.pipeline import _audit
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Seeded
from .support import raw_scope, scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

_INSERT = text(
    "INSERT INTO audit.platform_action_log (actor, action, outcome)"
    " VALUES (:actor, 'GET /v1/me', 'succeeded')"
)


def _empty(role: Role = Role.PLATFORM_ADMIN) -> TenantScope:
    return TenantScope((), role, "principal:isolation-suite")


def _rows(dsn: str, actor: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as cx:
        return cx.execute(
            "SELECT action, target_kind, target_ref, outcome FROM audit.platform_action_log"
            " WHERE actor = %s ORDER BY id", (actor,)).fetchall()


async def test_an_empty_scope_can_append(app_db: Database, seeded: Seeded) -> None:
    actor = f"user:{uuid4()}"
    async with app_db.transaction(_empty()) as conn:
        await conn.execute(_INSERT, {"actor": actor})
    assert _rows(seeded.admin_dsn, actor) == [("GET /v1/me", None, None, "succeeded")]


async def test_a_scoped_request_can_never_record_itself_here(
    app_db: Database, seeded: Seeded
) -> None:
    """A request with a tenant in scope is recorded under that tenant: it cannot
    choose the tenant-less log instead."""
    actor = f"user:{uuid4()}"
    with pytest.raises(Exception, match="row-level security"):
        async with app_db.transaction(scope(seeded.a, role=Role.PLATFORM_ADMIN)) as conn:
            await conn.execute(_INSERT, {"actor": actor})
    assert _rows(seeded.admin_dsn, actor) == []


@pytest.mark.parametrize(("role", "visible"), [(Role.AUDITOR, True),
                                               (Role.PLATFORM_ADMIN, False),
                                               (Role.FINOPS, False)])
async def test_only_auditors_read_it(
    app_db: Database, seeded: Seeded, role: Role, visible: bool
) -> None:
    actor = f"user:{uuid4()}"
    async with app_db.transaction(_empty()) as conn:
        await conn.execute(_INSERT, {"actor": actor})
    async with app_db.transaction(_empty(role)) as conn:
        seen = await conn.scalar(text(
            "SELECT count(*) FROM audit.platform_action_log WHERE actor = :a"), {"a": actor})
    assert seen == (1 if visible else 0)


def test_the_ingest_login_cannot_touch_it(seeded: Seeded) -> None:
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        raw_scope(seeded.ingest_dsn, "", "platform_admin") as cx,
    ):
        cx.execute("INSERT INTO audit.platform_action_log (actor, action, outcome)"
                   " VALUES ('x', 'y', 'succeeded')")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.platform_action_log SET outcome = 'failed'",
        "DELETE FROM audit.platform_action_log",
        "TRUNCATE audit.platform_action_log",
    ],
)
def test_the_application_cannot_rewrite_it(seeded: Seeded, statement: str) -> None:
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        raw_scope(seeded.app_dsn, "", "auditor") as cx,
    ):
        cx.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.platform_action_log SET outcome = 'failed'",
        "DELETE FROM audit.platform_action_log",
        "TRUNCATE audit.platform_action_log",
    ],
)
async def test_even_the_owner_cannot_rewrite_it(
    app_db: Database, seeded: Seeded, statement: str
) -> None:
    """RLS binds the owner too (FORCE) and no policy admits an update or delete, so
    those reach no row; truncate, which RLS does not cover, the trigger refuses."""
    actor = f"user:{uuid4()}"
    async with app_db.transaction(_empty()) as conn:
        await conn.execute(_INSERT, {"actor": actor})
    if statement.startswith("TRUNCATE"):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"),
            raw_scope(seeded.migrator_dsn, "", "auditor") as cx,
        ):
            cx.execute(statement)
    else:
        with raw_scope(seeded.migrator_dsn, "", "auditor") as cx:
            assert cx.execute(statement).rowcount == 0
            cx.commit()
    assert _rows(seeded.admin_dsn, actor) == [("GET /v1/me", None, None, "succeeded")]


def test_its_policies_are_exactly_these(seeded: Seeded) -> None:
    """Pinned here, since the generic check of extra permissive policies skips this
    table (it has no tenant isolation to widen)."""
    with psycopg.connect(seeded.admin_dsn) as cx:
        got = cx.execute(
            "SELECT p.polname, p.polcmd, p.polpermissive, p.polroles = '{0}'::oid[],"
            " pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)"
            " FROM pg_policy p WHERE p.polrelid = 'audit.platform_action_log'::regclass"
            " ORDER BY 1").fetchall()
        forced = cx.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                            " WHERE oid = 'audit.platform_action_log'::regclass").fetchone()
    assert forced == (True, True)
    assert [(g[0], g[1], g[2], g[3]) for g in got] == [
        ("auditors_read", "r", True, True), ("empty_scope_only", "a", True, True)]
    assert "'auditor'" in got[0][4]
    assert got[0][5] is None
    assert got[1][4] is None
    assert "cardinality(cloudops.current_tenant_ids()) = 0" in got[1][5]


# --- the API's audit stage ----------------------------------------------------------------


def _access(tenants: tuple[Any, ...]) -> Access:
    return Access(Identity(uuid4()), "staff", "Platform administrator", frozenset(),
                  TenantScope(tenants, Role.PLATFORM_ADMIN, "principal:isolation-suite"))


def _request(**path: str) -> Any:
    return SimpleNamespace(state=SimpleNamespace(), method="GET", path_params=path)


_ROUTE: Any = SimpleNamespace(path_format="/v1/me", name="me")


async def test_an_empty_scope_request_is_recorded_once_on_the_platform_log(
    app_db: Database, seeded: Seeded
) -> None:
    """The defect it fixes: no tenant in scope used to mean no audit row could be
    written, so the API withheld every response (audit_failed)."""
    access = _access(())
    await _audit(app_db, access, _request(), _ROUTE, 200)
    actor = access.identity.principal_id
    assert _rows(seeded.admin_dsn, actor) == [("GET /v1/me", "me", None, "succeeded")]
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT count(*) FROM audit.action_log WHERE actor = %s",
                          (actor,)).fetchone() == (0,)


async def test_a_scoped_request_is_still_recorded_per_tenant_and_never_here(
    app_db: Database, seeded: Seeded
) -> None:
    access = _access((seeded.a.id, seeded.b.id))
    await _audit(app_db, access, _request(), _ROUTE, 403)
    actor = access.identity.principal_id
    assert _rows(seeded.admin_dsn, actor) == []
    with psycopg.connect(seeded.admin_dsn) as cx:
        got = cx.execute("SELECT tenant_id, outcome FROM audit.action_log WHERE actor = %s",
                         (actor,)).fetchall()
    assert sorted(got, key=str) == sorted([(seeded.a.id, "blocked"), (seeded.b.id, "blocked")],
                                          key=str)
