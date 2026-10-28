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
    cx.execute(
        "INSERT INTO cloudops.global_knowledge_chunk (source_kind, title, body, embedding)"
        " VALUES ('runbook', 'Global runbook', 'Restart the service', %s)",
        (embedding(global_axis),),
    )
    cx.execute(
        "INSERT INTO staging.tenant_alias_quarantine (source_system, alias)"
        " VALUES ('qualys', 'Unmatched Holdings BV')"
    )


@pytest.fixture(scope="session")
def seeded() -> Iterator[Seeded]:
    with _admin_dsn() as admin:
        suffix = secrets.token_hex(4)
        dbname = f"cloudops_schema_test_{suffix}"
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
        dsn = {
            r: _with(admin, username=r, password=pw, database=dbname) for r, pw in passwords.items()
        }
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
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier(group), sql.Identifier(login)
                        )
                    )
                tenants = tuple(
                    Tenant(uuid4(), name, ccy, uuid4(), f"sub-{name}-{suffix}", uuid4(), axis)
                    for name, ccy, axis in (
                        ("alpha", "EUR", 1),
                        ("bravo", "GBP", 2),
                        ("charlie", "USD", 3),
                    )
                )
                _seed(cx, tenants, global_axis=4)
                c_account2, c_resource2 = uuid4(), uuid4()
                _seed_second_subscription(cx, tenants[2], c_account2, c_resource2)

            yield Seeded(
                admin_dsn=test_admin,
                migrator_dsn=dsn[migrator],
                app_dsn=dsn[app],
                ingest_dsn=dsn[ingest],
                partman=partman,
                a=tenants[0],
                b=tenants[1],
                c=tenants[2],
                global_axis=4,
                c_account2=c_account2,
                c_resource2=c_resource2,
            )
        finally:
            with psycopg.connect(server, autocommit=True) as cx:
                cx.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(dbname)
                    )
                )
                for role in (app, ingest, migrator):
                    cx.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


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


async def _access(conn: Any, sql_text: str, **params: Any) -> None:
    from sqlalchemy import text

    await conn.execute(text(sql_text), params)


@pytest.fixture(scope="session")
async def principals(seeded: Seeded) -> Principals:
    from cloudops_tenancy import TenantScope

    p = Principals(*(uuid4() for _ in range(11)))
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
