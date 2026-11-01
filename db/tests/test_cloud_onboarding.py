"""Cloud onboarding at the database level (migration 0025, ADR 0027, ADR 0003):
the credential inventory and its checks are tenant data no other tenant and no
customer can read; only a platform administrator records one, through the
functions; the 90-day expiry and one-credential-one-tenant hold in the table; the
worker claims a check before it knows the tenant and finishes it under that
tenant's scope; a provider id has one owner, internal or not (ADR 0031); and only
a platform administrator marks a tenant internal or the platform's host."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Principals, Seeded
from .support import raw_scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

ONBOARD = ("SELECT access.onboard_cloud_credential(:a, :t, :p, :d, :c, :k,"
           " CAST(:ids AS text[]), :e)")


def _admin(*tenants: UUID) -> TenantScope:
    return TenantScope(list(tenants), Role.PLATFORM_ADMIN, "test:cloud-onboarding")


def _ingest(*tenants: UUID) -> TenantScope:
    return TenantScope(list(tenants), Role.INGEST, "connector:test")


async def _call(db: Database, s: TenantScope, sql: str, **params: Any) -> Any:
    async with db.transaction(s) as conn:
        return (await conn.execute(text(sql), params)).all()


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def _sub() -> str:
    return str(uuid4())


async def _azure(db: Database, actor: UUID, tenant: UUID, *subs: str,
                 client: UUID | None = None, days: int = 60,
                 scope: tuple[UUID, ...] = ()) -> UUID:
    rows = await _call(db, _admin(*(scope or (tenant,))), ONBOARD, a=actor, t=tenant,
                       p="azure", d=uuid4(), c=client or uuid4(), k=None,
                       ids=list(subs or (_sub(),)),
                       e=datetime.now(UTC) + timedelta(days=days))
    return UUID(str(rows[0][0]))


@pytest.fixture
async def fresh(seeded: Seeded) -> AsyncIterator[list[UUID]]:
    """Tenants a test creates (as superuser), removed afterwards with their rows."""
    made: list[UUID] = []
    yield made
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for t in made:
            cx.execute("ALTER TABLE cloudops.customer_credential DISABLE TRIGGER"
                       " customer_credential_no_delete")
            cx.execute("ALTER TABLE cloudops.credential_check DISABLE TRIGGER"
                       " credential_check_no_delete")
            try:
                cx.execute("DELETE FROM cloudops.cloud_account WHERE tenant_id = %s", (t,))
                cx.execute("UPDATE cloudops.tenant SET hosts_platform = false, internal = false"
                           " WHERE id = %s", (t,))
                cx.execute("DELETE FROM cloudops.tenant WHERE id = %s", (t,))
            finally:
                cx.execute("ALTER TABLE cloudops.customer_credential ENABLE TRIGGER"
                           " customer_credential_no_delete")
                cx.execute("ALTER TABLE cloudops.credential_check ENABLE TRIGGER"
                           " credential_check_no_delete")


def _tenant(seeded: Seeded, made: list[UUID], code: str, *, internal: bool = False) -> UUID:
    t = uuid4()
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code, internal)"
                   " VALUES (%s, %s, %s, %s)", (t, f"Tenant {code} {t}", code, internal))
    made.append(t)
    return t


# --- the tables -----------------------------------------------------------------


@pytest.mark.parametrize("table", ["customer_credential", "credential_check"])
def test_both_tables_force_row_level_security(seeded: Seeded, table: str) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        row = cx.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                         " WHERE oid = %s::regclass", (f"cloudops.{table}",)).fetchone()
    assert row == (True, True)


async def test_another_tenants_credentials_and_checks_are_invisible(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
) -> None:
    a, b = seeded.a.id, seeded.b.id
    credential = await _azure(app_db, principals.admin, a, scope=(a, b))
    for db, scope in ((app_db, _admin(b)), (ingest_db, _ingest(b))):
        for table in ("customer_credential", "credential_check"):
            assert await _call(db, scope, f"SELECT id FROM cloudops.{table}"
                               " WHERE tenant_id = :a", a=a) == []
    # Cross-tenant attempts on the functions read as "not found".
    for sql in ("SELECT access.request_credential_check(:a, :c, NULL)",
                "SELECT access.revoke_cloud_credential(:a, :c)"):
        with pytest.raises(Exception, match="not found") as caught:
            await _call(app_db, _admin(b), sql, a=principals.admin, c=credential)
        assert _sqlstate(caught.value) == "P0002"
    assert await _call(app_db, _admin(a), "SELECT status FROM cloudops.customer_credential"
                       " WHERE id = :c", c=credential) == [("pending",)]


def test_a_customer_scope_reads_no_credential_even_in_its_own_tenant(seeded: Seeded) -> None:
    for table in ("customer_credential", "credential_check"):
        with raw_scope(seeded.app_dsn, str(seeded.a.id), "customer") as cx:
            assert cx.execute(f"SELECT count(*) FROM cloudops.{table}").fetchone() == (0,)


@pytest.mark.parametrize("sql", [
    "INSERT INTO cloudops.customer_credential (tenant_id, provider, code, access_key_id,"
    " account_ids, secret_names, owner_oid, expires_at) VALUES (%(t)s, 'aws', 'x',"
    " 'AKIAAAAAAAAAAAAAAAAA', '{123456789012}', '{n}', gen_random_uuid(),"
    " now() + interval '1 day')",
    "UPDATE cloudops.customer_credential SET status = 'active'",
    "UPDATE cloudops.credential_check SET status = 'passed'",
])
def test_the_application_writes_only_through_the_functions(seeded: Seeded, sql: str) -> None:
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "platform_admin") as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute(sql, {"t": seeded.a.id})


@pytest.mark.parametrize(("login", "sql"), [
    ("app", "SELECT * FROM cloudops.claim_credential_check('azure')"),
    ("app", "SELECT * FROM cloudops.active_credentials('azure')"),
    ("app", "SELECT cloudops.finish_credential_check(gen_random_uuid(), 'passed', 'x', '{}')"),
    ("ingest", "SELECT access.revoke_cloud_credential(gen_random_uuid(), gen_random_uuid())"),
])
def test_each_side_calls_only_its_own_functions(seeded: Seeded, login: str, sql: str) -> None:
    dsn = seeded.app_dsn if login == "app" else seeded.ingest_dsn
    with raw_scope(dsn, str(seeded.a.id), "platform_admin" if login == "app" else "ingest") \
            as cx, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute(sql)


# --- recording one --------------------------------------------------------------


async def test_onboarding_records_identifiers_and_secret_names_and_queues_a_check(
    app_db: Database, seeded: Seeded, principals: Principals, fresh: list[UUID],
) -> None:
    t = _tenant(seeded, fresh, "AZSPONSOR")
    sub = _sub().upper()  # stored as Azure lists it, lower case
    credential = await _azure(app_db, principals.admin, t, sub, scope=(t,))
    rows = await _call(app_db, _admin(t),
                       "SELECT provider, credential_source, account_ids, secret_names, owner_oid,"
                       " status FROM cloudops.customer_credential WHERE id = :c", c=credential)
    assert rows == [("azure", "stored_key", [sub.lower()], ["customer-azsponsor-azure-secret"],
                     principals.admin, "pending")]
    checks = await _call(app_db, _admin(t), "SELECT reason, status, requested_by"
                         " FROM cloudops.credential_check WHERE credential_id = :c", c=credential)
    assert checks == [("onboard", "requested", principals.admin)]
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT count(*) FROM access.change_log WHERE action ="
                          " 'onboard_cloud_credential' AND detail->>'credential_id' = %s",
                          (str(credential),)).fetchone() == (1,)


async def test_aws_names_the_key_id_and_the_secret_key(
    app_db: Database, seeded: Seeded, principals: Principals, fresh: list[UUID],
) -> None:
    t = _tenant(seeded, fresh, "AWSLAB")
    rows = await _call(app_db, _admin(t), ONBOARD, a=principals.admin, t=t, p="aws", d=None,
                       c=None, k="AKIAIOSFODNN7EXAMPLE", ids=["123456789012"],
                       e=datetime.now(UTC) + timedelta(days=30))
    names = await _call(app_db, _admin(t), "SELECT secret_names FROM cloudops.customer_credential"
                        " WHERE id = :c", c=rows[0][0])
    assert names == [(["customer-awslab-aws-access-key-id",
                       "customer-awslab-aws-secret-access-key"],)]


@pytest.mark.parametrize("days", [91, 365, -1, 0])
async def test_the_secret_expires_within_ninety_days(
    app_db: Database, seeded: Seeded, principals: Principals, days: int,
) -> None:
    with pytest.raises(Exception, match="90 days") as caught:
        await _azure(app_db, principals.admin, seeded.a.id, days=days)
    assert _sqlstate(caught.value) == "23514"


def test_the_table_itself_refuses_a_longer_expiry(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx, pytest.raises(psycopg.errors.CheckViolation):
        cx.execute(
            "INSERT INTO cloudops.customer_credential (tenant_id, provider, code, access_key_id,"
            " account_ids, secret_names, owner_oid, expires_at) VALUES (%s, 'aws', 'x',"
            " 'AKIAAAAAAAAAAAAAAAAA', '{123456789012}', '{n}', gen_random_uuid(),"
            " now() + interval '91 days')", (seeded.a.id,))


@pytest.mark.parametrize("value", ["delegated", "anything"])
def test_only_a_stored_key_can_be_recorded(seeded: Seeded, value: str) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx, pytest.raises(psycopg.errors.CheckViolation):
        cx.execute(
            "INSERT INTO cloudops.customer_credential (tenant_id, provider, credential_source,"
            " code, access_key_id, account_ids, secret_names, owner_oid, expires_at)"
            " VALUES (%s, 'aws', %s, 'x', 'AKIAAAAAAAAAAAAAAAAA', '{123456789012}', '{n}',"
            " gen_random_uuid(), now() + interval '1 day')", (seeded.a.id, value))


async def test_one_live_credential_per_tenant_and_one_tenant_per_app_registration(
    app_db: Database, seeded: Seeded, principals: Principals, fresh: list[UUID],
) -> None:
    t1, t2 = _tenant(seeded, fresh, "ONEA"), _tenant(seeded, fresh, "ONEB")
    client = uuid4()
    first = await _azure(app_db, principals.admin, t1, client=client, scope=(t1, t2))
    for tenant, c in ((t1, None), (t2, client)):  # same tenant again; same app, other tenant
        with pytest.raises(Exception, match="already has a live credential") as caught:
            await _azure(app_db, principals.admin, tenant, client=c, scope=(t1, t2))
        assert _sqlstate(caught.value) == "CMDUP"
    # Revoked, the app registration may be bound again (a new record, a new check).
    await _call(app_db, _admin(t1), "SELECT access.revoke_cloud_credential(:a, :c)",
                a=principals.admin, c=first)
    await _azure(app_db, principals.admin, t2, client=client, scope=(t1, t2))


async def test_only_a_platform_administrator_records_one(
    app_db: Database, seeded: Seeded, principals: Principals,
) -> None:
    for actor in (principals.finops_all, principals.engineer_a, uuid4()):
        with pytest.raises(Exception, match="not permitted") as caught:
            await _azure(app_db, actor, seeded.a.id)
        assert _sqlstate(caught.value) == "42501"


async def test_a_tenant_outside_scope_reads_as_not_found(
    app_db: Database, seeded: Seeded, principals: Principals,
) -> None:
    with pytest.raises(Exception, match="tenant not found") as caught:
        await _azure(app_db, principals.admin, seeded.a.id, scope=(seeded.b.id,))
    assert _sqlstate(caught.value) == "CMSCP"


# --- the worker's side ----------------------------------------------------------


async def _claim(ingest_db: Database, credential: UUID, provider: str = "azure") -> tuple[Any, ...]:
    for _ in range(50):
        rows = await _call(ingest_db, _ingest(), "SELECT id, tenant_id, credential_id, reason"
                           " FROM cloudops.claim_credential_check(:p)", p=provider)
        if not rows:
            break
        if rows[0][2] == credential:
            return tuple(rows[0])
    raise AssertionError("the check was never claimed")


async def test_the_worker_claims_without_a_scope_and_finishes_under_the_tenants(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
    fresh: list[UUID],
) -> None:
    t = _tenant(seeded, fresh, "WORKER")
    credential = await _azure(app_db, principals.admin, t, scope=(t,))
    # Unscoped, the worker sees no row; only what the functions hand it.
    assert await _call(ingest_db, _ingest(), "SELECT id FROM cloudops.customer_credential") == []
    check, tenant, _, reason = await _claim(ingest_db, credential)
    assert (tenant, reason) == (t, "onboard")
    with pytest.raises(Exception, match="no running check"):
        await _call(ingest_db, _ingest(seeded.b.id),
                    "SELECT cloudops.finish_credential_check(:id, 'passed', 'read_only', '{}')",
                    id=check)
    assert await _call(ingest_db, _ingest(), "SELECT id FROM cloudops.active_credentials('azure')"
                       " WHERE id = :c", c=credential) == []
    await _call(ingest_db, _ingest(t),
                "SELECT cloudops.finish_credential_check(:id, 'passed', 'read_only', '{}')",
                id=check)
    assert await _call(ingest_db, _ingest(), "SELECT code FROM cloudops.active_credentials("
                       "'azure') WHERE id = :c", c=credential) == [("worker",)]
    # Revoked: gone from what the worker may use, at once; kept in the inventory.
    await _call(app_db, _admin(t), "SELECT access.revoke_cloud_credential(:a, :c)",
                a=principals.admin, c=credential)
    assert await _call(ingest_db, _ingest(), "SELECT id FROM cloudops.active_credentials('azure')"
                       " WHERE id = :c", c=credential) == []
    with pytest.raises(Exception, match="not checked again"):
        await _call(app_db, _admin(t), "SELECT access.request_credential_check(:a, :c, NULL)",
                    a=principals.admin, c=credential)


async def test_an_expired_credential_is_not_listed_for_use(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
    fresh: list[UUID],
) -> None:
    t = _tenant(seeded, fresh, "EXPIRED")
    credential = await _azure(app_db, principals.admin, t, scope=(t,))
    check = (await _claim(ingest_db, credential))[0]
    await _call(ingest_db, _ingest(t), "SELECT cloudops.finish_credential_check(:id, 'passed',"
                " 'read_only', '{}')", id=check)
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.customer_credential SET secret_set_at = now() - interval"
                   " '80 days', expires_at = now() - interval '1 minute' WHERE id = %s",
                   (credential,))
    assert await _call(ingest_db, _ingest(), "SELECT id FROM cloudops.active_credentials('azure')"
                       " WHERE id = :c", c=credential) == []


async def test_a_rotation_restarts_the_expiry_and_queues_a_check(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
    fresh: list[UUID],
) -> None:
    t = _tenant(seeded, fresh, "ROTATE")
    credential = await _azure(app_db, principals.admin, t, days=10, scope=(t,))
    with pytest.raises(Exception, match="already waiting"):  # the onboarding check is
        await _call(app_db, _admin(t), "SELECT access.request_credential_check(:a, :c, NULL)",
                    a=principals.admin, c=credential)
    check = (await _claim(ingest_db, credential))[0]
    await _call(ingest_db, _ingest(t), "SELECT cloudops.finish_credential_check(:id, 'failed',"
                " 'secret_not_in_key_vault', '{}')", id=check)
    new_expiry = datetime.now(UTC) + timedelta(days=89)
    await _call(app_db, _admin(t), "SELECT access.request_credential_check(:a, :c, :e)",
                a=principals.admin, c=credential, e=new_expiry)
    rows = await _call(app_db, _admin(t), "SELECT k.expires_at, c.reason FROM"
                       " cloudops.customer_credential k JOIN cloudops.credential_check c"
                       " ON c.credential_id = k.id WHERE k.id = :c AND c.status = 'requested'",
                       c=credential)
    assert [(abs(r[0] - new_expiry) < timedelta(seconds=1), r[1]) for r in rows] == [
        (True, "rotation")]


async def test_the_inventory_and_its_checks_are_never_deleted(
    app_db: Database, seeded: Seeded, principals: Principals, fresh: list[UUID],
) -> None:
    t = _tenant(seeded, fresh, "KEEP")
    credential = await _azure(app_db, principals.admin, t, scope=(t,))
    with psycopg.connect(seeded.admin_dsn) as cx:
        for sql in ("DELETE FROM cloudops.customer_credential WHERE id = %s",
                    "DELETE FROM cloudops.credential_check WHERE credential_id = %s"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), cx.transaction():
                cx.execute(sql, (credential,))


async def test_a_finished_check_is_never_rewritten(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
    fresh: list[UUID],
) -> None:
    t = _tenant(seeded, fresh, "FORWARD")
    credential = await _azure(app_db, principals.admin, t, scope=(t,))
    check = (await _claim(ingest_db, credential))[0]
    await _call(ingest_db, _ingest(t), "SELECT cloudops.finish_credential_check(:id, 'refused',"
                " 'role_not_allowed', '{}')", id=check)
    with psycopg.connect(seeded.admin_dsn) as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute("UPDATE cloudops.credential_check SET status = 'passed' WHERE id = %s",
                   (check,))
    assert await _call(app_db, _admin(t), "SELECT status FROM cloudops.customer_credential"
                       " WHERE id = :c", c=credential) == [("refused",)]


# --- one owner per account (ADR 0031) -------------------------------------------


_ACCOUNT = ("INSERT INTO cloudops.cloud_account (tenant_id, provider, native_id, display_name)"
            " VALUES (:t, 'azure', :n, 'x') RETURNING id")


async def test_two_customers_never_hold_the_same_account(
    ingest_db: Database, seeded: Seeded,
) -> None:
    a, b = seeded.a, seeded.b
    with pytest.raises(Exception, match="cloud_account_provider_native_key") as caught:
        await _call(ingest_db, _ingest(b.id), _ACCOUNT, t=b.id, n=a.account_native_id)
    assert _sqlstate(caught.value) == "23505"
    assert "alpha" not in str(caught.value)
    assert str(a.id) not in str(caught.value)


@pytest.mark.parametrize("first_internal", [True, False])
async def test_an_internal_tenant_shares_an_account_with_nobody(
    ingest_db: Database, seeded: Seeded, fresh: list[UUID], first_internal: bool,
) -> None:
    """PR 62 let the internal tenant and one customer hold one subscription. ADR 0031
    withdrew that: internal or not, an account has one owner."""
    first = _tenant(seeded, fresh, "OWN1", internal=first_internal)
    second = _tenant(seeded, fresh, "OWN2", internal=True)
    sub = _sub()
    await _call(ingest_db, _ingest(first), _ACCOUNT, t=first, n=sub)
    with pytest.raises(Exception, match="cloud_account_provider_native_key"):
        await _call(ingest_db, _ingest(second), _ACCOUNT, t=second, n=sub)
    # Its own row again is an upsert, not a second owner.
    assert len(await _call(ingest_db, _ingest(first),
                "INSERT INTO cloudops.cloud_account (tenant_id, provider, native_id, display_name)"
                " VALUES (:t, 'azure', :n, 'x') ON CONFLICT (tenant_id, provider, native_id)"
                " DO UPDATE SET display_name = 'y' RETURNING id", t=first, n=sub)) == 1


def test_the_sharing_machinery_is_gone(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT tgname FROM pg_trigger WHERE tgname IN"
                          " ('cloud_account_sharing', 'tenant_internal_sharing')").fetchall() == []
        assert cx.execute("SELECT indexname FROM pg_indexes"
                          " WHERE indexname = 'tenant_one_internal_idx'").fetchall() == []
        assert cx.execute("SELECT polname FROM pg_policy WHERE polname IN"
                          " ('account_sharing_read', 'account_sharing_mark',"
                          "  'internal_tenant_read')").fetchall() == []


# --- internal tenants and the platform host (ADR 0031) ------------------------------


KIND = "SELECT access.set_tenant_internal(:a, :t, :i)"
HOST = "SELECT access.set_platform_host(:a, :t)"


def _kinds(seeded: Seeded, *tenants: UUID) -> list[tuple[UUID, bool, bool]]:
    with psycopg.connect(seeded.admin_dsn) as cx:
        return [(r[0], r[1], r[2]) for r in cx.execute(
            "SELECT id, internal, hosts_platform FROM cloudops.tenant WHERE id = ANY(%s)"
            " ORDER BY short_code", (list(tenants),)).fetchall()]


async def test_many_tenants_may_be_internal(
    app_db: Database, seeded: Seeded, principals: Principals, fresh: list[UUID],
) -> None:
    one, two = _tenant(seeded, fresh, "INTA"), _tenant(seeded, fresh, "INTB")
    for t in (one, two):
        await _call(app_db, _admin(t), KIND, a=principals.admin, t=t, i=True)
    assert _kinds(seeded, one, two) == [(one, True, False), (two, True, False)]
    await _call(app_db, _admin(two), KIND, a=principals.admin, t=two, i=False)
    assert _kinds(seeded, one, two) == [(one, True, False), (two, False, False)]


async def test_one_internal_tenant_hosts_the_platform(
    app_db: Database, seeded: Seeded, principals: Principals, fresh: list[UUID],
) -> None:
    first = _tenant(seeded, fresh, "HOSTA", internal=True)
    second = _tenant(seeded, fresh, "HOSTB", internal=True)
    customer = _tenant(seeded, fresh, "HOSTC")
    both = _admin(first, second, customer)
    await _call(app_db, both, HOST, a=principals.admin, t=first)
    assert await _call(app_db, both, "SELECT cloudops.platform_tenant()") == [(first,)]
    # Moving the host takes the flag from the old one.
    await _call(app_db, both, HOST, a=principals.admin, t=second)
    assert _kinds(seeded, first, second) == [(first, True, False), (second, True, True)]
    assert await _call(app_db, both, "SELECT cloudops.internal_tenant()") == [(second,)]
    # A customer cannot host; the host cannot become a customer while it hosts.
    with pytest.raises(Exception, match="only an internal tenant") as caught:
        await _call(app_db, both, HOST, a=principals.admin, t=customer)
    assert _sqlstate(caught.value) == "TKHST"
    with pytest.raises(Exception, match="move the host first"):
        await _call(app_db, both, KIND, a=principals.admin, t=second, i=False)
    # And the table holds it without the functions.
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx, \
            pytest.raises(psycopg.errors.UniqueViolation):
        cx.execute("UPDATE cloudops.tenant SET hosts_platform = true WHERE id = %s", (first,))
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx, \
            pytest.raises(psycopg.errors.CheckViolation):
        cx.execute("UPDATE cloudops.tenant SET internal = false WHERE id = %s", (second,))
    await _call(app_db, both, HOST, a=principals.admin, t=None)
    assert await _call(app_db, both, "SELECT cloudops.platform_tenant()") == [(None,)]


async def test_only_a_platform_administrator_sets_a_tenants_kind_in_its_scope(
    app_db: Database, seeded: Seeded, principals: Principals, fresh: list[UUID],
) -> None:
    t = _tenant(seeded, fresh, "KINDX")
    for sql in (KIND, HOST):
        params: dict[str, Any] = {"a": principals.finops_all, "t": t, "i": True}
        with pytest.raises(Exception, match="not permitted") as caught:
            await _call(app_db, _admin(t), sql, **params)
        assert _sqlstate(caught.value) == "42501"
        # A tenant outside the caller's scope reads as not found.
        params["a"] = principals.admin
        with pytest.raises(Exception, match="not found"):
            await _call(app_db, _admin(seeded.a.id), sql, **params)
    assert _kinds(seeded, t) == [(t, False, False)]


def test_the_application_cannot_change_a_tenants_kind_directly(seeded: Seeded) -> None:
    for column in ("internal", "hosts_platform"):
        with raw_scope(seeded.app_dsn, str(seeded.a.id), "platform_admin") as cx, \
                pytest.raises(psycopg.errors.InsufficientPrivilege):
            cx.execute(f"UPDATE cloudops.tenant SET {column} = true")

