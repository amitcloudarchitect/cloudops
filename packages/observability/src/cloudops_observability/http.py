"""HTTP: server spans for the API, client spans for every outbound call.

The API is the edge: requests come from browsers over Front Door. A client must
not choose our trace ids or join our traces, so ``StripTraceHeaders`` removes
``traceparent``, ``tracestate`` and ``baggage`` before instrumentation reads
them, and every API request starts a trace of its own.

Outbound calls (connectors, the AI gateway) get client spans from the httpx
instrumentation. Their URLs name customer hosts and paths, so only the method,
status and an ``external`` host survive scrubbing (``scrub``).
"""

from __future__ import annotations

import os
from typing import Any, Final

# The stable HTTP semantic conventions (http.server.request.duration in seconds,
# http.route, http.response.status_code), which the alert rules and dashboards in
# stack 03 name. Read once, when the first instrumentor initialises: set before.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

_TRACE_HEADERS: Final = frozenset({b"traceparent", b"tracestate", b"baggage"})
# Probes and metrics scrapes are not requests worth a trace.
EXCLUDED_URLS: Final = "healthz,readyz,livez"


class StripTraceHeaders:
    """ASGI middleware: drop inbound trace context headers."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            headers = [(k, v) for k, v in scope.get("headers", [])
                       if k.lower() not in _TRACE_HEADERS]
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


def instrument_fastapi(app: Any) -> Any:
    """Instrument ``app`` and return the ASGI app to serve (headers stripped first)."""
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS,
                                       exclude_spans=["receive", "send"])
    return StripTraceHeaders(app)


def instrument_httpx() -> None:
    HTTPXClientInstrumentor().instrument()


__all__ = ["EXCLUDED_URLS", "StripTraceHeaders", "instrument_fastapi", "instrument_httpx"]
