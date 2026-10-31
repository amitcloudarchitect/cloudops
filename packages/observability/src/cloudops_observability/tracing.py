"""Traces: export through the scrubber, sampling hints, the per-ticket trace.

Sampling is decided by the collector, not here (ADR 0022). Every span is sent;
the collector's tail sampler keeps 100% of traces with an error, 100% of traces
containing an execution span, and 10% of the rest by trace id. A trace id
always gets the same probabilistic decision, so a ticket's trace, whose spans
arrive over days, is kept or dropped as a whole.

**Execution** spans carry ``cloudops.execution = true`` (``mark_execution``),
and execution workflow classes are named ``Execute*``; the collector keeps both.

**One trace per ticket.** A ticket lives longer than any request or workflow:
seen by the connector, triaged, updated, closed, days apart. Its first sighting
starts a root span ``ticket`` whose W3C ``traceparent`` is stored on the
ticket's row (``cloudops.work_item.trace_parent``, under RLS). Every later
stage opens its span under that parent (``ticket_span``), so the ticket from
first sighting to closure is one trace. The traceparent is random; it is not
derived from the tenant or the ticket number, so it says nothing about either.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Final

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Span, SpanContext, Status
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import AttributeValue

from cloudops_observability.scrub import scrub_attributes, scrub_text

TRACER_NAME: Final = "cloudops"
EXECUTION: Final = "cloudops.execution"
EXECUTION_WORKFLOW_PREFIX: Final = "Execute"
TICKET_STAGE: Final = "cloudops.ticket.stage"

_TRACEPARENT: Final = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_KEPT_EVENTS: Final = ("exception",)


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def current_trace_id() -> str | None:
    """The current span's trace id as 32 hex digits, or None outside a span."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def mark_execution(span: Span | None = None) -> None:
    """Tell the collector to keep this whole trace (execution: always sampled)."""
    (span or trace.get_current_span()).set_attribute(EXECUTION, True)


# --- the per-ticket trace ------------------------------------------------------------


def traceparent(span: Span) -> str | None:
    """The span's W3C traceparent; None when tracing is off (a no-op span)."""
    if not span.get_span_context().is_valid:
        return None
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier, trace.set_span_in_context(span))
    return carrier.get("traceparent")


def parent_from(value: str | None) -> SpanContext | None:
    """The span context in a stored traceparent; None if absent or malformed."""
    if not value or not _TRACEPARENT.match(value):
        return None
    ctx = trace.get_current_span(
        TraceContextTextMapPropagator().extract({"traceparent": value})).get_span_context()
    return ctx if ctx.is_valid else None


@contextmanager
def ticket_span(stage: str, parent: str | None,
                attributes: Mapping[str, AttributeValue] | None = None) -> Iterator[Span]:
    """A span for one stage of a ticket's life, in that ticket's trace.

    ``parent`` is the ticket's stored traceparent. Without one (first sighting,
    or a row from before tracing existed) the span is the ticket's root: read
    ``traceparent(span)`` and store it. The span is linked to whatever was
    current (a connector batch, a triage workflow), so either trace leads to
    the other.
    """
    current = trace.get_current_span().get_span_context()
    links = [Link(current)] if current.is_valid else []
    ctx = parent_from(parent)
    base = (trace.set_span_in_context(trace.NonRecordingSpan(ctx)) if ctx is not None
            else otel_context.Context())
    attrs: dict[str, AttributeValue] = {TICKET_STAGE: stage, **(attributes or {})}
    with tracer().start_as_current_span(
            "ticket" if ctx is None else f"ticket.{stage}", context=base, links=links,
            attributes=attrs) as span:
        yield span


# --- export ------------------------------------------------------------------------------


def scrub_span(span: ReadableSpan) -> ReadableSpan:
    """A copy of ``span`` with only allowlisted, scrubbed attributes and events."""
    events = [Event(e.name, scrub_attributes(e.attributes), e.timestamp)
              for e in span.events if e.name in _KEPT_EVENTS or e.name.startswith("cloudops.")]
    links = [Link(link.context, scrub_attributes(link.attributes)) for link in span.links]
    # The status keeps its code, never its description: the description is an
    # exception's message (ADR 0022).
    status = Status(span.status.status_code) if span.status.description else span.status
    return ReadableSpan(
        name=scrub_text(span.name), context=span.context, parent=span.parent,
        resource=span.resource, attributes=scrub_attributes(span.attributes), events=events,
        links=links, kind=span.kind, status=status, start_time=span.start_time,
        end_time=span.end_time, instrumentation_scope=span.instrumentation_scope,
    )


class ScrubbingSpanExporter(SpanExporter):
    """Wraps the real exporter: nothing reaches it unscrubbed."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([scrub_span(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


__all__ = ["EXECUTION", "EXECUTION_WORKFLOW_PREFIX", "TICKET_STAGE", "TRACER_NAME",
           "ScrubbingSpanExporter", "current_trace_id", "mark_execution",
           "parent_from", "scrub_span", "ticket_span", "traceparent", "tracer"]
