"""Tenant lifecycle at the database level (migration 0036, ADR 0038): editing a
tenant, offboarding it (out of every scope at once, credentials revoked, portal
suspended, names to quarantine), restoring it within the retention, and purging it
after: every tenant table emptied of it, the audit log and a tombstone kept.

The module has its own database: it offboards and purges the seeded tenants."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Seeded, _access, _database, _seeded, onboard_portal

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

# The tables a purge keeps rows of: the audit log (LLD: never dropped) and the
# purge's own record. The tenant row stays too, as the tombstone (no tenant_id).
KEPT = {"audit.action_log", "audit.tenant_purge"}
# Columns that hold a tenant id under another name. Each must be a nullable foreign
# key to cloudops.tenant, which the purge clears; anything else fails the catalog test.
OTHER_TENANT_REFERENCES = {"staging.tenant_alias_quarantine.suggested_tenant_id"}
# Arrays of tenant ids in ingest state, from which the purge removes the tenant.
ARRAY_REFERENCES = {"staging.cur_assembly.tenant_ids"}
PLATFORM_SCHEMAS = ("cloudops", "staging", "audit", "access")

OFFBOARD = "SELECT access.offboard_tenant(:a, :t, :name, :reason)"
RESTORE = "SELECT access.restore_tenant(:a, :t)"
REQUEST = "SELECT access.request_tenant_purge(:a, :t, :name, :code)"
UPDATE = "SELECT access.update_tenant(:a, :t, :name, :code)"


@dataclass(frozen=True)
class Life:
    seeded: Seeded
    app: Database
    ingest: Database
    admin: UUID  # platform administrator, every tenant
    admin_a: UUID  # platform administrator, tenant a only
    mapper: UUID  # Customer master, every tenant
    engineer_a: UUID
    finops: UUID  # every tenant
    cxo_a: UUID  # tenant a's portal administrator
    cxo_c: UUID
    credential_a: UUID


@pytest.fixture(scope="module")
async def life(admin_server: str) -> AsyncIterator[Life]:
    with _database(admin_server, "life") as provisioned:
        seeded = _seeded(provisioned)
        app = Database.create(seeded.app_dsn, pool_size=2, max_overflow=0)
        ingest = Database.create(seeded.ingest_dsn, pool_size=2, max_overflow=0)
        admin, admin_a, mapper, engineer_a, finops, cxo_a, cxo_c = (uuid4() for _ in range(7))
        a, c = seeded.a.id, seeded.c.id
        async with app.transaction(TenantScope.for_resolution("setup:life")) as conn:
            await _access(conn, "SELECT access.bootstrap_admin(:o, 'Admin')", o=admin)
            for oid, role, tenant in ((admin_a, "Platform administrator", a),
                                      (mapper, "Customer master", None),
                                      (engineer_a, "Engineer", a),
                                      (finops, "FinOps", None)):
                await _access(conn, "SELECT access.set_principal(:a, :o, 'staff', :n, :r, :all)",
                              a=admin, o=oid, n=f"{role} {oid}", r=role, all=tenant is None)
                if tenant is not None:
                    await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)",
                                  a=admin, o=oid, t=tenant)
            await onboard_portal(conn, admin, a, cxo_a)
            await onboard_portal(conn, admin, c, cxo_c)
        async with app.transaction(_admin_scope(a)) as conn:
            credential = await conn.scalar(text(
                "SELECT access.onboard_cloud_credential(:a, :t, 'azure', :d, :c, NULL,"
                " CAST(:ids AS text[]), :e)"),
                {"a": admin, "t": a, "d": uuid4(), "c": uuid4(), "ids": [str(uuid4())],
                 "e": datetime.now(UTC) + timedelta(days=60)})
        try:
            yield Life(seeded, app, ingest, admin, admin_a, mapper, engineer_a, finops, cxo_a,
                       cxo_c, UUID(str(credential)))
        finally:
            await app.dispose()
            await ingest.dispose()


def _admin_scope(*tenants: UUID) -> TenantScope:
    return TenantScope(list(tenants), Role.PLATFORM_ADMIN, "test:life")


async def _resolve(life: Life, oid: UUID) -> tuple[Any, ...] | None:
    async with life.app.transaction(TenantScope.for_resolution("test:life")) as conn:
        row = (await conn.execute(text(
            "SELECT kind, role_class, tenant_ids FROM access.resolve_principal_v2(:o)"),
            {"o": oid})).one_or_none()
    return None if row is None else tuple(row)


async def _scope_of(life: Life, oid: UUID) -> TenantScope:
    """As the API resolves it: the tenants the database gives this principal now."""
    resolved = await _resolve(life, oid)
    assert resolved is not None
    role = Role.CUSTOMER if resolved[0] == "customer" else Role(resolved[1])
    return TenantScope(resolved[2], role, f"test:{oid}")


async def _call(db: Database, scope: TenantScope, sql: str, /, **params: Any) -> Any:
    async with db.transaction(scope) as conn:
        return await conn.scalar(text(sql), params)


async def _refused(db: Database, scope: TenantScope, sql: str, sqlstate: str, /,
                   **params: Any) -> None:
    with pytest.raises(DBAPIError) as caught:
        await _call(db, scope, sql, **params)
    assert getattr(caught.value.orig, "sqlstate", None) == sqlstate, caught.value


def _admin_sql(life: Life, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(life.seeded.admin_dsn, autocommit=True) as cx:
        cur = cx.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _backdate(life: Life, tenant: UUID) -> None:
    """The retention, over: as the lifecycle functions' owner (the guard admits it)."""
    with psycopg.connect(life.seeded.admin_dsn) as cx:
        cx.execute("SET LOCAL ROLE cloudops_resolver")
        cx.execute("UPDATE cloudops.tenant SET purge_after = now() - interval '1 minute'"
                   " WHERE id = %s", (tenant,))


