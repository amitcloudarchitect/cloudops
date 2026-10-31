"""Application metrics. Labels are low-cardinality and never name a tenant.

Golden signals come from instrumentation, not from here: HTTP server duration
and status (FastAPI), outbound HTTP (httpx), Temporal workflow and activity
latency and failures (the SDK's own metrics, ``temporal.runtime``). This module
holds what instrumentation cannot know: AI usage and cost, connector runs,
reconciliation checks.

Money is never a float (CLAUDE.md rule 5). Cost is counted as an integer number
of millionths of its currency (``*_micros``), exact at ``numeric(18,6)``, with
the currency as a label. Dashboards divide by 1e6 for display only.

Instruments are created on first use from the global meter provider, so code
that records a metric works unchanged with telemetry off (a no-op provider).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from typing import Final

from opentelemetry import metrics
from opentelemetry.sdk.metrics.view import View

METER: Final = "cloudops"
_MICRO: Final = Decimal("0.000001")


def micros(amount: Decimal) -> int:
    """``amount`` in millionths, exactly (half-up beyond six places)."""
    return int((amount / _MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@cache
def _meter() -> metrics.Meter:
    return metrics.get_meter(METER)


@cache
def _counter(name: str, unit: str, description: str) -> metrics.Counter:
    return _meter().create_counter(name, unit=unit, description=description)


@cache
def _histogram(name: str, unit: str, description: str) -> metrics.Histogram:
    return _meter().create_histogram(name, unit=unit, description=description)


# Metric attributes are allowlisted per instrument family, like span attributes
# (ADR 0022). Instrumentation libraries label HTTP metrics with server.address
# and url.*, which for an outbound call name a customer's host; a View keeps
# only the keys listed here. Our own instruments (cloudops.*) are labelled by
# this package alone, from fixed vocabularies.
_HTTP_KEYS: Final = frozenset({
    "http.request.method", "http.response.status_code", "error.type", "url.scheme",
    "network.protocol.name", "network.protocol.version",
})
_HTTP_SERVER_KEYS: Final = _HTTP_KEYS | {"http.route"}


def metric_views() -> list[View]:
    """The views every MeterProvider in this codebase is built with."""
    return [
        View(instrument_name="http.client.*", attribute_keys=set(_HTTP_KEYS)),
        View(instrument_name="http.server.*", attribute_keys=set(_HTTP_SERVER_KEYS)),
    ]


def reset_instruments() -> None:
    """For tests that swap the meter provider."""
    _meter.cache_clear()
    _counter.cache_clear()
    _histogram.cache_clear()


# --- AI ----------------------------------------------------------------------------------


def record_llm_call(*, task: str, tier: str, outcome: str, provider: str | None,
                    cache_hit: bool, tokens_in: int, tokens_out: int, cost: Decimal,
                    currency: str, latency_ms: int | None, refusal: str | None = None) -> None:
    """One gateway call: count, tokens, cost and latency by task and tier.
    ``refusal`` is the gateway's refusal code (``spend_circuit_open`` is the AI
    spend circuit breaker, a P2 page): a short fixed vocabulary, never free text."""
    labels: dict[str, str | bool] = {"task": task, "tier": tier, "outcome": outcome,
                                      "provider": provider or "none", "cache_hit": cache_hit,
                                      "refusal": refusal or "none"}
    _counter("cloudops.llm.calls", "{call}", "AI gateway calls").add(1, labels)
    usage = {"task": task, "tier": tier}
    tokens = _counter("cloudops.llm.tokens", "{token}", "Tokens through the AI gateway")
    if tokens_in:
        tokens.add(tokens_in, {**usage, "direction": "input"})
    if tokens_out:
        tokens.add(tokens_out, {**usage, "direction": "output"})
    if cost:
        _counter("cloudops.llm.cost_micros", "{micro}",
                 "AI spend in millionths of its currency").add(
            micros(cost), {**usage, "currency": currency})
    if latency_ms is not None:
        _histogram("cloudops.llm.duration", "ms", "AI gateway call latency").record(
            latency_ms, {**usage, "outcome": outcome})


def record_agent_run(*, agent: str, status: str, latency_ms: int) -> None:
    labels = {"agent": agent, "status": status}
    _counter("cloudops.agent.runs", "{run}", "Agent runs by outcome").add(1, labels)
    _histogram("cloudops.agent.duration", "ms", "Agent run latency").record(latency_ms, labels)


# --- connectors and FinOps ------------------------------------------------------------------


def record_connector_run(*, source: str, entity: str, mode: str, status: str, records: int,
                         written: int, rejected: int, quarantined: int) -> None:
    """A finished connector run (the gauges of health come from the database)."""
    labels = {"source": source, "entity": entity, "mode": mode}
    _counter("cloudops.connector.runs", "{run}", "Connector runs by outcome").add(
        1, {**labels, "status": status})
    records_c = _counter("cloudops.connector.records", "{record}", "Connector records")
    for kind, n in (("read", records), ("written", written), ("rejected", rejected),
                    ("quarantined", quarantined)):
        if n:
            records_c.add(n, {**labels, "kind": kind})


def record_reconciliation(*, kind: str, status: str, reason: str | None) -> None:
    """One reconciliation check (LLD: drift above 0.5% pages, P2)."""
    _counter("cloudops.finops.reconciliation_checks", "{check}",
             "Cost reconciliation checks by verdict").add(
        1, {"kind": kind, "status": status, "reason": reason or "none"})


__all__ = ["METER", "metric_views", "micros", "record_agent_run", "record_connector_run",
           "record_llm_call", "record_reconciliation", "reset_instruments"]
