"""OpenTelemetry for every CloudOps service (step 19; LLD, Observability; ADR 0022).

    from cloudops_observability import init
    telemetry = init("api", os.environ)

Nothing that identifies a customer leaves a process in telemetry: no tenant id,
no customer name, no principal, no customer host (``scrub``). Per-tenant
attribution lives in the RLS'd tables, joined by trace id.
"""

from cloudops_observability.metrics import (
    micros,
    record_agent_run,
    record_connector_run,
    record_llm_call,
    record_reconciliation,
)
from cloudops_observability.security import SecurityEvent, record_security_event
from cloudops_observability.setup import Backend, Telemetry, TelemetryConfigError, init
from cloudops_observability.tracing import (
    current_trace_id,
    mark_execution,
    ticket_span,
    traceparent,
    tracer,
)

__all__ = [
    "Backend",
    "SecurityEvent",
    "Telemetry",
    "TelemetryConfigError",
    "current_trace_id",
    "init",
    "mark_execution",
    "micros",
    "record_agent_run",
    "record_connector_run",
    "record_llm_call",
    "record_reconciliation",
    "record_security_event",
    "ticket_span",
    "traceparent",
    "tracer",
]
