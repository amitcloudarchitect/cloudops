"""Migration 0029 folds each account PR 62 shared (the internal tenant and one
customer, ADR 0027) into its customer, and moves the account's alias with it, so
the platform's own cost feed (ADR 0023) writes to the single owner (ADR 0031).

The session fixture migrates straight to head, so it never sees a shared pair.
This test stops at 0028, plants one, and upgrades from there.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from .conftest import REPO_ROOT, Seeded, _provision_extensions, _with

pytestmark = [pytest.mark.integration]


@pytest.fixture
def at_0028(seeded: Seeded) -> Iterator[tuple[Config, str]]:
    """A fresh database migrated to 0028 only, owned by the migrator role."""
    server = _with(seeded.admin_dsn, drivername="postgresql", database="postgres")
    migrator = make_url(seeded.migrator_dsn).username
    assert migrator is not None
    dbname = f"cloudops_fold_test_{secrets.token_hex(4)}"
    with psycopg.connect(server, autocommit=True) as cx:
        cx.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    admin = _with(seeded.admin_dsn, drivername="postgresql", database=dbname)
    try:
        with psycopg.connect(admin, autocommit=True) as cx:
            cx.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(dbname), sql.Identifier(migrator)))
            partman = _provision_extensions(cx, migrator)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["dsn"] = _with(seeded.migrator_dsn, database=dbname)
        cfg.attributes["allow_missing_partman"] = not partman
        command.upgrade(cfg, "0028")
        yield cfg, admin
    finally:
        with psycopg.connect(server, autocommit=True) as cx:
            cx.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(dbname)))


def test_a_shared_account_is_folded_into_its_customer(at_0028: tuple[Config, str]) -> None:
    cfg, admin = at_0028
    internal, customer, other = uuid4(), uuid4(), uuid4()
    shared, own = str(uuid4()), str(uuid4())
    # Planted as superuser, which bypasses RLS; the migrations run as the owner.
    with psycopg.connect(admin, autocommit=True) as cx:
        # 0025's trigger reads every tenant's rows as its owner, under account scope.
        cx.execute("SELECT set_config('app.account_ids', 'all', false)")
        for t, code, is_internal in ((internal, "GTX", True), (customer, "AZS", False),
                                     (other, "OTH", False)):
            cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code, internal)"
                       " VALUES (%s, %s, %s, %s)", (t, code, code, is_internal))
        for t, sub in ((internal, shared), (customer, shared), (internal, own)):
            cx.execute("INSERT INTO cloudops.cloud_account (tenant_id, provider, native_id,"
                       " display_name) VALUES (%s, 'azure', %s, 'x')", (t, sub))
        for t, sub in ((internal, shared), (internal, own)):
            cx.execute("INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias)"
                       " VALUES (%s, 'azure', %s)", (t, sub))
        assert cx.execute("SELECT count(*) FROM cloudops.cloud_account"
                          " WHERE shared_with_platform").fetchone() == (2,)

    command.upgrade(cfg, "head")

    with psycopg.connect(admin) as cx:
        accounts = cx.execute("SELECT tenant_id, native_id, shared_with_platform"
                              " FROM cloudops.cloud_account ORDER BY native_id = %s DESC",
                              (shared,)).fetchall()
        aliases: dict[str, UUID] = dict(cx.execute(
            "SELECT alias, tenant_id FROM cloudops.tenant_alias"
            " WHERE source_system = 'azure'").fetchall())
    # The customer keeps the shared one; the internal tenant keeps what was its alone.
    assert accounts == [(customer, shared, False), (internal, own, False)]
    assert aliases == {shared: customer, own: internal}
    # And no second owner can come back.
    with psycopg.connect(admin, autocommit=True) as cx, \
            pytest.raises(psycopg.errors.UniqueViolation):
        cx.execute("INSERT INTO cloudops.cloud_account (tenant_id, provider, native_id,"
                   " display_name) VALUES (%s, 'azure', %s, 'x')", (other, shared))
