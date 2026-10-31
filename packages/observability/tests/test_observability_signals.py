"""Spans, logs, metrics and configuration, as they would leave the process."""

from __future__ import annotations

import io
import json
import logging
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from cloudops_observability import (
    SecurityEvent,
    TelemetryConfigError,
    current_trace_id,
    mark_execution,
    micros,
    record_llm_call,
    record_security_event,
    ticket_span,
    traceparent,
    tracer,
)
from cloudops_observability.gauges import Gauge, GaugeSet, Snapshot
from cloudops_observability.http import StripTraceHeaders
from cloudops_observability.logs import JsonFormatter, ScrubFilter
from cloudops_observability.security import SecurityEventCounter
from cloudops_observability.setup import Backend, TelemetryConfig
from cloudops_observability.testing import MemoryTelemetry, memory_telemetry
from cloudops_observability.tracing import EXECUTION, parent_from

TENANT = str(uuid4())


@pytest.fixture
def mem() -> MemoryTelemetry:
    m = memory_telemetry()
    m.spans.clear()
    return m


# --- traces ------------------------------------------------------------------------------


def test_exported_spans_are_scrubbed(mem: MemoryTelemetry) -> None:
    with tracer().start_as_current_span(f"load {TENANT}", attributes={
            "tenant_id": TENANT, "http.route": "/v1/work/{number}",
            "url.full": "https://acme.service-now.com/x"}) as span:
        span.add_event("debug", {"tenant_id": TENANT})
        span.record_exception(ValueError(f"no row for tenant {TENANT}"))
        span.set_status(StatusCode.ERROR, f"failed for {TENANT}")
    (out,) = mem.finished()
    blob = repr((out.name, dict(out.attributes or {}), [(e.name, dict(e.attributes or {}))
                 for e in out.events], out.status.description))
    assert TENANT not in blob
    assert dict(out.attributes or {}) == {"http.route": "/v1/work/{number}"}
    assert [e.name for e in out.events] == ["exception"]
    assert out.status.status_code is StatusCode.ERROR


def test_execution_marker(mem: MemoryTelemetry) -> None:
    with tracer().start_as_current_span("ExecuteRunbook"):
        mark_execution()
    (out,) = mem.finished()
    assert (out.attributes or {})[EXECUTION] is True


def test_ticket_stages_share_one_trace(mem: MemoryTelemetry) -> None:
    with tracer().start_as_current_span("batch") as batch, \
            ticket_span("ingested", None) as root:
        stored = traceparent(root)
    with tracer().start_as_current_span("triage-workflow"), ticket_span("triaged", stored):
        pass
    with ticket_span("closed", stored):
        pass
    spans = {s.name: s for s in mem.finished()}
    trace_ids = {spans[n].context.trace_id for n in ("ticket", "ticket.triaged", "ticket.closed")}
    assert len(trace_ids) == 1
    assert spans["ticket"].context.trace_id != batch.get_span_context().trace_id
    assert spans["ticket"].parent is None
    assert spans["ticket.triaged"].parent.span_id == spans["ticket"].context.span_id  # type: ignore[union-attr]
    # Each stage links back to the work that produced it.
    assert spans["ticket"].links[0].context.trace_id == batch.get_span_context().trace_id
    assert spans["ticket.triaged"].links[0].context.trace_id == \
        spans["triage-workflow"].context.trace_id
    assert spans["ticket.closed"].links == ()


@pytest.mark.parametrize("bad", [None, "", "garbage", "01-" + "a" * 32 + "-" + "b" * 16 + "-01",
                                 "00-" + "0" * 32 + "-" + "b" * 16 + "-01"])
def test_malformed_traceparent_starts_a_new_root(bad: str | None) -> None:
    assert parent_from(bad) is None


def test_current_trace_id(mem: MemoryTelemetry) -> None:
    assert current_trace_id() is None
    with tracer().start_as_current_span("x") as span:
        assert current_trace_id() == format(span.get_span_context().trace_id, "032x")


# --- logs --------------------------------------------------------------------------------


