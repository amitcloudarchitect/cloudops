"""Turning telemetry on for one process. Explicit, eager, and fatal when incomplete.

    telemetry = init("api", os.environ)      # first thing in main()
    ...
    telemetry.shutdown()                     # flushes on the way out

``CLOUDOPS_TELEMETRY`` selects the backend, in the spirit of ``cloudops_config``:

- ``otlp``: traces, metrics and logs to the in-cluster OpenTelemetry Collector
  at ``OTEL_EXPORTER_OTLP_ENDPOINT`` (OTLP over HTTP). The collector samples,
  scrubs again, and exports to Application Insights and managed Prometheus.
- ``off``: JSON logs to stdout only, no-op traces and metrics. The local default.

In the Azure profile the choice must be explicit (unset refuses to start), and
``off`` is accepted only where ``CLOUDOPS_DEPLOYMENT`` is ``dev``: dev runs
without the collector until its identity exists; test, UAT and production never
run unobserved.

Logs are JSON, scrubbed, in both. Sampling is ``always_on`` here on purpose:
the collector's tail sampler decides (``tracing``), because only it can see
whether a trace ended in an error.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from opentelemetry import metrics, propagate, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from cloudops_observability.logs import configure_logging
from cloudops_observability.security import SecurityEventCounter
from cloudops_observability.tracing import ScrubbingSpanExporter

BACKEND_VAR: Final = "CLOUDOPS_TELEMETRY"
ENDPOINT_VAR: Final = "OTEL_EXPORTER_OTLP_ENDPOINT"
ENV_VAR: Final = "CLOUDOPS_ENV"
DEPLOYMENT_VAR: Final = "CLOUDOPS_DEPLOYMENT"  # dev, tst, uat, prd
VERSION_VAR: Final = "CLOUDOPS_VERSION"
METRIC_INTERVAL_MS: Final = 30_000


class TelemetryConfigError(Exception):
    """Telemetry is misconfigured; the process must not start."""


class Backend(enum.StrEnum):
    OTLP = "otlp"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    service: str
    backend: Backend
    endpoint: str | None
    deployment: str
    version: str

    @classmethod
    def from_environ(cls, service: str, environ: Mapping[str, str]) -> TelemetryConfig:
        azure = environ.get(ENV_VAR) == "azure"
        deployment = environ.get(DEPLOYMENT_VAR, "").strip()
        raw = environ.get(BACKEND_VAR, "").strip()
        if not raw:
            if azure:
                raise TelemetryConfigError(f"{BACKEND_VAR} is unset; the Azure profile must say")
            raw = Backend.OFF.value
        try:
            backend = Backend(raw)
        except ValueError:
            raise TelemetryConfigError(f"{BACKEND_VAR} must be 'otlp' or 'off'") from None
        if azure and backend is Backend.OFF and deployment != "dev":
            raise TelemetryConfigError("only dev may run without telemetry")
        endpoint = environ.get(ENDPOINT_VAR, "").strip() or None
        if backend is Backend.OTLP and endpoint is None:
            raise TelemetryConfigError(f"{ENDPOINT_VAR} is required with 'otlp'")
        return cls(service=service, backend=backend, endpoint=endpoint,
                   deployment=deployment or "local",
                   version=environ.get(VERSION_VAR, "unknown").strip() or "unknown")


@dataclass
class Telemetry:
    config: TelemetryConfig
    _tracer: TracerProvider | None = None
    _meter: MeterProvider | None = None
    _logger: LoggerProvider | None = None
    _handlers: list[logging.Handler] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.config.backend is Backend.OTLP

    def signal_endpoint(self, signal: str) -> str:
        """The OTLP/HTTP URL for one signal, e.g. for Temporal's metrics."""
        if self.config.endpoint is None:
            raise TelemetryConfigError("telemetry is off")
        return f"{self.config.endpoint.rstrip('/')}/v1/{signal}"

    def shutdown(self) -> None:
        for provider in (self._tracer, self._meter, self._logger):
            if provider is not None:
                provider.shutdown()


def init(service: str, environ: Mapping[str, str], *, level: int = logging.INFO) -> Telemetry:
    """Configure logging, and traces and metrics when the backend is ``otlp``."""
    config = TelemetryConfig.from_environ(service, environ)
    # Trace context only, never baggage: baggage travels with a request and
    # could carry anything a caller puts in it.
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    telemetry = Telemetry(config)
    handlers: list[logging.Handler] = []
    if telemetry.enabled:
        resource = Resource.create({
            "service.name": service, "service.namespace": "cloudops",
            "service.version": config.version, "deployment.environment.name": config.deployment,
        })
        tracer = TracerProvider(resource=resource, sampler=ALWAYS_ON)
        tracer.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(
            OTLPSpanExporter(endpoint=telemetry.signal_endpoint("traces")))))
        trace.set_tracer_provider(tracer)
        meter = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=telemetry.signal_endpoint("metrics")),
            export_interval_millis=METRIC_INTERVAL_MS)])
        metrics.set_meter_provider(meter)
        logger = LoggerProvider(resource=resource)
        logger.add_log_record_processor(BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=telemetry.signal_endpoint("logs"))))
        set_logger_provider(logger)
        handlers.append(LoggingHandler(level=logging.INFO, logger_provider=logger))
        telemetry._tracer, telemetry._meter, telemetry._logger = tracer, meter, logger
    handlers.append(SecurityEventCounter())
    configure_logging(service, level=level, extra_handlers=tuple(handlers))
    return telemetry


__all__ = ["Backend", "Telemetry", "TelemetryConfig", "TelemetryConfigError", "init"]
