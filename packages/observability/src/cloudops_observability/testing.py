"""In-memory telemetry for tests, installed once per process.

OpenTelemetry's global providers can be set only once, so every test that
looks at spans or metrics shares these. Spans pass through the same scrubbing
exporter as production, so a test sees exactly what would leave the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cloudops_observability.tracing import ScrubbingSpanExporter


@dataclass(frozen=True)
class MemoryTelemetry:
    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader

    def finished(self) -> tuple[ReadableSpan, ...]:
        return self.spans.get_finished_spans()

    def points(self, name: str) -> list[tuple[dict[str, object], float | int]]:
        """Every data point of metric ``name`` (dotted name), with its attributes."""
        out: list[tuple[dict[str, object], float | int]] = []
        data = self.metrics.get_metrics_data()
        for rm in data.resource_metrics if data else ():
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == name:
                        for p in m.data.data_points:
                            value = getattr(p, "value", None)
                            if value is None:
                                value = getattr(p, "sum", None) or 0
                            out.append((dict(p.attributes or {}), float(value)))
        return out

    def total(self, name: str, **labels: object) -> float | int:
        return sum(v for attrs, v in self.points(name)
                   if all(attrs.get(k) == want for k, want in labels.items()))


@cache
def memory_telemetry() -> MemoryTelemetry:
    spans = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(spans)))
    trace.set_tracer_provider(tracer)
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return MemoryTelemetry(spans, reader)


__all__ = ["MemoryTelemetry", "memory_telemetry"]
