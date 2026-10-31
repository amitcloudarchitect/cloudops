"""Temporal: trace context through workflows and activities, and the SDK's metrics.

``interceptors()`` propagates the caller's trace into the workflow and from the
workflow into every activity (in Temporal headers), so a request that starts a
workflow and the activities it runs are one trace. ``runtime()`` sends the
SDK's own metrics — workflow and activity latency, failures, task queue
polling — to the collector: the golden signals for a worker.

Both are empty when telemetry is off. Needs the ``temporal`` extra.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.client import Interceptor
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.runtime import OpenTelemetryConfig, Runtime, TelemetryConfig

from cloudops_observability.setup import Telemetry


def interceptors(telemetry: Telemetry) -> list[Interceptor]:
    return [TracingInterceptor()] if telemetry.enabled else []


def runtime(telemetry: Telemetry) -> Runtime | None:
    if not telemetry.enabled:
        return None
    return Runtime(telemetry=TelemetryConfig(
        metrics=OpenTelemetryConfig(url=telemetry.signal_endpoint("metrics"), http=True,
                                    metric_periodicity=timedelta(seconds=30),
                                    durations_as_seconds=True),
        global_tags={"service.name": telemetry.config.service}))


__all__ = ["interceptors", "runtime"]
