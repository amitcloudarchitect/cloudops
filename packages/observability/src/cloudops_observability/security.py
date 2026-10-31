"""Security events: the signals behind the alerts that always page (LLD, Observability).

A security event is a log record on the ``cloudops.security`` logger at
CRITICAL with two structured fields, ``security_event`` (one of
``SecurityEvent``) and ``source`` (where in the code it was detected). The
Log Analytics alert rules in stack 03 match on exactly these fields, and
``SecurityEventCounter`` also counts them as ``cloudops_security_events_total``
for the dashboards. The record carries no tenant, principal or resource: the
on-call engineer pivots on the trace id to the RLS'd audit trail.

Packages below this one (``packages/tenancy``) emit the same record with the
standard library only, so they need no telemetry dependency; a test pins their
strings to this enum.

| Event                        | Emitted by                                         |
| ---------------------------- | -------------------------------------------------- |
| ``cross_tenant_attempt``     | tenancy: statement guard, scope changed before     |
|                              | commit, row-level security refused a write;        |
|                              | telemetry store wrapper refused a query            |
| ``unsigned_runbook``         | execution plane (not built; LLD forbids it before  |
| ``credential_mint_unapproved``| the security review). The rules exist and wait.   |
| ``signing_key_anomaly``      | Key Vault audit log (no code: a KQL rule, stack 03)|
| ``break_glass``              | Entra sign-in log (no code: a KQL rule, stack 03)  |
"""

from __future__ import annotations

import enum
import logging
from typing import Final

from opentelemetry import metrics

LOGGER: Final = "cloudops.security"
EVENT_FIELD: Final = "security_event"
SOURCE_FIELD: Final = "source"


class SecurityEvent(enum.StrEnum):
    CROSS_TENANT_ATTEMPT = "cross_tenant_attempt"
    UNSIGNED_RUNBOOK = "unsigned_runbook"
    CREDENTIAL_MINT_UNAPPROVED = "credential_mint_unapproved"
    SIGNING_KEY_ANOMALY = "signing_key_anomaly"
    BREAK_GLASS = "break_glass"


def record_security_event(event: SecurityEvent, source: str, message: str) -> None:
    """Emit one security event. ``message`` names what happened, never whom."""
    logging.getLogger(LOGGER).critical(
        "SECURITY %s: %s", event.value, message,
        extra={EVENT_FIELD: event.value, SOURCE_FIELD: source})


class SecurityEventCounter(logging.Handler):
    """Counts security-event records; formats nothing, exports nothing else."""

    def __init__(self) -> None:
        super().__init__(level=logging.CRITICAL)
        self._counter = metrics.get_meter("cloudops").create_counter(
            "cloudops.security.events", unit="{event}",
            description="Security events by kind (LLD: these always page)")

    def emit(self, record: logging.LogRecord) -> None:
        event = getattr(record, EVENT_FIELD, None)
        if event in SecurityEvent._value2member_map_:
            self._counter.add(1, {"event": str(event),
                                  "source": str(getattr(record, SOURCE_FIELD, "unknown"))})


__all__ = ["EVENT_FIELD", "LOGGER", "SOURCE_FIELD", "SecurityEvent", "SecurityEventCounter",
           "record_security_event"]