def _log(record_fn: Any) -> dict[str, Any]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter("test"))
    handler.addFilter(ScrubFilter())
    logger = logging.getLogger("cloudops.test.logs")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        record_fn(logger)
    finally:
        logger.removeHandler(handler)
    return dict(json.loads(buf.getvalue()))


def test_log_records_are_scrubbed() -> None:
    entry = _log(lambda log: log.warning(
        "no scope: oid=%s token=%s", TENANT, "abcd1234efgh",
        extra={"tenant_id": TENANT, "customer": "Acme", "request_id": "r-1", "code": "no_scope"}))
    assert TENANT not in json.dumps(entry)
    assert "abcd1234efgh" not in entry["msg"]
    assert "tenant_id" not in entry
    assert "customer" not in entry
    assert entry["request_id"] == "r-1"
    assert entry["code"] == "no_scope"
    assert entry["level"] == "WARNING"
    assert entry["service"] == "test"


def test_exceptions_are_scrubbed_text() -> None:
    def boom(log: logging.Logger) -> None:
        try:
            raise RuntimeError(f"tenant {TENANT} password=hunter2pass")
        except RuntimeError:
            log.exception("handler failed")
    entry = _log(boom)
    assert "RuntimeError" in entry["msg"]
    assert TENANT not in entry["msg"]
    assert "hunter2pass" not in entry["msg"]


def test_logs_carry_the_trace(mem: MemoryTelemetry) -> None:
    with tracer().start_as_current_span("x") as span:
        entry = _log(lambda log: log.info("hello"))
    assert entry["trace_id"] == format(span.get_span_context().trace_id, "032x")


# --- metrics -----------------------------------------------------------------------------


def test_security_events_are_logged_and_counted(mem: MemoryTelemetry,
                                               caplog: pytest.LogCaptureFixture) -> None:
    counter = SecurityEventCounter()
    logging.getLogger("cloudops.security").addHandler(counter)
    before = mem.total("cloudops.security.events", event="cross_tenant_attempt")
    try:
        with caplog.at_level(logging.CRITICAL, logger="cloudops.security"):
            record_security_event(SecurityEvent.CROSS_TENANT_ATTEMPT, "test", "guard refused")
    finally:
        logging.getLogger("cloudops.security").removeHandler(counter)
    (rec,) = caplog.records
    assert rec.levelno == logging.CRITICAL
    assert rec.security_event == "cross_tenant_attempt"  # type: ignore[attr-defined]
    assert mem.total("cloudops.security.events", event="cross_tenant_attempt") == before + 1


def test_tenancy_emits_the_same_event_names() -> None:
    from cloudops_tenancy import database
    assert SecurityEvent.CROSS_TENANT_ATTEMPT.value == database.CROSS_TENANT_EVENT


@pytest.mark.parametrize(("amount", "expected"), [
    (Decimal("0.000840"), 840), (Decimal("12.5"), 12_500_000), (Decimal("0.0000005"), 1),
    (Decimal("0"), 0)])
def test_micros_are_exact(amount: Decimal, expected: int) -> None:
    assert micros(amount) == expected


def test_llm_cost_is_counted_per_currency_in_micros(mem: MemoryTelemetry) -> None:
    def total(ccy: str) -> float | int:
        return mem.total("cloudops.llm.cost_micros", task="t-cost", currency=ccy)
    eur, usd = total("EUR"), total("USD")
    for cost, ccy in ((Decimal("0.000840"), "EUR"), (Decimal("0.001200"), "USD"),
                      (Decimal("0.000160"), "EUR")):
        record_llm_call(task="t-cost", tier="small", outcome="succeeded", provider="azure",
                        cache_hit=False, tokens_in=100, tokens_out=20, cost=cost, currency=ccy,
                        latency_ms=420)
    assert total("EUR") - eur == 1000
    assert total("USD") - usd == 1200
    labels = {k for attrs, _ in mem.points("cloudops.llm.calls") for k in attrs}
    assert not labels & {"tenant", "tenant_id", "principal", "user"}


