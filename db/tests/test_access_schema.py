"""Access control and subscription scope at the database level (migration 0004,
ADR 0002). The API tests prove the path; these prove the floor under it."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Principals, Seeded
from .support import raw_scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

ACCOUNT_SCOPED = (
    "cloudops.cloud_account",
    "cloudops.resource",
    "cloudops.cost_fact",
    "cloudops.work_item",
    "cloudops.vulnerability",
    "staging.resource_quarantine",
    "cloudops.triage_result",  # 0008
    "cloudops.triage_review",  # 0008, through its triage result
    "cloudops.cost_finding",  # 0010, by account
    "staging.cost_fact_load",  # 0010, by account
    "cloudops.directory_principal",  # 0010: tenant-wide, so all-accounts scopes only
    "cloudops.directory_role_assignment",  # 0010, likewise
    "cloudops.sharepoint_site",  # 0010, likewise
    "cloudops.document",  # 0010, likewise
    # 0013: an assessment and a report pack answer for the whole estate.
    "cloudops.assessment",
    "cloudops.control_response",
    "cloudops.evidence",
    "cloudops.evidence_pack",
    "cloudops.report_pack",
    "cloudops.report_narrative",
)
GROUP_ROLES = ("cloudops_app", "cloudops_ingest")


async def _rows(db: Database, s: TenantScope, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with db.transaction(s) as conn:
        return [tuple(r) for r in await conn.execute(text(sql), params)]


# --- account_scope (D16) ----------------------------------------------------------------


@pytest.mark.parametrize("table", ACCOUNT_SCOPED)
def test_unset_account_scope_sees_nothing_even_inside_the_tenant(
    seeded: Seeded, table: str
) -> None:
    """A path that sets the tenant but forgets app.account_ids sees nothing."""
    dsn = seeded.ingest_dsn if table.startswith("staging.") else seeded.app_dsn
    with raw_scope(dsn, str(seeded.a.id), "finops", account_ids=None) as cx:
        assert cx.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    with raw_scope(dsn, str(seeded.a.id), "finops") as cx:
        assert cx.execute(f"SELECT count(*) FROM {table}").fetchone() != (0,)


async def test_restricted_scope_sees_only_its_subscription(
    app_db: Database, seeded: Seeded
) -> None:
    c = seeded.c
    limited = TenantScope([c.id], Role.CUSTOMER, "test", account_ids=[c.account_id])
    whole = TenantScope([c.id], Role.ENGINEER, "test")

    async def ids(s: TenantScope, sql: str) -> set[Any]:
        return {r[0] for r in await _rows(app_db, s, sql)}

    assert await ids(limited, "SELECT id FROM cloudops.cloud_account") == {c.account_id}
    assert await ids(whole, "SELECT id FROM cloudops.cloud_account") == {
        c.account_id, seeded.c_account2
    }
    assert await ids(limited, "SELECT id FROM cloudops.resource") == {c.resource_id}
    assert await ids(limited, "SELECT DISTINCT account_id FROM cloudops.cost_fact") == {
        c.account_id
    }
    # Tickets and findings follow their resource; unlinked ones are hidden.
    tickets = await ids(limited, "SELECT resource_id FROM cloudops.work_item")
    assert tickets == {c.resource_id}
    assert await ids(whole, "SELECT resource_id FROM cloudops.work_item") == {
        c.resource_id, seeded.c_resource2
    }
    assert await _rows(app_db, limited, "SELECT 1 FROM cloudops.vulnerability") == []
    assert await _rows(app_db, whole, "SELECT 1 FROM cloudops.vulnerability") != []


async def test_restricted_scope_cannot_write_into_another_subscription(
    app_db: Database, seeded: Seeded
) -> None:
    c = seeded.c
    limited = TenantScope([c.id], Role.ENGINEER, "test", account_ids=[c.account_id])
    insert = (
        "INSERT INTO cloudops.resource (tenant_id, account_id, native_id, name, type_native,"
        " type_canonical) VALUES (:t, :a, :n, 'x', 'x', 'compute.vm') RETURNING 1"
    )
    with pytest.raises(Exception, match="row-level security"):
        await _rows(app_db, limited, insert, t=c.id, a=seeded.c_account2, n=str(uuid4()))


@pytest.mark.parametrize("value", ["ALL", "*", "' OR true --", "all,", "not-a-uuid"])
def test_malformed_account_scope_fails_closed(seeded: Seeded, value: str) -> None:
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "finops", account_ids=value) as cx:
        try:
            rows = cx.execute("SELECT count(*) FROM cloudops.resource").fetchone()
        except psycopg.errors.InvalidTextRepresentation:
            return
    assert rows == (0,), "anything but exactly 'all' or account ids must not widen scope"


def test_every_account_bearing_table_has_the_restrictive_account_policy(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        found = {
            r[0]
            for r in cx.execute(
                "SELECT n.nspname || '.' || c.relname FROM pg_policy p"
                " JOIN pg_class c ON c.oid = p.polrelid"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE p.polname = 'account_scope' AND NOT p.polpermissive"
            ).fetchall()
        }
    assert found == set(ACCOUNT_SCOPED)


# --- the access schema is reachable only through its functions -----------------------


def test_application_roles_hold_no_privilege_on_access_tables(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        granted = cx.execute(
            "SELECT r.rolname, c.relname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace CROSS JOIN pg_roles r"
            " WHERE n.nspname = 'access' AND c.relkind IN ('r', 'p') AND r.rolname = ANY(%s)"
            "   AND has_table_privilege(r.oid, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')",
            (list(GROUP_ROLES),),
        ).fetchall()
    assert granted == []


def test_access_functions_are_not_public_and_owned_by_nologin_roles(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        rows = cx.execute(
            "SELECT p.proname, o.rolname, o.rolcanlogin OR o.rolsuper OR o.rolbypassrls,"
            "       has_function_privilege('public', p.oid, 'EXECUTE'),"
            "       has_function_privilege('cloudops_ingest', p.oid, 'EXECUTE'),"
            "       p.prosecdef, coalesce(array_to_string(p.proconfig, ','), '')"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " JOIN pg_roles o ON o.oid = p.proowner"
            " WHERE n.nspname = 'access' AND p.prokind = 'f' AND p.prorettype <> 'trigger'::regtype"
        ).fetchall()
    assert rows
    for name, owner, powerful, public, ingest, secdef, config in rows:
        assert owner in {"cloudops_resolver", "cloudops_access_admin"}, name
        assert not powerful, f"{name} owner can log in or bypass RLS"
        assert not public, f"{name} is executable by PUBLIC"
        assert not ingest, f"{name} is executable by the ingest role"
        assert secdef, f"{name} is not SECURITY DEFINER"
        assert "search_path=pg_catalog" in config, f"{name} has no pinned search_path"


def test_the_only_extra_permissive_policies_are_the_resolvers_registries(
    seeded: Seeded,
) -> None:
    """A second permissive policy ORs with tenant_isolation and widens it, so
    each one must be known and confined to a role no login can use. Onboarding
    (0009) inserts a tenant no scope holds yet: insert only, function owner only.
    The re-process queue (0009) is claimed before its tenant is known: waiting
    requests only, moved to running only, function owner only. Portal
    onboarding (0011) is read by the resolver before any scope exists and
    written by the access functions' owner only. The drafting queue (0013)
    lists requested drafts across tenants: requested only, three columns,
    function owner only."""
    with psycopg.connect(seeded.admin_dsn) as cx:
        extra = cx.execute(
            "SELECT c.relname, p.polname, array(SELECT rolname FROM pg_roles"
            "        WHERE oid = ANY(p.polroles)) FROM pg_policy p"
            " JOIN pg_class c ON c.oid = p.polrelid"
            " WHERE p.polpermissive AND p.polname <> 'tenant_isolation' ORDER BY 1, 2"
        ).fetchall()
    assert extra == [
        ("alias_reprocess", "reprocess_queue_claim", ["cloudops_access_admin"]),
        ("alias_reprocess", "reprocess_queue_read", ["cloudops_access_admin"]),
        ("control_response", "draft_queue_read", ["cloudops_resolver"]),
        ("llm_call", "spend_registry_read", ["cloudops_resolver"]),
        ("portal_onboarding", "access_admin_all", ["cloudops_access_admin"]),
        ("portal_onboarding", "resolver_read", ["cloudops_resolver"]),
        ("portal_role_agreement", "access_admin_all", ["cloudops_access_admin"]),
        ("portal_role_agreement", "resolver_read", ["cloudops_resolver"]),
        ("report_narrative", "draft_queue_read", ["cloudops_resolver"]),
        ("tenant", "tenant_onboarding", ["cloudops_access_admin"]),
        ("tenant", "tenant_registry_read", ["cloudops_resolver"]),
        ("tenant_alias", "alias_registry_read", ["cloudops_resolver"]),
    ]


# --- matrix and assignment guards (D10, D14, D15) ------------------------------------------


async def _call(db: Database, sql: str, **params: Any) -> None:
    await _rows(db, TenantScope.for_resolution("test:guards"), sql, **params)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (
            "SELECT access.grant_permission(:admin, 'Engineer', 'commercial.read')",
            "commercial permission",
        ),
        (
            "SELECT access.revoke_permission(:admin, 'Platform administrator', 'access.manage')",
            "cannot be removed",
        ),
        ("SELECT access.disable_principal(:admin, :admin)", "last platform administrator"),
        ("SELECT access.bootstrap_admin(gen_random_uuid(), 'second')", "already exists"),
        (
            "SELECT access.set_principal(:admin, gen_random_uuid(), 'staff', 'x',"
            " 'Customer viewer', false)",
            "role class does not match",
        ),
        (
            "SELECT access.set_principal(:admin, gen_random_uuid(), 'customer', 'x',"
            " 'Customer viewer', true)",
            "check",
        ),
        (
            "SELECT access.grant_tenant(:customer, :customer, gen_random_uuid())",
            "not permitted",
        ),
        (
            "SELECT access.restrict_accounts(:admin, :customer, :tenant_b,"
            " ARRAY[gen_random_uuid()])",
            "foreign key",
        ),
    ],
)
async def test_access_guards_refuse(
    app_db: Database, principals: Principals, seeded: Seeded, sql: str, message: str
) -> None:
    with pytest.raises(Exception, match=message):
        await _call(
            app_db, sql,
            admin=principals.admin, customer=principals.customer_a, tenant_b=seeded.b.id,
        )


def test_access_changes_are_logged_and_the_log_is_append_only(
    seeded: Seeded, principals: Principals
) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        actions = {
            r[0] for r in cx.execute("SELECT DISTINCT action FROM access.change_log").fetchall()
        }
        assert {"bootstrap_admin", "set_principal", "grant_tenant", "restrict_accounts",
                "define_role", "grant_permission", "disable_principal"} <= actions
        for statement in ("UPDATE access.change_log SET actor = 'x'",
                          "DELETE FROM access.change_log", "TRUNCATE access.change_log"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cx.execute(statement)
