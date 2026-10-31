"""A client span on every statement this package sends (step 19; ADR 0021).

The engine is private to this package, so this package instruments it: a span
per statement, named for its operation, holding the statement text. Statements
here carry bind markers, never values, and parameters are never read. The
scope's own statements (``set_config``, the commit check) are spans too: a
trace shows where scope was applied.

OpenTelemetry's API only: with no SDK configured, every span is a no-op. The
services' exporter scrubs what leaves the process (``cloudops_observability``).
"""

from __future__ import annotations

from typing import Any, Final

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine, ExceptionContext
from sqlalchemy.engine.interfaces import ExecutionContext

_SPAN: Final = "_cloudops_span"
_tracer: Final = trace.get_tracer("cloudops.tenancy")


def _operation(statement: str) -> str:
    head = statement.lstrip().split(None, 1)
    return head[0].upper() if head else "SQL"


def _before(conn: Connection, cursor: Any, statement: str, parameters: Any,
            context: ExecutionContext | None, executemany: bool) -> None:
    if context is None:
        return
    op = _operation(statement)
    setattr(context, _SPAN, _tracer.start_span(
        f"db {op}", kind=SpanKind.CLIENT,
        attributes={"db.system.name": "postgresql", "db.operation.name": op,
                    "db.query.text": statement}))


def _after(conn: Connection, cursor: Any, statement: str, parameters: Any,
           context: ExecutionContext | None, executemany: bool) -> None:
    span = getattr(context, _SPAN, None)
    if span is not None:
        span.end()


def _error(ctx: ExceptionContext) -> None:
    span = getattr(ctx.execution_context, _SPAN, None)
    if span is None:
        return
    err = ctx.original_exception
    span.set_attribute("error.type", getattr(err, "sqlstate", None) or type(err).__name__)
    span.set_status(Status(StatusCode.ERROR))
    span.end()


def instrument(engine: Engine) -> None:
    event.listen(engine, "before_cursor_execute", _before)
    event.listen(engine, "after_cursor_execute", _after)
    event.listen(engine, "handle_error", _error)