async def _new_tenant(life: Life, name: str, code: str, alias: str | None = None) -> UUID:
    created = await _call(life.app, _admin_scope(),
                          "SELECT access.onboard_tenant(:a, :n, :c, false)",
                          a=life.admin, n=name, c=code)
    tenant = UUID(str(created))
    if alias is not None:
        _admin_sql(life, "INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias)"
                         " VALUES (%s, 'servicenow', %s)", tenant, alias)
    return tenant


def _tenant(life: Life, tenant: UUID) -> dict[str, Any]:
    with psycopg.connect(life.seeded.admin_dsn) as cx:
        cur = cx.execute("SELECT * FROM cloudops.tenant WHERE id = %s", (tenant,))
        cols = [d.name for d in cur.description or []]
        row = cur.fetchone()
    assert row is not None
    return dict(zip(cols, row, strict=True))


def _changes(life: Life, action: str, tenant: UUID) -> list[dict[str, Any]]:
    return [r[0] for r in _admin_sql(
        life, "SELECT detail FROM access.change_log WHERE action = %s"
              " AND detail ->> 'tenant_id' = %s ORDER BY id", action, str(tenant))]


# --- edit ----------------------------------------------------------------------------


async def test_a_customer_master_renames_a_tenant_and_its_mappings_stay(life: Life) -> None:
    tenant = await _new_tenant(life, "Renamable Ltd", "RENAME", alias="RENAMABLE LIMITED")
    scope = await _scope_of(life, life.mapper)
    await _call(life.app, scope, UPDATE, a=life.mapper, t=tenant, name="Renamed Holdings",
                code="RENAMD")
    row = _tenant(life, tenant)
    assert (row["canonical_name"], row["short_code"]) == ("Renamed Holdings", "RENAMD")
    # Aliases are keyed by tenant id: the old spelling still resolves to it.
    resolved = _admin_sql(life, "SELECT tenant_id FROM cloudops.resolve_tenant_alias("
                                "'servicenow', 'RENAMABLE LIMITED')")
    assert resolved == [(tenant,)]
    assert _changes(life, "update_tenant", tenant) == [{
        "tenant_id": str(tenant),
        "canonical_name": {"from": "Renamable Ltd", "to": "Renamed Holdings"},
        "short_code": {"from": "RENAME", "to": "RENAMD"}}]


async def test_edit_is_refused_without_the_permission_or_outside_scope(life: Life) -> None:
    tenant = await _new_tenant(life, "Edit Refusals Ltd", "EDREF")
    engineer = await _scope_of(life, life.engineer_a)
    await _refused(life.app, engineer, UPDATE, "42501", a=life.engineer_a, t=tenant,
                   name="X", code="EDREF")
    # The customer master, with the tenant outside this transaction's scope.
    await _refused(life.app, _admin_scope(life.seeded.a.id), UPDATE, "CMSCP", a=life.mapper,
                   t=tenant, name="X", code="EDREF")
    # A customer scope runs as cloudops_customer, which cannot call it at all.
    customer = await _scope_of(life, life.cxo_a)
    await _refused(life.app, customer, UPDATE, "42501", a=life.cxo_a, t=life.seeded.a.id,
                   name="X", code="ALPHA")
    scope = await _scope_of(life, life.mapper)
    await _refused(life.app, scope, UPDATE, "23514", a=life.mapper, t=tenant, name=" ",
                   code="EDREF")
    await _refused(life.app, scope, UPDATE, "23514", a=life.mapper, t=tenant,
                   name="Edit Refusals Ltd", code="lower")
    await _refused(life.app, scope, UPDATE, "CMDUP", a=life.mapper, t=tenant,
                   name=life.seeded.b.name, code="EDREF")


async def test_the_short_code_stays_while_a_live_credential_is_named_by_it(life: Life) -> None:
    a = life.seeded.a.id
    scope = await _scope_of(life, life.admin)
    await _refused(life.app, scope, UPDATE, "TKCOD", a=life.admin, t=a,
                   name=life.seeded.a.name, code="ALPHA2")
    # The name alone may change.
    await _call(life.app, scope, UPDATE, a=life.admin, t=a, name="alpha renamed", code="ALPHA")
    await _call(life.app, scope, UPDATE, a=life.admin, t=a, name=life.seeded.a.name,
                code="ALPHA")


