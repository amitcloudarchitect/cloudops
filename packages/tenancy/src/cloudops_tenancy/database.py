"""The single entry point to the platform database.

    db = Database.create(dsn)
    async with db.transaction(scope) as conn:
        rows = await conn.execute(select(...))

There is no other way to obtain a connection. The engine is private to this
module, ``transaction`` is the only method that touches it, and every
connection it hands out is already inside a transaction with scope applied.
Outside this package, importing the driver or constructing an engine fails CI
(``tools/ci/check_data_access.py``).

Defence in depth, in order of strength:

1. The database role must not be able to bypass RLS. Each new physical
   connection is checked; superuser, BYPASSRLS, or membership of a role that
   owns tables is refused (``UnsafeDatabaseRoleError``). With RLS ``FORCE``d
   and an empty scope matching nothing, an unscoped connection sees zero rows.
2. Scope is applied inside the transaction, transaction-locally. A customer
   scope also switches to the ``cloudops_customer`` database role, whose
   column grants are the customer data boundary (ADR 0018).
3. Application statements cannot change scope or end the transaction
   (``_guard.check_statement``), and scope is re-verified before commit.
4. CI rejects any data access path outside this package.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final, final

from sqlalchemy import event, text
from sqlalchemy.engine import URL, Connection, CursorResult, ExceptionContext, Result, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection, ExecutionContext
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry
from sqlalchemy.sql import Executable

from cloudops_tenancy import _spans
from cloudops_tenancy._guard import check_statement
from cloudops_tenancy.errors import (
    ScopeClosedError,
    ScopeTamperingError,
    TenancyError,
    UnsafeDatabaseRoleError,
)
from cloudops_tenancy.scope import Role, TenantScope

# Security events (LLD, Observability: a cross-tenant query attempt always pages).
# Emitted with the standard library so this package needs no telemetry
# dependency; packages/observability counts them and stack 03's alert rule
# matches these exact fields. Never a tenant id, principal or statement.
_security_log: Final = logging.getLogger("cloudops.security")
CROSS_TENANT_EVENT: Final = "cross_tenant_attempt"
# PostgreSQL's insufficient_privilege, with the message row-level security uses
# when a write's WITH CHECK fails: a row for a tenant outside the scope.
_RLS_SQLSTATE: Final = "42501"
_RLS_MESSAGE: Final = "row-level security"


def _cross_tenant_attempt(source: str, message: str) -> None:
    _security_log.critical("SECURITY %s: %s", CROSS_TENANT_EVENT, message,
                           extra={"security_event": CROSS_TENANT_EVENT, "source": source})


def _rls_refusal(error: BaseException) -> bool:
    first = str(error).splitlines()[0] if str(error) else ""
    return getattr(error, "sqlstate", None) == _RLS_SQLSTATE and _RLS_MESSAGE in first


def _on_error(context: ExceptionContext) -> None:
    """Every statement error, whether or not the caller goes on to catch it."""
    if _rls_refusal(context.original_exception):
        _cross_tenant_attempt("tenancy.row_level_security",
                              "row-level security refused a write outside scope")


# Marks the package's own statements so the guard lets them through. Only this
# module sets it; ScopedConnection offers no way to pass execution options.
_INTERNAL: Final = "_cloudops_tenancy_internal"

# Why set_config(..., true) and why transaction-local, not session-level:
#
# Production reaches PostgreSQL through PgBouncer in *transaction* mode (LLD,
# Data tier). A server connection belongs to one client only for the length of
# one transaction, then goes back to the pool and is handed to whichever
# request — for whichever customer — asks next.
#
# A session-level `SET app.tenant_ids = ...` outlives the transaction and stays
# on that server connection. The next request to land on it inherits customer
# A's scope; if that request forgets to set its own, or sets it only partly, it
# reads customer A's data. SQLAlchemy's own pool and pre-ping do not save us:
# PgBouncer reuses server connections underneath them.
#
# `SET LOCAL` lasts until COMMIT or ROLLBACK and then reverts, so scope can
# never outlive the transaction that set it. `set_config(name, value, true)` is
# exactly SET LOCAL (the third argument is is_local) in function form, which
# lets the values travel as bind parameters instead of being spliced into SQL.
#
# Never change `true` to `false` here, and never add a plain SET anywhere.
_APPLY_SCOPE: Final = text(
    "SELECT set_config('app.tenant_ids', :tenant_ids, true),"
    "       set_config('app.role', :role, true),"
    "       set_config('app.account_ids', :account_ids, true)"
)

_READ_SCOPE: Final = text(
    "SELECT current_setting('app.tenant_ids', true), current_setting('app.role', true),"
    "       current_setting('app.account_ids', true),"
    "       CASE WHEN current_user = session_user THEN '' ELSE current_user::text END"
)

# A customer scope also runs as its own database role (ADR 0018). Row-level
# security decides which rows a scope sees but cannot hide a column, so the
# columns a customer may read are column-level grants to this role, and a table
# without a grant is refused outright. The login is a member WITH INHERIT FALSE
# (db/bootstrap/roles.sql): it can switch, and never otherwise holds the role's
# rights. Transaction-local for the same reason as the settings below: a
# session-level SET ROLE would hand the next pooled request a customer's role.
CUSTOMER_DB_ROLE: Final = "cloudops_customer"
_AS_CUSTOMER: Final = text(f"SET LOCAL ROLE {CUSTOMER_DB_ROLE}")

# One row: can this login reach a role that bypasses RLS, either directly or by
# SET ROLE, or that owns a table (owners can ALTER TABLE ... NO FORCE)?
_ROLE_SAFETY: Final = """
SELECT
  EXISTS (SELECT 1 FROM pg_roles r
          WHERE (r.rolsuper OR r.rolbypassrls)
            AND pg_has_role(current_user, r.oid, 'MEMBER')),
  EXISTS (SELECT 1 FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE c.relkind IN ('r', 'p')
            AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            AND n.nspname NOT LIKE 'pg\\_%'
            AND pg_has_role(current_user, c.relowner, 'MEMBER')),
  current_setting('row_security')
