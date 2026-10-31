"""Fixtures for the tenancy tests.

Integration tests need a real PostgreSQL 16. Supply a superuser URL in
``CLOUDOPS_TEST_ADMIN_DSN`` (CI does this with a service container), or have
Docker available and Testcontainers will start one. With neither, the
integration tests *error* rather than skip: a skipped isolation test reads as
green, and these are a release gate.

The admin connection is used only to provision a throwaway database, an owner
role and a deliberately unprivileged application role. Everything under test
connects as the application role, exactly as production does.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from cloudops_tenancy import Database

if sys.platform == "win32":
    # psycopg's async mode cannot run on the default Proactor loop on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined,unused-ignore]

ADMIN_DSN_ENV = "CLOUDOPS_TEST_ADMIN_DSN"
POSTGRES_IMAGE = "pgvector/pgvector:pg16"

# Copied from reference/schema.sql so these tests do not depend on Phase 1
# migrations. Phase 1's isolation suite re-tests against the real schema.
CURRENT_TENANT_IDS = """
CREATE FUNCTION cloudops.current_tenant_ids() RETURNS uuid[] AS $$
    SELECT CASE
        WHEN current_setting('app.tenant_ids', true) IS NULL
          OR current_setting('app.tenant_ids', true) = '' THEN ARRAY[]::uuid[]
        ELSE string_to_array(current_setting('app.tenant_ids', true), ',')::uuid[]
    END;
$$ LANGUAGE sql STABLE
"""

# Policies exactly as the LLD specifies them (Database design, Row-level security).
PROBE_TABLES = """
CREATE TABLE cloudops.tenancy_probe (
    id        bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    label     text NOT NULL
);
ALTER TABLE cloudops.tenancy_probe ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.tenancy_probe FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.tenancy_probe
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));

CREATE TABLE cloudops.tenancy_commercial_probe (
    id        bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    margin    numeric(18,6) NOT NULL
);
ALTER TABLE cloudops.tenancy_commercial_probe ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.tenancy_commercial_probe FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.tenancy_commercial_probe
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY commercial_visibility ON cloudops.tenancy_commercial_probe
  AS RESTRICTIVE
  USING (current_setting('app.role', true) IN
         ('ceo','cfo','finops','delivery_lead','commercial','auditor','platform_admin'));
"""


@dataclass(frozen=True)
class Provisioned:
    admin_dsn: str
    app_dsn: str
    owner_member_dsn: str
    tenant_a: UUID
    tenant_b: UUID
    tenant_c: UUID
    rows_per_tenant: dict[UUID, int]


@contextmanager
def _admin_dsn() -> Iterator[str]:
    if dsn := os.environ.get(ADMIN_DSN_ENV):
        yield dsn
        return
    try:
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer(POSTGRES_IMAGE, driver=None)
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


def _with(dsn: str, **parts: Any) -> str:
    return make_url(dsn).set(**parts).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def provisioned() -> Iterator[Provisioned]:
    with _admin_dsn() as admin:
        suffix = secrets.token_hex(4)
        dbname = f"cloudops_tenancy_test_{suffix}"
        owner, app, member = (f"cloudops_t_{r}_{suffix}" for r in ("owner", "app", "member"))
        app_pw, member_pw = secrets.token_hex(24), secrets.token_hex(24)
        a, b, c = uuid4(), uuid4(), uuid4()
        rows = {a: 2, b: 3, c: 1}

        with psycopg.connect(_with(admin, drivername="postgresql"), autocommit=True) as cx:
            cx.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
            cx.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(owner)))
            for role, pw in ((app, app_pw), (member, member_pw)):
                cx.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS "
                        "NOCREATEDB NOCREATEROLE"
                    ).format(sql.Identifier(role), sql.Literal(pw))
                )
            cx.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(owner), sql.Identifier(member))
            )
            # The customer database role is cluster-wide and fixed by name (ADR 0018).
            if cx.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'cloudops_customer'"
            ).fetchone() is None:
                cx.execute("CREATE ROLE cloudops_customer NOLOGIN NOSUPERUSER NOBYPASSRLS")
            cx.execute(
                sql.SQL("GRANT cloudops_customer TO {} WITH INHERIT FALSE, SET TRUE").format(
                    sql.Identifier(app)
                )
            )

        test_admin = _with(admin, drivername="postgresql", database=dbname)
        try:
            with psycopg.connect(test_admin, autocommit=True) as cx:
                o, ap = sql.Identifier(owner), sql.Identifier(app)
                cx.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                cx.execute(sql.SQL("CREATE SCHEMA cloudops AUTHORIZATION {}").format(o))
                cx.execute(sql.SQL("SET ROLE {}").format(o))
                cx.execute(CURRENT_TENANT_IDS)
                cx.execute(PROBE_TABLES)
                cx.execute(sql.SQL("GRANT USAGE ON SCHEMA cloudops TO {}").format(ap))
                for grant in (
                    "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA cloudops TO {}",
                    "GRANT USAGE ON ALL SEQUENCES IN SCHEMA cloudops TO {}",
                ):
                    cx.execute(sql.SQL(grant).format(ap))
                # The customer role reads the probe table, never the commercial one.
                cx.execute("GRANT USAGE ON SCHEMA cloudops TO cloudops_customer")
                cx.execute("GRANT SELECT ON cloudops.tenancy_probe TO cloudops_customer")
                cx.execute("RESET ROLE")
                # Seeded as superuser, which bypasses RLS; nothing under test does.
                for tenant, n in rows.items():
                    for i in range(n):
                        cx.execute(
                            "INSERT INTO cloudops.tenancy_probe (tenant_id, label) VALUES (%s, %s)",
                            (tenant, f"row-{i}"),
                        )
                for tenant in (a, b):
                    cx.execute(
                        "INSERT INTO cloudops.tenancy_commercial_probe (tenant_id, margin) "
                        "VALUES (%s, 0.215000)",
                        (tenant,),
                    )

            yield Provisioned(
                admin_dsn=_with(admin, database=dbname),
                app_dsn=_with(admin, username=app, password=app_pw, database=dbname),
                owner_member_dsn=_with(admin, username=member, password=member_pw, database=dbname),
                tenant_a=a,
                tenant_b=b,
                tenant_c=c,
                rows_per_tenant=rows,
            )
        finally:
            with psycopg.connect(_with(admin, drivername="postgresql"), autocommit=True) as cx:
                drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                cx.execute(drop.format(sql.Identifier(dbname)))
                for role in (member, app, owner):
                    cx.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture
async def db(provisioned: Provisioned) -> AsyncIterator[Database]:
    """A pool of exactly one connection, so consecutive transactions share it."""
    database = Database.create(provisioned.app_dsn, pool_size=1, max_overflow=0)
    try:
        yield database
    finally:
        await database.dispose()