# --- offboard ------------------------------------------------------------------------


async def test_offboarding_is_a_platform_administrators_with_the_name_and_a_reason(
    life: Life,
) -> None:
    tenant = await _new_tenant(life, "Offboard Refusals Ltd", "OFREF")
    mapper = await _scope_of(life, life.mapper)
    await _refused(life.app, mapper, OFFBOARD, "42501", a=life.mapper, t=tenant,
                   name="Offboard Refusals Ltd", reason="contract ended")
    finops = await _scope_of(life, life.finops)
    await _refused(life.app, finops, OFFBOARD, "42501", a=life.finops, t=tenant,
                   name="Offboard Refusals Ltd", reason="contract ended")
    customer = await _scope_of(life, life.cxo_a)
    await _refused(life.app, customer, OFFBOARD, "42501", a=life.cxo_a, t=life.seeded.a.id,
                   name=life.seeded.a.name, reason="contract ended")
    # A platform administrator assigned tenant a only: the tenant reads as not found.
    admin_a = await _scope_of(life, life.admin_a)
    await _refused(life.app, admin_a, OFFBOARD, "CMSCP", a=life.admin_a, t=tenant,
                   name="Offboard Refusals Ltd", reason="contract ended")
    admin = await _scope_of(life, life.admin)
    for name, reason, state in (("offboard refusals ltd", "contract ended", "TKCNF"),
                               ("Offboard Refusals Ltd ", "contract ended", "TKCNF"),
                               ("Offboard Refusals Ltd", "   ", "23514"),
                               ("Offboard Refusals Ltd", "x" * 1001, "23514")):
        await _refused(life.app, admin, OFFBOARD, state, a=life.admin, t=tenant, name=name,
                       reason=reason)
    assert _tenant(life, tenant)["status"] == "active"


async def test_the_platform_host_is_never_offboarded(life: Life) -> None:
    host = await _call(life.app, _admin_scope(),
                       "SELECT access.onboard_tenant(:a, 'Host Ltd', 'HOSTX', true)",
                       a=life.admin)
    scope = await _scope_of(life, life.admin)
    await _call(life.app, scope, "SELECT access.set_platform_host(:a, :t)", a=life.admin, t=host)
    await _refused(life.app, scope, OFFBOARD, "TKHST", a=life.admin, t=host, name="Host Ltd",
                   reason="no")
    await _call(life.app, scope, "SELECT access.set_platform_host(:a, NULL)", a=life.admin)


async def test_offboarding_takes_a_tenant_out_of_every_scope_at_once(life: Life) -> None:
    a = life.seeded.a.id
    scope = await _scope_of(life, life.admin)
    assert a in scope.tenant_ids
    purge_after = await _call(life.app, scope, OFFBOARD, a=life.admin, t=a,
                              name=life.seeded.a.name, reason="Contract ended")
    assert timedelta(days=29) < purge_after - datetime.now(UTC) <= timedelta(days=30)
    row = _tenant(life, a)
    assert (row["status"], row["offboarded_from"], row["offboarded_by"], row["offboard_reason"]) \
        == ("offboarded", "active", life.admin, "Contract ended")

    # Every principal: all-tenant staff lose it, named staff are left with nothing,
    # its customers resolve to no access at all.
    for oid in (life.admin, life.finops, life.mapper):
        resolved = await _resolve(life, oid)
        assert resolved is not None
        assert a not in resolved[2]
    assert await _resolve(life, life.engineer_a) == ("staff", "engineer", [])
    assert await _resolve(life, life.admin_a) == ("staff", "platform_admin", [])
    assert await _resolve(life, life.cxo_a) is None

    # Its data, through the scope the API would now resolve: none of it.
    finops = await _scope_of(life, life.finops)
    async with life.app.transaction(finops) as conn:
        for table in ("cloudops.tenant", "cloudops.work_item", "cloudops.cost_fact",
                      "cloudops.resource", "cloudops.knowledge_chunk"):
            column = "id" if table == "cloudops.tenant" else "tenant_id"
            count = await conn.scalar(text(f"SELECT count(*) FROM {table} WHERE {column} = :t"),
                                      {"t": a})
            assert count == 0, table

    # The workers' tenant lists, and name resolution: its names go to quarantine.
    lists = _admin_sql(life, "SELECT cloudops.finops_tenants(),"
                             " ARRAY(SELECT tenant_id FROM cloudops.knowledge_tenants())")
    assert a not in lists[0][0]
    assert a not in lists[0][1]
    assert _admin_sql(life, "SELECT tenant_id, outcome FROM cloudops.resolve_tenant_alias("
                            "'servicenow', %s)", f"{life.seeded.a.name.upper()} LIMITED") \
        == [(None, "unmatched")]

    # Credentials revoked, the portal suspended, everything logged.
    assert _admin_sql(life, "SELECT status, revoked_by FROM cloudops.customer_credential"
                            " WHERE id = %s", life.credential_a) == [("revoked", life.admin)]
    assert _admin_sql(life, "SELECT status FROM cloudops.portal_onboarding WHERE tenant_id = %s",
                      a) == [("suspended",)]
    [logged] = _changes(life, "offboard_tenant", a)
    assert (logged["credentials_revoked"], logged["portal_suspended"], logged["reason"]) \
        == (1, True, "Contract ended")
    # Out of scope now, so it cannot be offboarded twice or edited.
    await _refused(life.app, finops, OFFBOARD, "42501", a=life.finops, t=a,
                   name=life.seeded.a.name, reason="again")
    await _refused(life.app, await _scope_of(life, life.admin), OFFBOARD, "CMSCP",
                   a=life.admin, t=a, name=life.seeded.a.name, reason="again")


