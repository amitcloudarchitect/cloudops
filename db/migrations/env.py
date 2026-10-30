"""Alembic environment for the platform database.

Migrations are the one place outside packages/tenancy that opens a database
connection (allowlisted in tools/ci/data_access_allowlist.txt). They run as the
dedicated migration role, which owns the schema, from a pipeline job — never
from a human session or an application startup hook (LLD, Migrations).

Every connection runs with lock_timeout = 3s so a migration blocked behind
application traffic fails fast instead of queueing the application behind it.
The setting is a connection startup option, then checked, so a pooler that
silently drops startup options cannot quietly disable it.

In Azure the migration role is an Entra identity (the PreSync job's workload
identity, ADR 0008): with ``CLOUDOPS_MIGRATION_AUTH=entra`` the DSN file holds no
password, and an Entra access token for the server is used as the password,
fetched once for the run. Tokens last about an hour; a migration run is minutes.

After migrating, the job applies the one database setting that is configuration
rather than schema: ``CLOUDOPS_MAPPING_APPROVERS`` (1 or 2), how many people a
customer-mapping approval needs (migration 0009, ADR 0015). Unset leaves it as
it is; the schema's default is two. Only the dev overlay and the local stack set
one. The job runs on every sync, so the database converges on the overlay.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import Connection, create_engine, pool, text
from sqlalchemy.engine import make_url

DSN_FILE_ENV = "CLOUDOPS_MIGRATION_DSN_FILE"
AUTH_ENV = "CLOUDOPS_MIGRATION_AUTH"
ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
LOCK_TIMEOUT = "3s"
VERSION_SCHEMA = "migration_meta"
APPROVERS_ENV = "CLOUDOPS_MAPPING_APPROVERS"


def _dsn() -> str:
    # Tests pass the DSN programmatically; the CLI never can.
    if dsn := context.config.attributes.get("dsn"):
        return str(dsn)
    path = os.environ.get(DSN_FILE_ENV)
    if not path:
        raise RuntimeError(f"set {DSN_FILE_ENV} to the CSI-mounted migration DSN file")
    return Path(path).read_text(encoding="utf-8").strip()


def _entra_token() -> str:
    from azure.identity import WorkloadIdentityCredential

    credential = WorkloadIdentityCredential()
    try:
        return credential.get_token(ENTRA_SCOPE).token
    finally:
        credential.close()


def run_migrations_online() -> None:
    url = make_url(_dsn()).set(drivername="postgresql+psycopg")
    auth = os.environ.get(AUTH_ENV, "password")
    if auth == "entra":
        if url.password:
            raise RuntimeError("entra auth: the DSN file must not hold a password")
        url = url.set(password=_entra_token())
    elif auth != "password":
        raise RuntimeError(f"{AUTH_ENV} must be 'password' or 'entra'")
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
        _apply_mapping_approvers(connection)
    engine.dispose()


def _apply_mapping_approvers(connection: Connection) -> None:
    value = os.environ.get(APPROVERS_ENV)
    if value is None or context.config.attributes.get("dsn"):
        return  # unset, or a test harness (which sets the policy itself)
    if value not in {"1", "2"}:
        raise RuntimeError(f"{APPROVERS_ENV} must be 1 or 2, got {value!r}")
    changed = connection.execute(
        text("UPDATE access.mapping_policy SET approvers = :n, updated_at = now()"
             " WHERE approvers <> :n"),
        {"n": int(value)},
    ).rowcount
    if changed:
        connection.execute(
            text("INSERT INTO access.change_log (actor, action, detail)"
                 " VALUES ('migration:config', 'set_mapping_approvers',"
                 " jsonb_build_object('approvers', CAST(:n AS integer)))"),
            {"n": int(value)},
        )
    connection.commit()


if context.is_offline_mode():
    raise RuntimeError("offline (--sql) migrations are not supported")
run_migrations_online()
