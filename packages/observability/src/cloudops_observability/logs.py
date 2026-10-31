"""Structured JSON logs, scrubbed before any handler sees them.

    {"ts": "2026-10-31T09:14:02.113Z", "level": "WARNING", "logger": "cloudops.api",
     "service": "api", "msg": "no scope: unknown principal oid=[id] request=[id]",
     "trace_id": "4bf9…", "span_id": "00f0…", "request_id": "…"}

``ScrubFilter`` sits on every handler ``configure_logging`` installs (stdout
and, when telemetry is on, OpenTelemetry to Log Analytics). It rewrites the
record in place: the message is formatted and scrubbed (``scrub.scrub_text``),
an exception becomes its type, SQLSTATE or HTTP status and frames, never its
message (``scrub.describe_exception``), and every ``extra`` field not on the
allowlist (``scrub.allowed_key``) is removed. So ``extra={"tenant_id": t}``
never leaves the process, and neither does ``"... oid=%s" % oid``.

Nothing here can recognise a customer *name* in free text. Messages name
identifiers, codes and counts, never a customer (CLAUDE.md, Errors).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

from opentelemetry import trace

from cloudops_observability.scrub import allowed_key, describe_exception, scrub_text

# Attributes every LogRecord has; anything else on a record came from ``extra``.
_STANDARD: Final = frozenset(logging.LogRecord(
    "", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}
SCRUBBED: Final = "_cloudops_scrubbed"


def extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items()
            if k not in _STANDARD and not k.startswith("_") and k != SCRUBBED}


class ScrubFilter(logging.Filter):
    """Scrub a record once, in place, before any handler formats or exports it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, SCRUBBED, False):
            return True
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not lose the record
            message = str(record.msg)
        if record.exc_info:
            exc = record.exc_info[1]
            if exc is not None:
                # Type, SQLSTATE or HTTP status, and frames: never the message.
                message += "\n" + describe_exception(exc)
            record.exc_info = None
            record.exc_text = None
        if record.stack_info:
            record.stack_info = None
        record.msg = scrub_text(message)
        record.args = None
        for key, value in extras(record).items():
            if not allowed_key(key):
                delattr(record, key)
            elif isinstance(value, str):
                setattr(record, key, scrub_text(value))
            elif not isinstance(value, bool | int | float):
                setattr(record, key, scrub_text(str(value)))
        setattr(record, SCRUBBED, True)
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
                  .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "msg": record.getMessage(),
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
            entry["span_id"] = format(span.span_id, "016x")
        entry.update(extras(record))
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(service: str, *, level: int = logging.INFO,
                      extra_handlers: tuple[logging.Handler, ...] = ()) -> None:
    """Replace the root handlers with JSON to stdout (plus ``extra_handlers``),
    each behind ``ScrubFilter``. Idempotent: calling twice leaves one set."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(JsonFormatter(service))
    for handler in (stdout, *extra_handlers):
        handler.addFilter(ScrubFilter())
        root.addHandler(handler)
    root.setLevel(level)
    # Libraries that log request URLs or bodies at INFO: their URLs carry
    # customer hosts and paths. Warnings and errors still come through, scrubbed.
    for noisy in ("httpx", "httpcore", "azure", "urllib3", "temporalio.activity"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


__all__ = ["JsonFormatter", "ScrubFilter", "configure_logging", "extras"]