async def test_the_offboarded_list_is_a_platform_administrators(life: Life) -> None:
    listed = "SELECT tenant_id, status FROM access.offboarded_tenants(:a)"
    async with life.app.transaction(TenantScope.for_resolution("test:life")) as conn:
        rows = [tuple(r) for r in await conn.execute(text(listed), {"a": life.admin})]
        assert (life.seeded.a.id, "offboarded") in rows
        # The named administrator still holds the assignment: listed for it too.
        named = [tuple(r) for r in await conn.execute(text(listed), {"a": life.admin_a})]
        assert (life.seeded.a.id, "offboarded") in named
    for oid in (life.mapper, life.finops, life.engineer_a):
        await _refused(life.app, TenantScope.for_resolution("test:life"), listed, "42501", a=oid)


# --- restore -------------------------------------------------------------------------


async def test_restore_within_the_retention_brings_it_back_with_credentials_revoked(
    life: Life,
) -> None:
    a = life.seeded.a.id
    empty = TenantScope.for_resolution("test:life")
    for oid in (life.mapper, life.finops):
        await _refused(life.app, empty, RESTORE, "42501", a=oid, t=a)
    await _refused(life.app, empty, RESTORE, "CMSCP", a=life.admin, t=uuid4())
    await _call(life.app, empty, RESTORE, a=life.admin_a, t=a)
    row = _tenant(life, a)
    assert (row["status"], row["offboarded_at"], row["purge_after"]) == ("active", None, None)
    resolved = await _resolve(life, life.engineer_a)
    assert resolved is not None
    assert resolved[2] == [a]
    # Re-onboard: the credential stays revoked, the portal suspended.
    assert _admin_sql(life, "SELECT status FROM cloudops.customer_credential WHERE id = %s",
                      life.credential_a) == [("revoked",)]
    assert await _resolve(life, life.cxo_a) is None
    assert _admin_sql(life, "SELECT tenant_id FROM cloudops.resolve_tenant_alias("
                            "'servicenow', %s)", f"{life.seeded.a.name.upper()} LIMITED") \
        == [(a,)]
    assert [c["status"] for c in _changes(life, "restore_tenant", a)] == ["active"]
    # A live tenant is not restored (reads as not found through the lifecycle policy).
    await _refused(life.app, empty, RESTORE, "CMSCP", a=life.admin, t=a)


async def test_restore_is_refused_once_the_retention_has_ended(life: Life) -> None:
    tenant = await _new_tenant(life, "Late Restore Ltd", "LATER")
    await _call(life.app, await _scope_of(life, life.admin), OFFBOARD, a=life.admin, t=tenant,
                name="Late Restore Ltd", reason="test")
    _backdate(life, tenant)
    await _refused(life.app, TenantScope.for_resolution("test:life"), RESTORE, "TKEXP",
                   a=life.admin, t=tenant)


# --- purge ---------------------------------------------------------------------------


async def test_purge_is_refused_before_the_retention_ends_and_needs_two_confirmations(
    life: Life,
) -> None:
    b = life.seeded.b
    await _call(life.app, await _scope_of(life, life.admin), OFFBOARD, a=life.admin, t=b.id,
                name=b.name, reason="Moved to another provider")
    empty = TenantScope.for_resolution("test:life")
    for oid in (life.mapper, life.finops, life.engineer_a):
        await _refused(life.app, empty, REQUEST, "42501", a=oid, t=b.id, name=b.name,
                       code=b.name.upper())
    await _refused(life.app, empty, REQUEST, "TKRET", a=life.admin, t=b.id, name=b.name,
                   code=b.name.upper())
    _backdate(life, b.id)
    await _refused(life.app, empty, REQUEST, "TKCNF", a=life.admin, t=b.id, name=b.name,
                   code="WRONG")
    await _refused(life.app, empty, REQUEST, "TKCNF", a=life.admin, t=b.id, name="wrong",
                   code=b.name.upper())
    # A live tenant cannot be purged at all.
    await _refused(life.app, empty, REQUEST, "CMSCP", a=life.admin, t=life.seeded.a.id,
                   name=life.seeded.a.name, code="ALPHA")
    assert _tenant(life, b.id)["status"] == "offboarded"


