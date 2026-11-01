"""ServiceNow Table API record -> ``cloudops.work_item`` row.

Field names follow the ServiceNow export columns, so the mapping is 1:1
(reference/schema.sql, ServiceNow mirror). Records arrive with
``sysparm_display_value=all``: each field is ``{"value": ..., "display_value":
...}``. Dates, booleans and numbers are read from ``value`` (UTC, raw);
choices and references from ``display_value`` (what people see).

Every field not mapped here is kept, as received, in ``raw_extra``: nothing a
source sends is discarded (LLD, Connector contract).

Project tasks (``pm_project_task``, ADR 0038) map to ``cloudops.project_task``
with ``normalise_project``: open or closed from the state choice, and a price
with its currency code, read from a configurable field.

Ticket text (descriptions, notes) is stored verbatim. It is data, never
instruction (CLAUDE.md rule 4): nothing here interprets it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

SOURCE: Final = "servicenow"
TIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

# entity (table) -> work_item.kind
ENTITIES: Final = {"incident": "incident", "change_task": "change_task"}

# Fields this mapping consumes; everything else goes to raw_extra.
MAPPED: Final = frozenset({
    "number", "company", "opened_at", "closed_at", "sys_updated_on", "priority", "state",
    "assigned_to", "assignment_group", "short_description", "cmdb_ci", "change_request",
    "change_task_type", "u_resolve_sla", "u_response_sla", "made_sla", "u_sla_breached",
    "calendar_duration", "calendar_stc", "u_pending_total", "u_planned_hours",
    "u_actual_hours", "close_code", "close_notes",
})

NO_COMPANY: Final = "(no company)"


class MalformedRecordError(ValueError):
    """A record without the fields that key it. Kept in bronze, counted, skipped."""


@dataclass(frozen=True)
class Ticket:
    kind: str
    number: str
    company_raw: str
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    priority: str | None
    state: str | None
    assigned_to: str | None
    assignment_workgroup: str | None
    short_description: str | None
    configuration_item: str | None
    ci_sys_id: str | None
    change_request: str | None
    change_type: str | None
    resolve_sla: bool | None
    response_sla: bool | None
    made_sla: bool | None
    sla_breached: bool | None
    duration_seconds: int | None
    resolve_time_seconds: int | None
    pending_total_seconds: int | None
    planned_hours: Decimal | None
    actual_hours: Decimal | None
    closure_code: str | None
    close_notes: str | None
    raw_extra: dict[str, Any] = field(default_factory=dict)


def _field(record: Mapping[str, Any], name: str) -> tuple[str, str]:
    """(value, display_value), both '' when absent. Tolerates the plain form
    (sysparm_display_value=false), where a field is just its value."""
    raw = record.get(name)
    if raw is None:
        return "", ""
    if isinstance(raw, Mapping):
        value = raw.get("value")
        display = raw.get("display_value")
        return (
            "" if value is None else str(value),
            "" if display is None else str(display),
        )
    return str(raw), str(raw)


def _text(record: Mapping[str, Any], name: str, *, display: bool = True) -> str | None:
    value, shown = _field(record, name)
    out = shown if display else value
    return out if out.strip() else None


def _time(record: Mapping[str, Any], name: str) -> datetime | None:
    value, _ = _field(record, name)
    if not value.strip():
        return None
    try:
        # The integration user's timezone is GMT, so raw values are UTC.
        return datetime.strptime(value.strip(), TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise MalformedRecordError(f"{name} is not a ServiceNow date-time") from None


def _bool(record: Mapping[str, Any], name: str) -> bool | None:
    value, _ = _field(record, name)
    v = value.strip().lower()
    if v in ("true", "1"):
        return True
    if v in ("false", "0"):
        return False
    return None


def _int(record: Mapping[str, Any], name: str) -> int | None:
    value, _ = _field(record, name)
    try:
        return int(value) if value.strip() else None
    except ValueError:
        return None


def _decimal(record: Mapping[str, Any], name: str) -> Decimal | None:
    value, _ = _field(record, name)
    try:
        return Decimal(value) if value.strip() else None
    except InvalidOperation:
        return None


def _duration(record: Mapping[str, Any], name: str) -> int | None:
    """ServiceNow durations are date-times after the epoch: '1970-01-02 03:00:00'
    is 27 hours."""
    try:
        moment = _time(record, name)
    except MalformedRecordError:
        return None
    return None if moment is None else int((moment - EPOCH).total_seconds())


def normalise(entity: str, record: Mapping[str, Any]) -> Ticket:
    if entity not in ENTITIES:
        raise ValueError(f"unknown entity {entity!r}")
    number = _text(record, "number", display=False)
    opened_at = _time(record, "opened_at")
    updated_at = _time(record, "sys_updated_on")
    if number is None or opened_at is None or updated_at is None:
        raise MalformedRecordError("number, opened_at and sys_updated_on are required")
    ci_value, ci_display = _field(record, "cmdb_ci")
    return Ticket(
        kind=ENTITIES[entity],
        number=number,
        company_raw=_text(record, "company") or NO_COMPANY,
        opened_at=opened_at,
        updated_at=updated_at,
        closed_at=_time(record, "closed_at"),
        priority=_text(record, "priority"),
        state=_text(record, "state"),
        assigned_to=_text(record, "assigned_to"),
        assignment_workgroup=_text(record, "assignment_group"),
        short_description=_text(record, "short_description", display=False),
        configuration_item=ci_display if ci_display.strip() else None,
        ci_sys_id=ci_value if ci_value.strip() else None,
        change_request=_text(record, "change_request"),
        change_type=_text(record, "change_task_type"),
        resolve_sla=_bool(record, "u_resolve_sla"),
        response_sla=_bool(record, "u_response_sla"),
        made_sla=_bool(record, "made_sla"),
        sla_breached=_bool(record, "u_sla_breached"),
        duration_seconds=_duration(record, "calendar_duration"),
        resolve_time_seconds=_int(record, "calendar_stc"),
        pending_total_seconds=_int(record, "u_pending_total"),
        planned_hours=_decimal(record, "u_planned_hours"),
        actual_hours=_decimal(record, "u_actual_hours"),
        closure_code=_text(record, "close_code"),
        close_notes=_text(record, "close_notes", display=False),
        raw_extra={k: v for k, v in record.items() if k not in MAPPED},
    )


def updated_at(record: Mapping[str, Any]) -> datetime | None:
    """sys_updated_on of any record, malformed or not, for the watermark."""
    try:
        return _time(record, "sys_updated_on")
    except MalformedRecordError:
        return None


# --- project tasks (ADR 0038) ------------------------------------------------------
#
# Fields, state values and the currency-field shape as dev407774 has them, recorded
# (connectors/tests/recorded). PPM holds costs, not a price: which cost stands for
# the price is a setting, not a constant.

PROJECT_ENTITY: Final = "pm_project_task"
ALL_ENTITIES: Final = (*ENTITIES, PROJECT_ENTITY)
PRICE_FIELD_VAR: Final = "CLOUDOPS_SERVICENOW_PROJECT_PRICE_FIELD"
DEFAULT_PRICE_FIELD: Final = "cost"  # planned_task.cost, "Total planned cost"
_FIELD_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
# planned_task's state choice: these values are closed; any other is open.
CLOSED_STATES: Final = frozenset({"3", "4", "7"})
PROJECT_MAPPED: Final = frozenset({
    "sys_id", "number", "company", "top_task", "short_description", "state", "opened_at",
    "start_date", "end_date", "closed_at", "sys_updated_on",
})
_CURRENCY_STRING: Final = re.compile(r"^([A-Z]{3});(-?\d+(?:\.\d+)?)$")


def price_field(environ: Mapping[str, str]) -> str:
    """The source field read as the price: ``cost`` unless configured. A field
    name only, checked, so the configuration cannot reach the query string."""
    name = environ.get(PRICE_FIELD_VAR, "").strip() or DEFAULT_PRICE_FIELD
    if not _FIELD_NAME.match(name):
        raise ValueError(f"{PRICE_FIELD_VAR} must be a ServiceNow field name")
    return name


@dataclass(frozen=True)
class ProjectTask:
    sys_id: str
    number: str
    company_raw: str
    updated_at: datetime
    project: str | None
    short_description: str | None
    state: str | None
    status: str  # open | closed
    opened_at: datetime | None
    start_at: datetime | None
    end_at: datetime | None
    closed_at: datetime | None
    price: Decimal | None
    price_currency: str | None
    price_field: str | None
    raw_extra: dict[str, Any] = field(default_factory=dict)


def money(record: Mapping[str, Any], name: str) -> tuple[Decimal, str] | None:
    """A currency field as (amount, ISO code) in the currency it was entered in, or
    None when the record does not state one.

    With ``sysparm_display_value=all`` a currency field carries three halves
    (dev407774, 2026-11-01): ``value`` and ``display_value`` converted to the
    instance's reference currency at the instance's own rate (``1379.1834``,
    ``$1,379.1834``), and ``currency_display_value``, the amount as entered with its
    code (``EUR;1234.50``). Only the last is the transaction amount (CLAUDE.md rule
    5), so only it is read; a stored currency string in ``value`` is accepted too.
    A plain or converted amount is never paired with a guessed currency."""
    raw = record.get(name)
    entered = raw.get("currency_display_value") if isinstance(raw, Mapping) else None
    candidates = (str(entered or "").strip(), _field(record, name)[0].strip())
    found = next((m for m in map(_CURRENCY_STRING.match, candidates) if m), None)
    if found is None:
        return None
    try:
        return Decimal(found.group(2)), found.group(1)
    except InvalidOperation:  # pragma: no cover - the pattern admits only decimals
        return None


def normalise_project(record: Mapping[str, Any], price_field: str) -> ProjectTask:
    sys_id = _text(record, "sys_id", display=False)
    number = _text(record, "number", display=False)
    updated = _time(record, "sys_updated_on")
    state_value, state_shown = _field(record, "state")
    if sys_id is None or number is None or updated is None or not state_value.strip():
        raise MalformedRecordError("sys_id, number, state and sys_updated_on are required")
    priced = money(record, price_field)
    return ProjectTask(
        sys_id=sys_id,
        number=number,
        company_raw=_text(record, "company") or NO_COMPANY,
        updated_at=updated,
        project=_text(record, "top_task"),
        short_description=_text(record, "short_description", display=False),
        state=state_shown.strip() or None,
        status="closed" if state_value.strip() in CLOSED_STATES else "open",
        opened_at=_time(record, "opened_at"),
        start_at=_time(record, "start_date"),
        end_at=_time(record, "end_date"),
        closed_at=_time(record, "closed_at"),
        price=None if priced is None else priced[0],
        price_currency=None if priced is None else priced[1],
        price_field=None if priced is None else price_field,
        # The price field stays here too: it is configurable, and kept as received.
        raw_extra={k: v for k, v in record.items() if k not in PROJECT_MAPPED},
    )