def test_gauges_publish_snapshots(mem: MemoryTelemetry) -> None:
    gauges = GaugeSet([Gauge("cloudops.test.depth", "{row}", "test")])
    snap = Snapshot()
    snap.add("cloudops.test.depth", 7, {"source": "servicenow"})
    gauges.publish(snap)
    assert mem.points("cloudops.test.depth") == [({"source": "servicenow"}, 7)]
    bad = Snapshot()
    bad.add("cloudops.test.other", 1, {})
    with pytest.raises(ValueError, match="unregistered"):
        gauges.publish(bad)


# --- configuration -----------------------------------------------------------------------


def test_local_defaults_to_off() -> None:
    cfg = TelemetryConfig.from_environ("api", {"CLOUDOPS_ENV": "local"})
    assert cfg.backend is Backend.OFF


@pytest.mark.parametrize("environ", [
    {"CLOUDOPS_ENV": "azure", "CLOUDOPS_DEPLOYMENT": "dev"},
    {"CLOUDOPS_ENV": "azure", "CLOUDOPS_TELEMETRY": "off"},
    {"CLOUDOPS_ENV": "azure", "CLOUDOPS_TELEMETRY": "off", "CLOUDOPS_DEPLOYMENT": "prd"},
    {"CLOUDOPS_ENV": "azure", "CLOUDOPS_TELEMETRY": "otlp"},
    {"CLOUDOPS_ENV": "local", "CLOUDOPS_TELEMETRY": "console"},
])
def test_incomplete_configuration_refuses(environ: dict[str, str]) -> None:
    with pytest.raises(TelemetryConfigError):
        TelemetryConfig.from_environ("api", environ)


def test_dev_may_run_without_the_collector() -> None:
    cfg = TelemetryConfig.from_environ("api", {
        "CLOUDOPS_ENV": "azure", "CLOUDOPS_TELEMETRY": "off", "CLOUDOPS_DEPLOYMENT": "dev"})
    assert cfg.backend is Backend.OFF


def test_azure_with_collector() -> None:
    cfg = TelemetryConfig.from_environ("api", {
        "CLOUDOPS_ENV": "azure", "CLOUDOPS_TELEMETRY": "otlp", "CLOUDOPS_DEPLOYMENT": "dev",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector.observability:4318"})
    assert (cfg.backend, cfg.deployment) == (Backend.OTLP, "dev")


# --- http --------------------------------------------------------------------------------


async def test_inbound_trace_headers_are_stripped() -> None:
    seen: list[list[tuple[bytes, bytes]]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope["headers"])

    await StripTraceHeaders(app)({"type": "http", "headers": [
        (b"traceparent", b"00-" + b"a" * 32 + b"-" + b"b" * 16 + b"-01"),
        (b"Baggage", b"tenant=x"), (b"authorization", b"Bearer x")]}, None, None)
    assert seen == [[(b"authorization", b"Bearer x")]]


def test_no_tracer_provider_leaks_globally_unscrubbed() -> None:
    assert trace.get_tracer_provider() is not None


async def test_api_server_spans_and_metrics_use_the_stable_names(mem: MemoryTelemetry) -> None:
    """Stack 03's rules read http_server_request_duration{http_route,
    http_response_status_code}: the stable HTTP conventions, not the old ones."""
    import httpx
    from fastapi import FastAPI

    from cloudops_observability.http import instrument_fastapi

    app = FastAPI()

    @app.get("/v1/things/{thing}")
    async def thing(thing: str) -> dict[str, str]:
        return {"ok": thing}

    served = instrument_fastapi(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=served),
                                 base_url="http://test") as client:
        r = await client.get(f"/v1/things/{TENANT}",
                             headers={"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"})
    assert r.status_code == 200
    (span,) = [s for s in mem.finished() if s.kind.name == "SERVER"]
    assert span.context.trace_id != int("a" * 32, 16), "a client cannot choose our trace"
    attrs = dict(span.attributes or {})
    assert attrs["http.route"] == "/v1/things/{thing}"
    assert attrs["http.response.status_code"] == 200
    assert TENANT not in repr(attrs) + span.name
    assert mem.points("http.server.request.duration"), "the stable duration histogram"
