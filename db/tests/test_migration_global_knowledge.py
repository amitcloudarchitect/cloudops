"""Migrations 0002 and 0003 move Getronics-wide knowledge out of
knowledge_chunk without losing a row, then require a tenant (ADR 0001).

The session fixture migrates straight to head before seeding, so it never
sees a NULL-tenant chunk being moved. This test stops at 0001, plants one,
and upgrades from there.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from .conftest import REPO_ROOT, Seeded, _provision_extensions, _with, embedding

pytestmark = [pytest.mark.integration]


@pytest.fixture
def at_0001(seeded: Seeded) -> Iterator[tuple[Config, str]]:
    """A fresh database migrated to 0001 only, owned by the migrator role."""
    server = _with(seeded.admin_dsn, drivername="postgresql", database="postgres")
    migrator = make_url(seeded.migrator_dsn).username
    assert migrator is not None
    dbname = f"cloudops_contract_test_{secrets.token_hex(4)}"
    with psycopg.connect(server, autocommit=True) as cx:
        cx.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    admin = _with(seeded.admin_dsn, drivername="postgresql", database=dbname)
    try:
        with psycopg.connect(admin, autocommit=True) as cx:
            cx.execute(
                sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(dbname), sql.Identifier(migrator)
                )
            )
            partman = _provision_extensions(cx, migrator)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["dsn"] = _with(seeded.migrator_dsn, database=dbname)
        cfg.attributes["allow_missing_partman"] = not partman
        command.upgrade(cfg, "0001")
        yield cfg, admin
    finally:
        with psycopg.connect(server, autocommit=True) as cx:
            cx.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )


def test_global_rows_move_and_tenant_becomes_required(at_0001: tuple[Config, str]) -> None:
    cfg, admin = at_0001
    tenant, own_chunk, global_chunk = uuid4(), uuid4(), uuid4()
    # Planted as superuser, which bypasses RLS; the migrations run as the owner.
    with psycopg.connect(admin, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO cloudops.tenant (id, canonical_name, short_code) VALUES (%s, 'T', 'T')",
            (tenant,),
        )
        for chunk_id, tenant_id, body in (
            (own_chunk, tenant, "customer close note"),
            (global_chunk, None, "shared runbook"),
        ):
            cx.execute(
                "INSERT INTO cloudops.knowledge_chunk (id, tenant_id, source_kind, body, embedding)"
                " VALUES (%s, %s, 'runbook', %s, %s)",
                (chunk_id, tenant_id, body, embedding(5)),
            )

    command.upgrade(cfg, "head")

    with psycopg.connect(admin) as cx:
        moved: list[Any] = cx.execute(
            "SELECT id, body, embedding IS NOT NULL FROM cloudops.global_knowledge_chunk"
            # Not the assistant's help pages, which the migrate step loads (ADR 0029).
            " WHERE source_kind NOT LIKE 'platform_help%'"
        ).fetchall()
        remaining = cx.execute("SELECT id, tenant_id FROM cloudops.knowledge_chunk").fetchall()
        not_null = cx.execute(
            "SELECT attnotnull FROM pg_attribute"
            " WHERE attrelid = 'cloudops.knowledge_chunk'::regclass AND attname = 'tenant_id'"
        ).fetchone()
        policy = cx.execute(
            "SELECT pg_get_expr(polqual, polrelid) FROM pg_policy"
            " WHERE polrelid = 'cloudops.knowledge_chunk'::regclass"
            " AND polname = 'tenant_isolation'"
        ).fetchone()
    assert moved == [(global_chunk, "shared runbook", True)]
    assert remaining == [(own_chunk, tenant)]
    assert not_null == (True,)
    assert policy is not None
    assert "IS NULL" not in policy[0]