"""


@final
class ScopedConnection:
    """A connection that is inside a scoped transaction, and only that.

    Exposes statement execution and nothing else: no commit, rollback, begin,
    raw driver access or execution options. Dead once its transaction ends.
    """

    __slots__ = ("_conn", "_open", "_scope")

    def __init__(self, conn: AsyncConnection, scope: TenantScope) -> None:
        self._conn = conn
        self._scope = scope
        self._open = True

    @property
    def scope(self) -> TenantScope:
        return self._scope

    def _live(self) -> AsyncConnection:
        if not self._open:
            raise ScopeClosedError("scoped connection used after its transaction ended")
        return self._conn

    async def execute(
        self,
        statement: Executable,
        parameters: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    ) -> CursorResult[Any]:
        if not isinstance(statement, Executable):
            raise TypeError("statement must be a SQLAlchemy Executable; wrap raw SQL in text()")
        return await self._live().execute(statement, parameters)

    async def scalar(
        self, statement: Executable, parameters: Mapping[str, Any] | None = None
    ) -> Any:
        return (await self.execute(statement, parameters)).scalar()

    def _close(self) -> None:
        self._open = False


class Database:
    """Owns the connection pool. Hands out scoped transactions, nothing else."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine, *, _token: object = None) -> None:
        if _token is not _CONSTRUCT:
            raise TypeError("use Database.create() or Database.from_dsn_file()")
        self._engine = engine

    @classmethod
    def create(
        cls,
        dsn: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 0,
        pool_timeout: float = 10.0,
    ) -> Database:
        """Build the pool for ``dsn`` (a ``postgresql://`` URL).

        Connections are opened lazily; the role safety check runs on each new
        physical connection, so a misconfigured role fails the first
        transaction rather than silently serving unfiltered data.
        """
        url = _psycopg_url(dsn)
        engine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_pre_ping=True,
            pool_reset_on_return="rollback",
            # Server-side prepared statements are bound to a server connection,
            # which PgBouncer transaction mode swaps between transactions.
            connect_args={"prepare_threshold": None},
        )
        event.listen(engine.sync_engine, "connect", _verify_role_on_connect)
        event.listen(engine.sync_engine, "before_cursor_execute", _guard_statement)
        event.listen(engine.sync_engine, "handle_error", _on_error)
        _spans.instrument(engine.sync_engine)
        return cls(engine, _token=_CONSTRUCT)

    @classmethod
    def from_dsn_file(cls, path: Path, **pool: Any) -> Database:
        """Build from a DSN mounted as a file by the Key Vault CSI driver."""
        return cls.create(path.read_text(encoding="utf-8").strip(), **pool)

    @asynccontextmanager
    async def transaction(self, scope: TenantScope) -> AsyncIterator[ScopedConnection]:
        """Run one transaction under ``scope``. Commits on normal exit, rolls back on error."""
        if type(scope) is not TenantScope:
            raise TypeError("transaction() requires a TenantScope")
        async with self._engine.connect() as conn, conn.begin():
            await conn.execute(
                _APPLY_SCOPE,
                {
                    "tenant_ids": scope.tenant_ids_setting,
                    "role": scope.role.value,
                    "account_ids": scope.account_ids_setting,
                },
                execution_options={_INTERNAL: True},
            )
            if scope.role is Role.CUSTOMER:
                await conn.execute(_AS_CUSTOMER, execution_options={_INTERNAL: True})
            scoped = ScopedConnection(conn, scope)
            try:
                yield scoped
                await _assert_scope_intact(conn, scope)
            finally:
                scoped._close()

    async def ping(self) -> None:
        """Readiness: the database answers, under a scope that sees no tenant rows.

        Raises whatever the driver raises when it does not. Opens a pooled
        connection, so it also proves the login role passes the safety check.
        """
        async with self.transaction(TenantScope.for_resolution(_PING_PRINCIPAL)) as conn:
            await conn.scalar(_PING)

    async def dispose(self) -> None:
        await self._engine.dispose()