async def test_the_append_only_tables_still_refuse_a_delete_outside_a_purge(life: Life) -> None:
    """The marker alone is nothing: the tenant must be purging, which only a confirmed
    request after the retention makes it."""
    b = life.seeded.b.id
    with psycopg.connect(life.seeded.migrator_dsn) as cx:
        cx.execute("SELECT set_config('app.tenant_ids', %s, true),"
                   " set_config('app.role', 'platform_admin', true),"
                   " set_config('app.account_ids', 'all', true),"
                   " set_config('cloudops.purge_tenant', %s, true)", (str(b), str(b)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cx.execute("DELETE FROM cloudops.evidence WHERE tenant_id = %s", (b,))


async def test_the_lifecycle_moves_only_through_its_functions(life: Life) -> None:
    b = life.seeded.b.id
    with psycopg.connect(life.seeded.ingest_dsn) as cx:
        cx.execute("SELECT set_config('app.tenant_ids', %s, true)", (str(life.seeded.c.id),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cx.execute("UPDATE cloudops.tenant SET status = 'offboarded', offboarded_at = now(),"
                       " offboarded_from = 'active', offboarded_by = gen_random_uuid(),"
                       " offboard_reason = 'x', purge_after = now() WHERE id = %s",
                       (life.seeded.c.id,))
    with psycopg.connect(life.seeded.ingest_dsn) as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute("INSERT INTO cloudops.tenant (canonical_name, short_code, status,"
                   " offboarded_at, offboarded_from, offboarded_by, offboard_reason,"
                   " purge_after) VALUES ('Sneaky', 'SNEAK', 'offboarded', now(),"
                   " 'active', gen_random_uuid(), 'x', now() - interval '40 days')")
    # The worker cannot claim what nobody asked for.
    async with life.ingest.transaction(TenantScope((), Role.INGEST, "test:purge")) as conn:
        claimed = (await conn.execute(text(
            "SELECT * FROM cloudops.claim_tenant_purge()"))).all()
    assert claimed == []
    assert _tenant(life, b)["status"] == "offboarded"


def _counts(life: Life, tenant: UUID) -> dict[str, int]:
    """Rows of ``tenant`` in every table with a tenant_id, plus every other column
    that references a tenant, read from the catalog: a new table is counted here
    without anyone listing it."""
    with psycopg.connect(life.seeded.admin_dsn) as cx:
        tables = [r[0] for r in cx.execute(
            "SELECT n.nspname || '.' || c.relname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'"
            "  AND NOT a.attisdropped"
            " WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p') AND NOT c.relispartition",
            (list(PLATFORM_SCHEMAS),)).fetchall()]
        counts = {t: cx.execute(f"SELECT count(*) FROM {t} WHERE tenant_id = %s",
                                (tenant,)).fetchone()[0] for t in tables}  # type: ignore[index]
        for ref in OTHER_TENANT_REFERENCES:
            table, column = ref.rsplit(".", 1)
            counts[ref] = cx.execute(f"SELECT count(*) FROM {table} WHERE {column} = %s",
                                     (tenant,)).fetchone()[0]  # type: ignore[index]
        for ref in ARRAY_REFERENCES:
            table, column = ref.rsplit(".", 1)
            counts[ref] = cx.execute(f"SELECT count(*) FROM {table} WHERE %s = ANY({column})",
                                     (tenant,)).fetchone()[0]  # type: ignore[index]
    return counts


async def test_purge_removes_every_tenant_row_and_keeps_the_audit_log_and_a_tombstone(
    life: Life,
) -> None:
    c, a = life.seeded.c, life.seeded.a.id
    # Something in every corner: a quarantined name suggesting it, a customer of it
    # only, a released pack's narrative and evidence (append-only), a credential.
    _admin_sql(life, "INSERT INTO staging.tenant_alias_quarantine (source_system, alias,"
                     " suggested_tenant_id) VALUES ('servicenow', 'CHARLIE LTD', %s)", c.id)
    _admin_sql(life, "INSERT INTO staging.cur_assembly (assembly_id, scope_key, invoice_period,"
                     " manifest_ref, report_keys, tenant_ids, account_ids) VALUES ('asm-life',"
                     " 'payer', '2026-10', 'm', '{}', ARRAY[%s, %s]::uuid[],"
                     " ARRAY[%s, %s]::uuid[])",
               c.id, a, c.account_id, life.seeded.a.account_id)
    async with life.app.transaction(_admin_scope(c.id)) as conn:
        await conn.execute(text(
            "SELECT access.onboard_cloud_credential(:a, :t, 'azure', :d, :cl, NULL,"
            " CAST(:ids AS text[]), :e)"),
            {"a": life.admin, "t": c.id, "d": uuid4(), "cl": uuid4(), "ids": [str(uuid4())],
             "e": datetime.now(UTC) + timedelta(days=60)})
        proposal = await conn.scalar(text(
            "SELECT access.propose_alias(:a, 'servicenow', 'CHARLIE HOLDINGS', :t, NULL)"),
            {"a": life.admin, "t": c.id})
        # The two-person rule: another person approves.
        await conn.execute(text("SELECT access.decide_alias(:a, :p, 'approve', NULL)"),
                           {"a": life.mapper, "p": proposal})
    _admin_sql(life, "UPDATE cloudops.report_narrative SET text = 'Reviewed.', edited_by = %s,"
                     " edited_at = now() WHERE tenant_id = %s", life.admin, c.id)
    _admin_sql(life, "UPDATE cloudops.report_pack SET status = 'approved', approved_by = %s,"
                     " approved_at = now() WHERE tenant_id = %s", life.admin,
               c.id)
    before_c = _counts(life, c.id)
    before_a = _counts(life, a)
    assert before_c["audit.action_log"] > 0
    empty = {t for t, n in before_c.items() if n == 0 and t not in KEPT}
    assert {"cloudops.work_item", "cloudops.cost_fact", "cloudops.knowledge_chunk",
            "cloudops.evidence", "cloudops.customer_credential", "cloudops.alias_decision",
            "access.principal_tenant"} <= {t for t, n in before_c.items() if n}, empty

    await _call(life.app, await _scope_of(life, life.admin), OFFBOARD, a=life.admin, t=c.id,
                name=c.name, reason="Customer left; purge requested by contract")
    _backdate(life, c.id)
    empty_scope = TenantScope.for_resolution("test:life")
    purge = await _call(life.app, empty_scope, REQUEST, a=life.admin, t=c.id, name=c.name,
                        code=c.name.upper())
    assert _tenant(life, c.id)["status"] == "purging"
    # Purging: no restore, no second request.
    await _refused(life.app, empty_scope, RESTORE, "TKSTA", a=life.admin, t=c.id)
    await _refused(life.app, empty_scope, REQUEST, "TKSTA", a=life.admin, t=c.id, name=c.name,
                   code=c.name.upper())

    worker = TenantScope((), Role.INGEST, "test:purge")
    async with life.ingest.transaction(worker) as conn:
        claimed = (await conn.execute(text(
            "SELECT id, tenant_id FROM cloudops.claim_tenant_purge()"))).one()
    assert tuple(claimed) == (purge, c.id)
    async with life.ingest.transaction(worker) as conn:
        removed = await conn.scalar(text(
            "SELECT cloudops.purge_tenant(:p, :t, 3, 'not_configured')"),
            {"p": purge, "t": c.id})

    after_c = _counts(life, c.id)
    survivors = {t: n for t, n in after_c.items() if n}
    assert survivors == {"audit.action_log": before_c["audit.action_log"],
                         "audit.tenant_purge": 1}
    assert _counts(life, a) == before_a  # nobody else's rows moved
    assert _admin_sql(life, "SELECT tenant_ids, account_ids FROM staging.cur_assembly"
                            " WHERE assembly_id = 'asm-life'") \
        == [([a], [life.seeded.a.account_id])]
    assert removed["cloudops.work_item"] == before_c["cloudops.work_item"]
    assert removed["access.principal"] == 1  # its portal administrator, a customer of it only
    assert _admin_sql(life, "SELECT count(*) FROM access.principal WHERE oid = %s",
                      life.cxo_c) == [(0,)]
    # The tombstone: the tenant existed, and when it went.
    row = _tenant(life, c.id)
    assert (row["status"], row["canonical_name"], row["offboarded_by"]) \
        == ("purged", c.name, life.admin)
    assert row["purged_at"] is not None
    assert row["contract_start"] is None
    assert _admin_sql(life, "SELECT status, blobs_deleted, telemetry, requested_by"
                            " FROM audit.tenant_purge WHERE id = %s", purge) \
        == [("succeeded", 3, "not_configured", life.admin)]
    # Still in the offboarded list, as purged.
    async with life.app.transaction(empty_scope) as conn:
        listed = {tuple(r) for r in await conn.execute(text(
            "SELECT tenant_id, status, purge_status FROM access.offboarded_tenants(:a)"),
            {"a": life.admin})}
    assert (c.id, "purged", "succeeded") in listed
    # A tombstone is final, and its name may be used again.
    await _refused(life.app, empty_scope, RESTORE, "CMSCP", a=life.admin, t=c.id)
    reused = await _new_tenant(life, c.name, c.name.upper())
    assert reused != c.id


async def test_a_failed_purge_can_be_asked_for_again(life: Life) -> None:
    tenant = await _new_tenant(life, "Failing Purge Ltd", "FAILP")
    await _call(life.app, await _scope_of(life, life.admin), OFFBOARD, a=life.admin, t=tenant,
                name="Failing Purge Ltd", reason="test")
    _backdate(life, tenant)
    empty = TenantScope.for_resolution("test:life")
    await _call(life.app, empty, REQUEST, a=life.admin, t=tenant, name="Failing Purge Ltd",
                code="FAILP")
    worker = TenantScope((), Role.INGEST, "test:purge")
    async with life.ingest.transaction(worker) as conn:
        first = (await conn.execute(text("SELECT id FROM cloudops.claim_tenant_purge()"))).one()
        await conn.execute(text("SELECT cloudops.fail_tenant_purge(:p, 'blob_delete_failed')"),
                           {"p": first[0]})
    assert _tenant(life, tenant)["status"] == "purging"
    second = await _call(life.app, empty, REQUEST, a=life.admin, t=tenant,
                         name="Failing Purge Ltd", code="FAILP")
    assert second != first[0]
    # The record of each attempt is kept and moves only forward.
    with psycopg.connect(life.seeded.admin_dsn) as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute("UPDATE audit.tenant_purge SET status = 'requested' WHERE id = %s",
                   (first[0],))
    with psycopg.connect(life.seeded.admin_dsn) as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute("DELETE FROM audit.tenant_purge WHERE id = %s", (first[0],))


async def test_purge_records_are_no_customers_and_no_other_tenants(life: Life) -> None:
    """audit.tenant_purge is tenant data: FORCE RLS, tenant_isolation, no grant to the
    application at all (it reads through the function)."""
    with psycopg.connect(life.seeded.admin_dsn) as cx:
        grants = cx.execute(
            "SELECT r FROM unnest(ARRAY['cloudops_app', 'cloudops_customer', 'cloudops_ingest']) r"
            " WHERE has_table_privilege(r, 'audit.tenant_purge', 'SELECT,INSERT,UPDATE,DELETE')"
        ).fetchall()
    assert grants == []


# --- catalog -------------------------------------------------------------------------


def test_every_tenant_reference_is_one_the_purge_clears(life: Life) -> None:
    """A table that holds a tenant id the purge cannot find fails here: the purge
    deletes by a ``tenant_id`` column and clears nullable foreign keys to the tenant.
    A uuid column named for a tenant that is neither, or a foreign key to the tenant
    that cannot be cleared, must be handled (and listed) before it ships."""
    with psycopg.connect(life.seeded.admin_dsn) as cx:
        named = {r[0] for r in cx.execute(
            "SELECT n.nspname || '.' || c.relname || '.' || a.attname FROM pg_attribute a"
            " JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
            "   AND NOT a.attisdropped AND a.attnum > 0 AND a.attname LIKE '%%tenant%%'"
            "   AND a.atttypid IN ('uuid'::regtype, 'uuid[]'::regtype)"
            "   AND a.attname <> 'tenant_id' AND c.relname <> 'tenant'",
            (list(PLATFORM_SCHEMAS),)).fetchall()}
        foreign = cx.execute(
            "SELECT n.nspname || '.' || r.relname || '.' || a.attname, a.attnotnull"
            " FROM pg_constraint k JOIN pg_class r ON r.oid = k.conrelid"
            " JOIN pg_namespace n ON n.oid = r.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY(k.conkey)"
            " WHERE k.contype = 'f' AND k.confrelid = 'cloudops.tenant'::regclass"
            "   AND NOT r.relispartition AND a.attname <> 'tenant_id'").fetchall()
        outside = cx.execute(
            "SELECT n.nspname || '.' || c.relname FROM pg_attribute a"
            " JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE a.attname = 'tenant_id' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
            "   AND n.nspname <> ALL(%s) AND n.nspname NOT IN ('pg_catalog', 'information_schema')",
            (list(PLATFORM_SCHEMAS),)).fetchall()
        # pg_partman's templates carry the parents' columns and never a row.
        templates = [t for (t,) in outside if t.startswith("partman.template_")]
        filled = [t for t in templates
                  if cx.execute(f"SELECT count(*) FROM {t}").fetchone() != (0,)]
    assert named == OTHER_TENANT_REFERENCES | ARRAY_REFERENCES
    assert {f for f, _ in foreign} == OTHER_TENANT_REFERENCES
    assert all(not notnull for _, notnull in foreign)
    assert [t for (t,) in outside if t not in templates] == []
    assert filled == []


# --- connectors and the purge worker -------------------------------------------------


async def _with_credential(life: Life, name: str, code: str, alias: str) -> tuple[UUID, UUID]:
    tenant = await _new_tenant(life, name, code, alias=alias)
    credential = await _call(
        life.app, await _scope_of(life, life.admin),
        "SELECT access.onboard_cloud_credential(:a, :t, 'azure', :d, :c, NULL,"
        " CAST(:ids AS text[]), :e)",
        a=life.admin, t=tenant, d=uuid4(), c=uuid4(), ids=[str(uuid4())],
        e=datetime.now(UTC) + timedelta(days=60))
    # As a passed check leaves it (the worker's finish_credential_check).
    _admin_sql(life, "UPDATE cloudops.customer_credential SET status = 'active',"
                     " validated_at = now() WHERE id = %s", credential)
    return tenant, UUID(str(credential))


async def test_connectors_skip_an_offboarded_tenant(life: Life) -> None:
    from cloudops_connectors.core import state
    from cloudops_connectors.customer import credentials

    tenant, credential = await _with_credential(life, "Skip Ltd", "SKIPT", "SKIP LIMITED")
    worker = TenantScope((), Role.INGEST, "connector:test")

    async def seen() -> tuple[set[UUID], state.Resolution]:
        async with life.ingest.transaction(worker) as conn:
            active = {c.id for c in await credentials.active(conn, "azure")}
            resolved = await state.resolve_aliases(conn, "servicenow", ["SKIP LIMITED"])
        return active, resolved["SKIP LIMITED"]

    active, resolved = await seen()
    assert credential in active
    assert resolved.tenant_id == tenant
    await _call(life.app, await _scope_of(life, life.admin), OFFBOARD, a=life.admin, t=tenant,
                name="Skip Ltd", reason="test")
    # The customer worker drops the credential's worker and schedules on its next pass;
    # every connector's records for the name go to quarantine.
    active, resolved = await seen()
    assert credential not in active
    assert (resolved.tenant_id, resolved.outcome) == (None, "unmatched")


class _Blob:
    def __init__(self, name: str, metadata: dict[str, str]) -> None:
        self.name, self.metadata = name, metadata


class _Container:
    """What BlobBronzePages uses of a ContainerClient."""

    def __init__(self, pages: dict[str, dict[str, str]], *, fail: bool = False) -> None:
        self.pages = dict(pages)
        self.fail = fail

    async def _list(self) -> AsyncIterator[_Blob]:
        for name, metadata in sorted(self.pages.items()):
            yield _Blob(name, metadata)

    def list_blobs(self, include: list[str]) -> AsyncIterator[_Blob]:
        assert include == ["metadata"]
        return self._list()

    async def delete_blob(self, name: str, delete_snapshots: str) -> None:
        if self.fail:
            raise OSError("storage unavailable")
        assert delete_snapshots == "include"
        del self.pages[name]


async def _due(life: Life, name: str, code: str) -> tuple[UUID, UUID]:
    tenant, credential = await _with_credential(life, name, code, f"{name.upper()} ALIAS")
    await _call(life.app, await _scope_of(life, life.admin), OFFBOARD, a=life.admin, t=tenant,
                name=name, reason="test")
    _backdate(life, tenant)
    await _call(life.app, TenantScope.for_resolution("test:life"), REQUEST, a=life.admin,
                t=tenant, name=name, code=code)
    return tenant, credential


async def test_the_worker_deletes_the_tenants_credential_pages_and_nothing_else(
    life: Life,
) -> None:
    from cloudops_connectors.lifecycle.purge import BlobBronzePages, TenantPurge

    tenant, credential = await _due(life, "Worker Purge Ltd", "WPURGE")
    other = uuid4()
    container = _Container({
        "azure/resource/2026/10/30/run-00001.json":
            {"instance": f"https://management.azure.com#credential={credential}"},
        "azure/cost_daily/2026/10/30/run-00001.json":
            {"instance": f"https://management.azure.com#credential={credential}"},
        "azure/resource/2026/10/30/other-00001.json":
            {"instance": f"https://management.azure.com#credential={other}"},
        "azure/cost_daily/2026/10/30/platform-00001.json":
            {"instance": "https://management.azure.com"},
        "azure/resource/2026/10/30/bare-00001.json": {},
    })
    # The earlier test's second request waits too: the worker takes both, oldest first.
    reports = await TenantPurge(life.ingest, [BlobBronzePages(container)]).run()  # type: ignore[arg-type]
    [report] = [r for r in reports if r.tenant_id == tenant]
    assert (report.status, report.blobs_deleted, report.telemetry) \
        == ("succeeded", 2, "not_configured")
    assert sorted(container.pages) == ["azure/cost_daily/2026/10/30/platform-00001.json",
                                       "azure/resource/2026/10/30/bare-00001.json",
                                       "azure/resource/2026/10/30/other-00001.json"]
    assert report.row_counts["cloudops.customer_credential"] == 1
    assert _tenant(life, tenant)["status"] == "purged"
    assert await TenantPurge(life.ingest, []).run() == []  # nothing else waits


async def test_a_failing_step_fails_the_purge_and_the_tenant_stays_purging(life: Life) -> None:
    from cloudops_connectors.lifecycle.purge import BlobBronzePages, TenantPurge

    tenant, credential = await _due(life, "Failing Bronze Ltd", "FBRONZE")
    container = _Container({"azure/resource/2026/10/30/run-00001.json":
                            {"instance": f"x#credential={credential}"}}, fail=True)
    [report] = await TenantPurge(life.ingest, [BlobBronzePages(container)]).run()  # type: ignore[arg-type]
    assert (report.status, report.outcome) == ("failed", "bronze_failed")
    assert _tenant(life, tenant)["status"] == "purging"
    assert _admin_sql(life, "SELECT status, outcome FROM audit.tenant_purge WHERE tenant_id = %s",
                      tenant) == [("failed", "bronze_failed")]
    # Nothing of the tenant's was deleted from the database.
    assert _counts(life, tenant)["cloudops.customer_credential"] == 1


def test_an_instance_names_a_credential_only_after_its_mark() -> None:
    from cloudops_connectors.lifecycle.purge import credential_of

    assert credential_of("https://management.azure.com#credential=abc") == "abc"
    assert credential_of("https://management.azure.com") is None
    assert credential_of("#credential=") is None
    assert credential_of("") is None
