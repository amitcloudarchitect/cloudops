"""Alembic environment for the platform database.

Migrations are the one place outside packages/tenancy that opens a database
connection (allowlisted in tools/ci/data_access_allowlist.txt). They run as the
dedicated migration role, which owns the schema, from a pipeline job — never
from a human session or an application startup hook (LLD, Migrations).

Every connection runs with lock_timeout = 3s so a migration blocked behind
application traffic fails fast instead of queueing the application behind it.
The setting is a connection startup option, then checked, so a pooler that
silently drops startup options cannot quietly disable it.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import make_url

DSN_FILE_ENV = "CLOUDOPS_MIGRATION_DSN_FILE"
LOCK_TIMEOUT = "3s"
VERSION_SCHEMA = "migration_meta"


def _dsn() -> str:
    # Tests pass the DSN programmatically; the CLI never can.
    if dsn := context.config.attributes.get("dsn"):
        return str(dsn)
    path = os.environ.get(DSN_FILE_ENV)
    if not path:
        raise RuntimeError(f"set {DSN_FILE_ENV} to the CSI-mounted migration DSN file")
    return Path(path).read_text(encoding="utf-8").strip()


def run_migrations_online() -> None:
    url = make_url(_dsn()).set(drivername="postgresql+psycopg")
    engine = create_engine(
        url,
        poolclass=pool.NullPool,
        connect_args={"options": f"-c lock_timeout={LOCK_TIMEOUT}"},
    )
    with engine.connect() as connection:
        actual = connection.execute(text("SHOW lock_timeout")).scalar_one()
        if actual != LOCK_TIMEOUT:
            raise RuntimeError(f"lock_timeout is {actual!r}, expected {LOCK_TIMEOUT!r}")
        # The version table lives in its own schema: public is not writable
        # (PostgreSQL 15+ default, and kept that way) and is no place for it.
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {VERSION_SCHEMA}"))
        connection.execute(text(f"REVOKE ALL ON SCHEMA {VERSION_SCHEMA} FROM PUBLIC"))
        connection.commit()
        context.configure(
            connection=connection,
            transaction_per_migration=True,
            version_table_schema=VERSION_SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (--sql) migrations are not supported")
run_migrations_online()
