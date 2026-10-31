"""Provision a database, run the real migrations as the owner role, seed three
tenants in three currencies, and hand out connections as the roles production
uses.

Needs a PostgreSQL 16 superuser URL in CLOUDOPS_TEST_ADMIN_DSN, or Docker (the
image in db/test-image is built by Testcontainers). Without either these tests
error; they never skip.

pg_partman: if the server lacks it, the migration is run with the test-only
``allow_missing_partman`` attribute and gets plain default partitions. CI sets
CLOUDOPS_REQUIRE_PARTMAN=1 so that path can never be what CI tests.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from cloudops_tenancy import Database

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined,unused-ignore]

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_DSN_ENV = "CLOUDOPS_TEST_ADMIN_DSN"
REQUIRE_PARTMAN_ENV = "CLOUDOPS_REQUIRE_PARTMAN"
EMBED_DIM = 1536


def embedding(hot: int) -> str:
    """A unit vector along axis ``hot``, in pgvector text form."""
    values = ["0"] * EMBED_DIM
    values[hot] = "1"
    return "[" + ",".join(values) + "]"


@dataclass(frozen=True)
class Tenant:
    id: UUID
    name: str
    currency: str
    account_id: UUID
    account_native_id: str
    resource_id: UUID
    axis: int  # embedding axis of this tenant's knowledge chunk


@dataclass(frozen=True)
class Seeded:
    admin_dsn: str
    migrator_dsn: str
    app_dsn: str
    ingest_dsn: str
    partman: bool
    a: Tenant
    b: Tenant
    c: Tenant
    global_axis: int
    # Tenant c's second subscription, with one resource, ticket and cost row:
    # what a subscription-limited principal must not see (ADR 0002, D16).
    c_account2: UUID
    c_resource2: UUID

    @property
    def tenants(self) -> tuple[Tenant, Tenant, Tenant]:
        return (self.a, self.b, self.c)


def _with(dsn: str, **parts: Any) -> str:
    return make_url(dsn).set(**parts).render_as_string(hide_password=False)


@contextmanager
def _admin_dsn() -> Iterator[str]:
    if dsn := os.environ.get(ADMIN_DSN_ENV):
        yield dsn
        return
    try:
        from testcontainers.core.image import DockerImage
        from testcontainers.postgres import PostgresContainer

        image = DockerImage(path=str(REPO_ROOT / "db/test-image"), tag="cloudops-pg-test:local")
        image.build()
        container = PostgresContainer(str(image), driver=None)
        container.start()
    except Exception as exc:
        pytest.fail(
            f"No test database: set {ADMIN_DSN_ENV} to a PostgreSQL 16 superuser URL, "
            f"or make Docker available for Testcontainers ({type(exc).__name__}).",
            pytrace=False,
        )
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


def _provision_access_roles(cx: psycopg.Connection[Any], migrator: str) -> None:
    """As infrastructure does: the function-owner roles migration 0004 needs.
    Cluster-wide, so created once and reused by every test database."""
    for role in ("cloudops_resolver", "cloudops_access_admin"):
        exists = cx.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if exists is None:
            cx.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT").format(
                    sql.Identifier(role)
                )
            )
        cx.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT FALSE").format(
                sql.Identifier(role), sql.Identifier(migrator)
            )
        )
    # What a customer scope runs as (migration 0011): cloudops_app may switch to
    # it, never inherit it. Created here if the server's bootstrap predates it.
    for role in ("cloudops_app", "cloudops_ingest", "cloudops_customer"):
        if cx.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
            cx.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT").format(
                    sql.Identifier(role)
                )
            )
    cx.execute("GRANT cloudops_customer TO cloudops_app WITH INHERIT FALSE, SET TRUE")


def _provision_extensions(cx: psycopg.Connection[Any], migrator: str) -> bool:
    _provision_access_roles(cx, migrator)
    cx.execute("CREATE EXTENSION IF NOT EXISTS vector")
    available = cx.execute(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'pg_partman'"
    ).fetchone()
    if available is None:
        if os.environ.get(REQUIRE_PARTMAN_ENV) == "1":
            pytest.fail("pg_partman is required here but not installed", pytrace=False)
        return False
    m = sql.Identifier(migrator)
    cx.execute("CREATE SCHEMA partman")
    cx.execute("CREATE EXTENSION pg_partman SCHEMA partman")
    for grant in (
        "GRANT ALL ON SCHEMA partman TO {}",
        "GRANT ALL ON ALL TABLES IN SCHEMA partman TO {}",
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA partman TO {}",
        "GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA partman TO {}",
    ):
        cx.execute(sql.SQL(grant).format(m))
    return True


def _migrate(migrator_dsn: str, partman: bool) -> None:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["dsn"] = migrator_dsn
    cfg.attributes["allow_missing_partman"] = not partman
    command.upgrade(cfg, "head")


def _seed_second_subscription(
    cx: psycopg.Connection[Any], t: Tenant, account: UUID, resource: UUID
) -> None:
    now = datetime.now(UTC)
    cx.execute(
        "INSERT INTO cloudops.cloud_account (id, tenant_id, provider, native_id, display_name)"
        " VALUES (%s, %s, 'aws', %s, %s)",
        (account, t.id, f"{t.account_native_id}-2", f"{t.name} subsidiary"),
    )
    cx.execute(
        "INSERT INTO cloudops.resource (id, tenant_id, account_id, native_id, name,"
        " type_native, type_canonical) VALUES (%s, %s, %s, %s, %s,"
        " 'AWS::EC2::Instance', 'compute.vm')",
        (resource, t.id, account, f"/vm/{t.name}-2", f"vm-{t.name}-02"),
    )
    cx.execute(
        "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
        " resource_id) VALUES (%s, 'incident', %s, %s, %s, %s)",
        (t.id, f"INC-{t.name}-2", t.name.upper(), now - timedelta(days=1), resource),
    )
    cx.execute(
        "INSERT INTO cloudops.cost_fact (tenant_id, account_id, resource_id, usage_date,"
        " invoice_period, service_native, unblended_cost, amortized_cost, currency)"
        " VALUES (%s, %s, %s, %s, %s, 'EC2', 7.500000, 7.500000, 'USD')",
        (t.id, account, resource, now.date(), now.strftime("%Y-%m")),
    )


def _seed(cx: psycopg.Connection[Any], tenants: tuple[Tenant, ...], global_axis: int) -> None:
    """Seeded as superuser, which bypasses RLS. Nothing under test does."""
    now = datetime.now(UTC)
    for t in tenants:
        cx.execute(
            "INSERT INTO cloudops.tenant (id, canonical_name, short_code) VALUES (%s, %s, %s)",
            (t.id, t.name, t.name.upper()),
        )
        cx.execute(
            "INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias)"
            " VALUES (%s, 'servicenow', %s)",
            (t.id, f"{t.name.upper()} LIMITED"),
        )
        cx.execute(
            "INSERT INTO cloudops.sow (tenant_id, reference, effective_from, currency,"
            " monthly_manage_revenue) VALUES (%s, %s, '2026-01-01', %s, 41250.500000)",
            (t.id, f"SOW-{t.name}", t.currency),
        )
        cx.execute(
            "INSERT INTO cloudops.cloud_account (id, tenant_id, provider, native_id, display_name)"
            " VALUES (%s, %s, 'azure', %s, %s)",
            (t.account_id, t.id, t.account_native_id, f"{t.name} production"),
        )
        cx.execute(
            "INSERT INTO cloudops.resource (id, tenant_id, account_id, native_id, name,"
            " type_native, type_canonical) VALUES (%s, %s, %s, %s, %s,"
            " 'Microsoft.Compute/virtualMachines', 'compute.vm')",
            (t.resource_id, t.id, t.account_id, f"/vm/{t.name}", f"vm-{t.name}-01"),
        )
        cx.execute(
            "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
            " resource_id, closure_code) VALUES (%s, 'incident', %s, %s, %s, %s,"
            " 'Resolved (Workaround)')",
            (
                t.id,
                f"INC-{t.name}",
                f"{t.name.upper()} LIMITED",
                now - timedelta(days=2),
                t.resource_id,
            ),
        )
        cx.execute(
            "INSERT INTO cloudops.vulnerability (tenant_id, qid, asset_name, scope, title,"
            " severity, first_detected) VALUES (%s, 91234, %s, 'os', 'OpenSSL', 4, %s)",
            (t.id, f"vm-{t.name}-01", now - timedelta(days=3)),
        )
        cx.execute(
            "INSERT INTO cloudops.cost_fact (tenant_id, account_id, resource_id, usage_date,"
            " invoice_period, service_native, unblended_cost, amortized_cost, currency)"
            " VALUES (%s, %s, %s, %s, %s, 'Virtual Machines', 120.125000, 118.000000, %s)",
            (t.id, t.account_id, t.resource_id, now.date(), now.strftime("%Y-%m"), t.currency),
        )
        cx.execute(
            "INSERT INTO cloudops.knowledge_chunk (tenant_id, source_kind, title, body, embedding)"
            " VALUES (%s, 'close_note', %s, %s, %s)",
            (t.id, f"{t.name} fix", f"Close note for {t.name}", embedding(t.axis)),
        )
        triage = cx.execute(
            "INSERT INTO cloudops.triage_result (tenant_id, number, resource_id, route, stage,"
            " cost_currency, workflow_id) VALUES (%s, %s, %s, 'human', 2, 'USD', %s) RETURNING id",
            (t.id, f"INC-{t.name}", t.resource_id, f"seed-triage-{t.name}"),
        ).fetchone()
        cx.execute(
            "INSERT INTO cloudops.triage_review (tenant_id, triage_result_id, decision,"
            " reviewer_oid) VALUES (%s, %s, 'accept', gen_random_uuid())",
            (t.id, triage[0] if triage else None),
        )
        cx.execute(
            "INSERT INTO audit.action_log (tenant_id, actor, action)"
            " VALUES (%s, 'user:seed', 'read')",
            (t.id,),
        )
        cx.execute(
            "INSERT INTO staging.resource_quarantine (tenant_id, account_id, source_system,"
            " external_key, name) VALUES (%s, %s, 'qualys', %s, 'unknown-host')",
            (t.id, t.account_id, f"q-{t.name}"),
        )
        # Step 15 tables, one row each, so "zero rows" is never an empty table.
        cx.execute(
            "INSERT INTO cloudops.cost_finding (tenant_id, account_id, source_system, finding_id,"
            " category, title, status, estimated_saving, saving_currency, customer_raw,"
            " detected_at, updated_at) VALUES (%s, %s, 'corestack', %s, 'idle', 'Idle VM',"
            " 'open', 12.500000, %s, %s, now(), now())",
            (t.id, t.account_id, f"f-{t.name}", t.currency, t.name),
        )
        cx.execute(
            "INSERT INTO cloudops.directory_principal (tenant_id, directory_id, object_id, kind,"
            " display_name) VALUES (%s, %s, %s, 'user', %s)",
            (t.id, f"dir-{t.name}", f"u-{t.name}", f"{t.name} admin"),
        )
        cx.execute(
            "INSERT INTO cloudops.directory_role_assignment (tenant_id, directory_id,"
            " assignment_id, principal_object_id, role_definition_id, role_name,"
            " directory_scope_id) VALUES (%s, %s, %s, %s, 'r-ga', 'Global Administrator', '/')",
            (t.id, f"dir-{t.name}", f"ra-{t.name}", f"u-{t.name}"),
        )
        cx.execute(
            "INSERT INTO cloudops.sharepoint_site (tenant_id, site_id, site_url, customer_raw)"
            " VALUES (%s, %s, %s, %s)",
            (t.id, f"site-{t.name}", f"https://{t.name}.sharepoint.example/sites/ops", t.name),
        )
        cx.execute(
            "INSERT INTO cloudops.document (tenant_id, source_system, site_id, item_id, name,"
            " extraction) VALUES (%s, 'sharepoint', %s, %s, 'design.md', 'text')",
            (t.id, f"site-{t.name}", f"item-{t.name}"),
        )
        cx.execute(
            "INSERT INTO staging.cost_fact_load (tenant_id, account_id, assembly_id, object_key,"
            " usage_date, invoice_period, charge_class, unblended_cost, amortized_cost,"
            " currency) VALUES (%s, %s, 'asm-1', 'k', now()::date, to_char(now(), 'YYYY-MM'),"
            " 'consumption', 1.000000, 1.000000, %s)",
            (t.id, t.account_id, t.currency),
        )
    cx.execute(
        "INSERT INTO cloudops.global_knowledge_chunk (source_kind, title, body, embedding)"
        " VALUES ('runbook', 'Global runbook', 'Restart the service', %s)",
        (embedding(global_axis),),
    )
    cx.execute(
        "INSERT INTO staging.tenant_alias_quarantine (source_system, alias)"
        " VALUES ('qualys', 'Unmatched Holdings BV')"
    )


@dataclass(frozen=True)
class Provisioned:
    admin_dsn: str  # superuser, on the test database
    migrator_dsn: str
    app_dsn: str
    ingest_dsn: str
    partman: bool
    suffix: str


@pytest.fixture(scope="session")
def admin_server() -> Iterator[str]:
    """One server for the session (one container, if Testcontainers)."""
    with _admin_dsn() as admin:
        yield admin


@contextmanager
def _database(admin: str, label: str) -> Iterator[Provisioned]:
    """A fresh database migrated to head, with migrator, app and ingest logins
    made exactly as production makes them. Dropped afterwards."""
    suffix = secrets.token_hex(4)
    dbname = f"cloudops_{label}_test_{suffix}"
    migrator, app, ingest = (f"cloudops_t_{r}_{suffix}" for r in ("migrator", "app", "ingest"))
    passwords = {r: secrets.token_hex(24) for r in (migrator, app, ingest)}
    server = _with(admin, drivername="postgresql")

    with psycopg.connect(server, autocommit=True) as cx:
        cx.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
        for role, pw in passwords.items():
            extra = "CREATEROLE" if role == migrator else ""
            cx.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS " + extra
                ).format(sql.Identifier(role), sql.Literal(pw))
            )

    test_admin = _with(admin, drivername="postgresql", database=dbname)
    dsn = {r: _with(admin, username=r, password=pw, database=dbname) for r, pw in passwords.items()}
    try:
        with psycopg.connect(test_admin, autocommit=True) as cx:
            cx.execute(
                sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(dbname), sql.Identifier(migrator)
                )
            )
            partman = _provision_extensions(cx, migrator)

        _migrate(dsn[migrator], partman)

        with psycopg.connect(test_admin, autocommit=True) as cx:
            for login, group in ((app, "cloudops_app"), (ingest, "cloudops_ingest")):
                cx.execute(
                    sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(login))
                )
        yield Provisioned(test_admin, dsn[migrator], dsn[app], dsn[ingest], partman, suffix)
    finally:
        with psycopg.connect(server, autocommit=True) as cx:
            cx.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )
            for role in (app, ingest, migrator):
                cx.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture(scope="session")
def seeded(admin_server: str) -> Iterator[Seeded]:
    with _database(admin_server, "schema") as db:
        with psycopg.connect(db.admin_dsn, autocommit=True) as cx:
            tenants = tuple(
                Tenant(uuid4(), name, ccy, uuid4(), f"sub-{name}-{db.suffix}", uuid4(), axis)
                for name, ccy, axis in (
                    ("alpha", "EUR", 1), ("bravo", "GBP", 2), ("charlie", "USD", 3)
                )
            )
            _seed(cx, tenants, global_axis=4)
            c_account2, c_resource2 = uuid4(), uuid4()
            _seed_second_subscription(cx, tenants[2], c_account2, c_resource2)
        yield Seeded(
            admin_dsn=db.admin_dsn,
            migrator_dsn=db.migrator_dsn,
            app_dsn=db.app_dsn,
            ingest_dsn=db.ingest_dsn,
            partman=db.partman,
            a=tenants[0],
            b=tenants[1],
            c=tenants[2],
            global_axis=4,
            c_account2=c_account2,
            c_resource2=c_resource2,
        )


@pytest.fixture
async def app_db(seeded: Seeded) -> AsyncIterator[Database]:
    database = Database.create(seeded.app_dsn, pool_size=2, max_overflow=0)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def ingest_db(seeded: Seeded) -> AsyncIterator[Database]:
    database = Database.create(seeded.ingest_dsn, pool_size=1, max_overflow=0)
    try:
        yield database
    finally:
        await database.dispose()


# --- Access and the API (step 04) -------------------------------------------------


@dataclass(frozen=True)
class Principals:
    """One principal per shape the resolver must handle, created through the
    real access functions by a bootstrapped administrator."""

    admin: UUID
    finops_all: UUID  # staff, all tenants
    engineer_a: UUID  # staff, tenant a only
    customer_a: UUID  # customer of tenant a
    customer_c_limited: UUID  # customer of tenant c, first subscription only
    tickets_only: UUID  # staff role whose matrix lacks inventory.read
    revocable: UUID  # staff, tenant a; tests revoke it
    staff_no_tenants: UUID  # valid identity, nothing assigned -> 403
    disabled: UUID  # customer of a, disabled -> 403
    customer_two_tenants: UUID  # defect -> 500
    customer_no_tenant: UUID  # defect -> 500
    # Step 18: each tenant's portal is onboarded and live, with a first
    # administrator (a CXO) assigned through the onboarding functions.
    tenant_admin_a: UUID
    tenant_admin_b: UUID
    tenant_admin_c: UUID


# The role set the suite's tenants agree: every default customer role (0011)
# and the original viewer.
AGREED_ROLES = (
    "Customer viewer", "CXO", "VP or director", "Architect", "Customer engineer",
    "Security officer", "Approver",
)


async def onboard_portal(conn: Any, actor: UUID, tenant: UUID, first_admin: UUID) -> None:
    """HLD M2, as a platform administrator performs it."""
    await _access(
        conn, "SELECT access.portal_record_federation(:a, :t, 'entra', :ref, :domains)",
        a=actor, t=tenant, ref=str(uuid4()), domains=[f"t{tenant.hex[:8]}.example.test"],
    )
    await _access(
        conn, "SELECT access.portal_agree_roles(:a, :t, :roles, 'TEST-AGREEMENT')",
        a=actor, t=tenant, roles=list(AGREED_ROLES),
    )
    await _access(
        conn, "SELECT access.portal_assign_first_admin(:a, :t, :o, 'First admin', 'CXO')",
        a=actor, t=tenant, o=first_admin,
    )
    await _access(conn, "SELECT access.portal_set_live(:a, :t, true)", a=actor, t=tenant)


async def _access(conn: Any, sql_text: str, **params: Any) -> None:
    from sqlalchemy import text

    await conn.execute(text(sql_text), params)


@pytest.fixture(scope="session")
async def principals(seeded: Seeded) -> Principals:
    from cloudops_tenancy import TenantScope

    p = Principals(*(uuid4() for _ in range(14)))
    db = Database.create(seeded.app_dsn, pool_size=1, max_overflow=0)
    a, b, c = seeded.a.id, seeded.b.id, seeded.c.id
    try:
        async with db.transaction(TenantScope.for_resolution("setup:principals")) as conn:
            await _access(conn, "SELECT access.bootstrap_admin(:o, 'Test admin')", o=p.admin)
            act = p.admin

            async def principal(
                oid: UUID, kind: str, role: str, *tenants: UUID, all_tenants: bool = False
            ) -> None:
                await _access(
                    conn,
                    "SELECT access.set_principal(:a, :o, :k, :n, :r, :all)",
                    a=act, o=oid, k=kind, n=f"{kind} {oid}", r=role, all=all_tenants,
                )
                for t in tenants:
                    await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)", a=act, o=oid, t=t)

            await principal(p.finops_all, "staff", "FinOps", all_tenants=True)
            await principal(p.engineer_a, "staff", "Engineer", a)
            await principal(p.customer_a, "customer", "Customer viewer", a)
            await principal(p.customer_c_limited, "customer", "Customer viewer", c)
            await _access(
                conn,
                "SELECT access.restrict_accounts(:a, :o, :t, CAST(:ids AS uuid[]))",
                a=act, o=p.customer_c_limited, t=c, ids=[seeded.c.account_id],
            )
            await _access(
                conn,
                "SELECT access.define_role(:a, 'Tickets only', 'engineer', 'test role')",
                a=act,
            )
            await _access(
                conn, "SELECT access.grant_permission(:a, 'Tickets only', 'tickets.read')", a=act
            )
            await principal(p.tickets_only, "staff", "Tickets only", a)
            await principal(p.revocable, "staff", "Engineer", a)
            await principal(p.staff_no_tenants, "staff", "Engineer")
            await principal(p.disabled, "customer", "Customer viewer", a)
            await _access(conn, "SELECT access.disable_principal(:a, :o)", a=act, o=p.disabled)
            # Defects: the functions allow building them; the resolver must refuse.
            await principal(p.customer_two_tenants, "customer", "Customer viewer", a, b)
            await principal(p.customer_no_tenant, "customer", "Customer viewer")
            for tenant, first_admin in ((a, p.tenant_admin_a), (b, p.tenant_admin_b),
                                        (c, p.tenant_admin_c)):
                await onboard_portal(conn, act, tenant, first_admin)
    finally:
        await db.dispose()
    return p


@dataclass
class Api:
    client: Any  # httpx.AsyncClient
    signing_key: dict[str, Any]
    auth: Any  # AuthConfig

    def token(self, oid: UUID, **mint_kwargs: Any) -> str:
        from cloudops_devdata.tokens import mint

        return mint(self.signing_key, self.auth, oid, **mint_kwargs)

    async def get(
        self,
        path: str,
        oid: UUID,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        **mint_kwargs: Any,
    ) -> Any:
        auth = {"Authorization": f"Bearer {self.token(oid, **mint_kwargs)}"}
        return await self.client.get(path, headers={**auth, **(headers or {})}, params=params)


@pytest.fixture(scope="session")
async def api(seeded: Seeded, principals: Principals) -> AsyncIterator[Api]:
    import httpx

    from cloudops_api.app import create_app
    from cloudops_api.auth import StaticKeys, TokenValidator
    from cloudops_config import AuthConfig
    from cloudops_devdata.tokens import new_signing_key

    auth = AuthConfig(
        issuer="https://login.example.test/44444444-4444-4444-8444-444444444444/v2.0",
        audience="api://cloudops-isolation-suite",
        tenant_id="44444444-4444-4444-8444-444444444444",
        required_scope="access_as_user",
        jwks_url=None,
        jwks_secret=None,
    )
    private, public = new_signing_key("suite")
    db = Database.create(seeded.app_dsn, pool_size=4, max_overflow=0)
    app = create_app(db=db, validator=TokenValidator(auth, StaticKeys(public)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        yield Api(client, private, auth)
    await db.dispose()


# --- The synthetic dataset, for connectors (step 05) ------------------------------


@dataclass(frozen=True)
class Synthetic:
    """A database holding the synthetic dataset's tenants, aliases, accounts and
    resources, but no tickets and no quarantine: whatever lands there in a
    connector test, the connector put there."""

    admin_dsn: str
    app_dsn: str
    ingest_dsn: str


async def _synthetic(
    admin_server: str, label: str, *clear: str
) -> AsyncIterator[Synthetic]:
    from dataclasses import replace

    from cloudops_devdata.seed import load
    from cloudops_devdata.servicenow_fixtures import FIXTURE_ANCHOR, FIXTURE_SEED
    from cloudops_devdata.synthetic import generate

    ds = replace(
        generate(FIXTURE_ANCHOR, FIXTURE_SEED),
        work_items=[], alias_quarantine=[], resource_quarantine=[],
    )
    for field in clear:
        setattr(ds, field, [])
    with _database(admin_server, label) as db:
        ingest = Database.create(db.ingest_dsn, pool_size=1, max_overflow=0)
        try:
            await load(ingest, ds)
        finally:
            await ingest.dispose()
        yield Synthetic(db.admin_dsn, db.app_dsn, db.ingest_dsn)


@pytest.fixture(scope="session")
async def synthetic(admin_server: str) -> AsyncIterator[Synthetic]:
    async for s in _synthetic(admin_server, "connector"):
        yield s


@pytest.fixture(scope="module")
async def synthetic_module(admin_server: str) -> AsyncIterator[Synthetic]:
    """The same dataset in a database of the test module's own, for connectors
    whose tests assert on whole tables (step 15): no findings and no cost either,
    so whatever is there, the connector under test put there."""
    async for s in _synthetic(admin_server, "conn", "vulnerabilities", "costs"):
        yield s


# --- The telemetry column store (step 15) -------------------------------------------

CLICKHOUSE_URL_ENV = "CLOUDOPS_TEST_CLICKHOUSE_URL"  # an admin URL: http://user:pw@host:8123
CLICKHOUSE_IMAGE = "clickhouse/clickhouse-server:24.8.14.39"  # as docker-compose.yml


@dataclass(frozen=True)
class Telemetry:
    url: str
    username: str  # the application's user: SELECT and INSERT only
    password: str
    admin: Any  # httpx.Client as the store's administrator, for assertions


@contextmanager
def _clickhouse_admin() -> Iterator[tuple[str, str, str]]:
    if url := os.environ.get(CLICKHOUSE_URL_ENV):
        parts = make_url(url.replace("http://", "clickhouse://", 1))
        yield (f"http://{parts.host}:{parts.port or 8123}", parts.username or "",
               parts.password or "")
        return
    import urllib.request

    try:
        from testcontainers.core.container import DockerContainer

        password = secrets.token_hex(16)
        container = (
            DockerContainer(CLICKHOUSE_IMAGE)
            .with_env("CLICKHOUSE_USER", "cloudops_admin")
            .with_env("CLICKHOUSE_PASSWORD", password)
            .with_env("CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT", "1")
            .with_exposed_ports(8123)
        )
        container.start()
    except Exception as exc:
        pytest.fail(
            f"No telemetry store: set {CLICKHOUSE_URL_ENV} to an admin URL, or make Docker "
            f"available for Testcontainers ({type(exc).__name__}).",
            pytrace=False,
        )
    try:
        base = (f"http://{container.get_container_host_ip()}:"
                f"{container.get_exposed_port(8123)}")
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"{base}/ping", timeout=2) as r:  # noqa: S310
                    if r.read().strip() == b"Ok.":
                        break
            except OSError:
                pass
            import time

            time.sleep(0.5)
        yield base, "cloudops_admin", password
    finally:
        container.stop()


@pytest.fixture(scope="session")
def telemetry_store() -> Iterator[Telemetry]:
    """The schema from db/telemetry, applied as the administrator, and an
    application user with SELECT and INSERT only, as production has."""
    import httpx

    from db.telemetry.migrate import migrate

    with _clickhouse_admin() as (base, user, password):
        admin = httpx.Client(base_url=base, timeout=30.0,
                             headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password})
        migrate(admin)
        app_user, app_password = f"cloudops_t_{secrets.token_hex(4)}", secrets.token_hex(16)
        for statement in (
            f"CREATE USER {app_user} IDENTIFIED WITH sha256_password BY '{app_password}'",
            f"GRANT SELECT, INSERT ON telemetry.* TO {app_user}",
        ):
            admin.post("/", content=statement.encode()).raise_for_status()
        try:
            yield Telemetry(base, app_user, app_password, admin)
        finally:
            admin.post("/", content=f"DROP USER IF EXISTS {app_user}".encode())
            admin.close()