_CONSTRUCT: Final = object()
_PING_PRINCIPAL: Final = "probe:readyz"
_PING: Final = text("SELECT 1")


def _psycopg_url(dsn: str) -> URL:
    url = make_url(dsn)
    if url.get_backend_name() != "postgresql":
        raise TenancyError("tenancy supports PostgreSQL only")
    return url.set(drivername="postgresql+psycopg")


async def _assert_scope_intact(conn: AsyncConnection, scope: TenantScope) -> None:
    row = (
        await conn.execute(
            _READ_SCOPE, execution_options={_INTERNAL: True}
        )
    ).one()
    expected = (
        scope.tenant_ids_setting,
        scope.role.value,
        scope.account_ids_setting,
        CUSTOMER_DB_ROLE if scope.role is Role.CUSTOMER else "",
    )
    if (row[0], row[1], row[2], row[3]) != expected:
        _cross_tenant_attempt("tenancy.commit_check", "scope changed during the transaction")
        raise ScopeTamperingError("scope changed during the transaction; rolled back")


def _guard_statement(
    conn: Connection,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: ExecutionContext | None,
    executemany: bool,
) -> None:
    if context is not None and context.execution_options.get(_INTERNAL):
        return
    try:
        check_statement(statement)
    except ScopeTamperingError:
        _cross_tenant_attempt("tenancy.statement_guard",
                              "a statement tried to change scope or end the transaction")
        raise


def _verify_role_on_connect(dbapi_connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(_ROLE_SAFETY)
        row = cursor.fetchone()
    finally:
        cursor.close()
    dbapi_connection.rollback()
    if row is None:
        raise UnsafeDatabaseRoleError("role safety check returned no row")
    can_bypass, owns_tables, row_security = row
    if can_bypass:
        raise UnsafeDatabaseRoleError("database role is, or can become, superuser or BYPASSRLS")
    if owns_tables:
        raise UnsafeDatabaseRoleError("database role owns tables and could disable RLS")
    if row_security != "on":
        raise UnsafeDatabaseRoleError("row_security is not on for this connection")


__all__ = ["Database", "Result", "ScopedConnection"]
