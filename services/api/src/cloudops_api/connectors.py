"""Connector health (step 15; LLD, Connector contract; HLD, Integration): for each
scheduled source and entity, when it last succeeded, how its last day of runs
went, what it holds in quarantine and dead letters, and schema drift waiting for
a person. Staff only: ``connectors.read`` to look, ``connectors.manage`` to
acknowledge drift. No customer role holds either, and the database's functions
(migration 0010) refuse a customer again, so this module is a thin door.

The figures come from the platform's own run log, never from a source, and carry
no tenant: run counts and drift are per connector. Resource quarantine is counted
under the caller's scope (its row-level security applies inside the function),
so two people may see different depths; alias quarantine has no tenant and is
counted per source system, across its entities.

Health band, computed here and only here (``assess``), on the console's scale
(``apps/web/src/lib/health.ts``):

=========  ==========================================================  =========
timeliness  meaning                                                     band
=========  ==========================================================  =========
on_time    last success no more than one interval ago                  healthy
late       more than ``LATE_AFTER`` intervals since the last success    at-risk
outage     more than ``OUTAGE_AFTER`` intervals (LLD: a P2 page), or    critical
           runs but never a success
never_run  no run at all yet (or the first is still running)           unknown
=========  ==========================================================  =========

``source_absent``: the last run found the source has no such table (ServiceNow
project tasks while Project Portfolio Management is inactive, ADR 0038). A
supported state, not a failure: band ``unknown``, its own reason and count, and
none of the attention causes apply.

Attention raises the band to at least ``watch``: a failed or dead-lettered run
in the last 24 hours, the last run failed, a rejected record in the last 24
hours, an open dead letter, or open schema drift. A failed-run rate of
``FAILING_RATE`` or more over the last 24 hours raises it to at least
``at-risk``. The band is the worst of these; each cause is named in ``reasons``.

Rates are decimals between 0 and 1, rounded half-up to four places, and absent
(not zero) when there was nothing to divide by: failed runs over finished runs,
rejected records over records read, both over the last 24 hours.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_api.access import Access
from cloudops_api.pipeline import ApiError, AuditedRoute, current_access, platform, requires
from cloudops_api.provenance import ApiModel, Metric, query_ref
from cloudops_tenancy import ScopedConnection

router = APIRouter(prefix="/v1/connectors", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

READ: Final = "connectors.read"
MANAGE: Final = "connectors.manage"

# The run log is the platform's, not the source's: figures name it as their source.
RUN_LOG: Final = "cloudops_ingest"
POLICY: Final = "cloudops_policy"

# The bands' thresholds, in one place (see the module docstring).
LATE_AFTER: Final = 1  # intervals since the last success, strictly more than
OUTAGE_AFTER: Final = 3  # LLD: a connector three intervals late pages (P2)
FAILING_RATE: Final = Decimal("0.5")  # not money: failed runs over runs, last 24 hours
_RATE_PLACES: Final = Decimal("0.0001")
_INTERVAL_PLACES: Final = Decimal("0.01")

Band = Literal["healthy", "watch", "at-risk", "critical", "unknown"]
Timeliness = Literal["on_time", "late", "outage", "never_run", "source_absent"]
Reason = Literal[
    "late", "outage", "never_run", "no_success", "failing", "failed_runs",
    "last_run_failed", "rejected_records", "dead_letters", "schema_drift", "source_absent",
]
RunStatus = Literal["running", "succeeded", "dead_lettered", "failed", "source_absent"]

RANK: Final[dict[Band, int]] = {"unknown": -1, "healthy": 0, "watch": 1, "at-risk": 2,
                                 "critical": 3}
_FAILED: Final = frozenset({"failed", "dead_lettered"})

Name = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,40}$")]


@asynccontextmanager
async def _guarded(request: Request, access: Access) -> AsyncIterator[ScopedConnection]:
    try:
        async with platform(request).db.transaction(access.scope) as conn:
            yield conn
    except DBAPIError as exc:
        # The functions refuse a principal without the permission (migration 0010).
        if str(getattr(exc.orig, "sqlstate", "")) == "42501":
            raise ApiError(403, "forbidden") from None
        raise


def _staff(access: Access) -> None:
    # The database refuses a customer anyway; this keeps the reason out of reach.
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


# --- the band ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Figures:
    """One row of ``access.connector_health``, as the band needs it."""

    every_seconds: int
    last_success_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    runs_24h: int
    failed_runs_24h: int
    records_24h: int
    rejected_24h: int
    dead_letters_open: int
    drift_open: int


@dataclass(frozen=True, slots=True)
class Assessment:
    band: Band
    timeliness: Timeliness
    reasons: tuple[Reason, ...]
    age_seconds: int | None
    intervals: Decimal | None  # since the last success
    failed_run_rate: Decimal | None
    rejected_rate: Decimal | None


def rate(part: int, whole: int) -> Decimal | None:
    """``part / whole`` to four places, half-up; None when there is no whole."""
    if whole <= 0:
        return None
    return (Decimal(part) / Decimal(whole)).quantize(_RATE_PLACES, rounding=ROUND_HALF_UP)


def _worst(a: Band, b: Band) -> Band:
    return a if RANK[a] >= RANK[b] else b


def assess(f: Figures, now: datetime) -> Assessment:
    """The health band of one connector at ``now`` (the database's clock)."""
    if f.last_status == "source_absent":
        return Assessment(band="unknown", timeliness="source_absent", reasons=("source_absent",),
                          age_seconds=None, intervals=None, failed_run_rate=None,
                          rejected_rate=None)
    reasons: list[Reason] = []
    age = None if f.last_success_at is None else max(
        0, int((now - f.last_success_at).total_seconds()))
    intervals = None if age is None else (Decimal(age) / Decimal(f.every_seconds)).quantize(
        _INTERVAL_PLACES, rounding=ROUND_HALF_UP)

    timeliness: Timeliness
    base: Band
    if age is None:
        # Runs, none succeeded, and the latest has finished: as down as a late one.
        if f.last_run_at is not None and f.last_status != "running":
            timeliness, base = "outage", "critical"
            reasons.append("no_success")
        else:
            timeliness, base = "never_run", "unknown"
            reasons.append("never_run")
    elif age > OUTAGE_AFTER * f.every_seconds:
        timeliness, base = "outage", "critical"
        reasons.append("outage")
    elif age > LATE_AFTER * f.every_seconds:
        timeliness, base = "late", "at-risk"
        reasons.append("late")
    else:
        timeliness, base = "on_time", "healthy"

    band: Band = base
    failed_rate = rate(f.failed_runs_24h, f.runs_24h)
    rejected_rate = rate(f.rejected_24h, f.records_24h)
    if failed_rate is not None and failed_rate >= FAILING_RATE:
        reasons.append("failing")
        band = _worst(band, "at-risk")
    attention: list[tuple[bool, Reason]] = [
        (f.failed_runs_24h > 0, "failed_runs"),
        (f.last_status in _FAILED, "last_run_failed"),
        (f.rejected_24h > 0, "rejected_records"),
        (f.dead_letters_open > 0, "dead_letters"),
        (f.drift_open > 0, "schema_drift"),
    ]
    for raised, reason in attention:
        if raised:
            reasons.append(reason)
            band = _worst(band, "watch")
    return Assessment(band=band, timeliness=timeliness, reasons=tuple(reasons),
                      age_seconds=age, intervals=intervals, failed_run_rate=failed_rate,
                      rejected_rate=rejected_rate)


# --- views ---------------------------------------------------------------------------


class ConnectorHealth(ApiModel):
    source_system: str
    entity: str
    band: Band
    timeliness: Timeliness
    reasons: list[Reason]
    expected_every: Metric[int]  # seconds, from the connector's schedule
    last_success_at: datetime | None  # all time, incremental runs
    last_success_age: Metric[int] | None  # seconds; absent when never succeeded
    intervals_since_success: Metric[Decimal] | None  # age over the expected interval
    last_run_at: datetime | None
    last_status: RunStatus | None
    last_lag: Metric[int] | None  # seconds behind the source, as the last run reported
    runs_24h: Metric[int]  # finished incremental runs
    failed_runs_24h: Metric[int]  # failed or dead-lettered
    records_24h: Metric[int]
    rejected_24h: Metric[int]  # malformed records, kept in bronze
    quarantined_24h: Metric[int]  # records held for an unmatched identity
    failed_run_rate_24h: Metric[Decimal] | None  # failed runs / runs, 0 to 1
    rejected_record_rate_24h: Metric[Decimal] | None  # rejected / records, 0 to 1
    dead_letters_open: Metric[int]
    alias_quarantine_depth: Metric[int]  # names held for this source, every entity
    resource_quarantine_depth: Metric[int]  # resources held, in the caller's scope
    drift_open: Metric[int]


class BandRules(ApiModel):
    """The thresholds the bands were computed with, so a screen explains them
    without restating them."""

    late_after_intervals: Metric[int]
    outage_after_intervals: Metric[int]
    failing_run_rate: Metric[Decimal]


class ConnectorHealthList(ApiModel):
    items: list[ConnectorHealth]
    as_of: datetime
    rules: BandRules
    outages: Metric[int]  # connectors whose band is critical
    late: Metric[int]  # at-risk
    attention: Metric[int]  # watch
    never_run: Metric[int]  # unknown: never run
    source_absent: Metric[int]  # unknown: the source has no such table (ADR 0038)


class DriftView(ApiModel):
    source_system: str
    entity: str
    field: str  # a field name, never a value
    drift: Literal["added", "missing"]
    drift_at: datetime | None
    occurrences: Metric[int]  # times the drift was seen since it was last acknowledged


class DriftList(ApiModel):
    items: list[DriftView]
    open: Metric[int]


class AcknowledgeRequest(ApiModel):
    source: Name
    entity: Name
    field: str = Field(min_length=1, max_length=200)


# --- endpoints -----------------------------------------------------------------------


def _metric(value: int, unit: str, name: str, as_of: datetime, **params: Any) -> Metric[int]:
    return Metric[int](value=value, unit=unit, source_system=RUN_LOG, as_of=as_of,
                       query_ref=query_ref(name, 1, **params), is_modelled=False)


def _ratio(value: Decimal | None, unit: str, name: str, as_of: datetime,
           **params: Any) -> Metric[Decimal] | None:
    if value is None:
        return None
    return Metric[Decimal](value=value, unit=unit, source_system=RUN_LOG, as_of=as_of,
                           query_ref=query_ref(name, 1, **params), is_modelled=False)


HEALTH_QUERY: Final = text(
    "SELECT h.source_system, h.entity, h.expected_every_seconds, h.last_success_at,"
    " h.last_run_at, h.last_status, h.last_lag_seconds, h.runs_24h, h.failed_runs_24h,"
    " h.records_24h, h.rejected_24h, h.quarantined_24h, h.dead_letters_open,"
    " h.alias_quarantine_depth, h.resource_quarantine_depth, h.drift_open,"
    " now() AS as_of FROM access.connector_health(:actor) h"
)


def health_row(row: tuple[Any, ...]) -> ConnectorHealth:
    (source, entity, every, last_success, last_run, last_status, lag, runs, failed, records,
     rejected, quarantined, dead, alias_depth, resource_depth, drift, now) = row
    figures = Figures(
        every_seconds=int(every), last_success_at=last_success, last_run_at=last_run,
        last_status=last_status, runs_24h=int(runs), failed_runs_24h=int(failed),
        records_24h=int(records), rejected_24h=int(rejected),
        dead_letters_open=int(dead), drift_open=int(drift))
    a = assess(figures, now)
    key = {"source": source, "entity": entity}

    def m(value: int, unit: str, name: str) -> Metric[int]:
        return _metric(value, unit, f"connectors.{name}", now, **key)

    return ConnectorHealth(
        source_system=source, entity=entity, band=a.band, timeliness=a.timeliness,
        reasons=list(a.reasons),
        expected_every=Metric[int](
            value=figures.every_seconds, unit="seconds", source_system=POLICY, as_of=now,
            query_ref=query_ref("connectors.schedule", 1, **key), is_modelled=False),
        last_success_at=last_success,
        last_success_age=None if a.age_seconds is None
        else m(a.age_seconds, "seconds", "last_success_age"),
        intervals_since_success=_ratio(a.intervals, "intervals",
                                       "connectors.intervals_since_success", now, **key),
        last_run_at=last_run, last_status=last_status,
        last_lag=None if lag is None else m(int(lag), "seconds", "last_lag"),
        runs_24h=m(figures.runs_24h, "runs", "runs_24h"),
        failed_runs_24h=m(figures.failed_runs_24h, "runs", "failed_runs_24h"),
        records_24h=m(figures.records_24h, "records", "records_24h"),
        rejected_24h=m(figures.rejected_24h, "records", "rejected_24h"),
        quarantined_24h=m(int(quarantined), "records", "quarantined_24h"),
        failed_run_rate_24h=_ratio(a.failed_run_rate, "ratio", "connectors.failed_run_rate",
                                   now, **key),
        rejected_record_rate_24h=_ratio(a.rejected_rate, "ratio",
                                        "connectors.rejected_record_rate", now, **key),
        dead_letters_open=m(figures.dead_letters_open, "dead letters", "dead_letters_open"),
        alias_quarantine_depth=_metric(int(alias_depth), "names",
                                       "connectors.alias_quarantine_depth", now, source=source),
        resource_quarantine_depth=_metric(int(resource_depth), "resources",
                                          "connectors.resource_quarantine_depth", now,
                                          source=source, scoped=True),
        drift_open=m(figures.drift_open, "fields", "drift_open"),
    )


def _rules(now: datetime) -> BandRules:
    def rule(name: str) -> str:
        return query_ref(f"connectors.rule.{name}", 1)

    return BandRules(
        late_after_intervals=Metric[int](
            value=LATE_AFTER, unit="intervals", source_system=POLICY, as_of=now,
            query_ref=rule("late"), is_modelled=False),
        outage_after_intervals=Metric[int](
            value=OUTAGE_AFTER, unit="intervals", source_system=POLICY, as_of=now,
            query_ref=rule("outage"), is_modelled=False),
        failing_run_rate=Metric[Decimal](
            value=FAILING_RATE, unit="ratio", source_system=POLICY, as_of=now,
            query_ref=rule("failing"), is_modelled=False),
    )


@router.get("/health", response_model=ConnectorHealthList)
@requires(READ)
async def connector_health(request: Request, access: AccessDep) -> ConnectorHealthList:
    """Every scheduled connector, worst first, with its band and why."""
    _staff(access)
    async with _guarded(request, access) as conn:
        rows = [tuple(r) for r in await conn.execute(HEALTH_QUERY,
                                                    {"actor": access.identity.oid})]
        now: datetime = await conn.scalar(text("SELECT now()"))
    items = sorted((health_row(r) for r in rows),
                   key=lambda h: (-RANK[h.band], h.source_system, h.entity))

    def total(band: Band, name: str, timeliness: Timeliness | None = None) -> Metric[int]:
        return _metric(sum(1 for h in items if h.band == band
                           and timeliness in (None, h.timeliness)), "connectors",
                       f"connectors.{name}", now, band=band)

    return ConnectorHealthList(
        items=items, as_of=now, rules=_rules(now),
        outages=total("critical", "outages"), late=total("at-risk", "late"),
        attention=total("watch", "attention"),
        never_run=total("unknown", "never_run", "never_run"),
        source_absent=total("unknown", "source_absent", "source_absent"),
    )


@router.get("/drift", response_model=DriftList)
@requires(READ)
async def schema_drift(request: Request, access: AccessDep) -> DriftList:
    """Fields a source added, or stopped sending, that nobody has acknowledged yet.
    Field names only: never a value from a payload."""
    _staff(access)
    async with _guarded(request, access) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT source_system, entity, field, drift, drift_at, drift_count"
            " FROM access.schema_drift(:actor)"), {"actor": access.identity.oid})]
        now: datetime = await conn.scalar(text("SELECT now()"))
    items = [DriftView(
        source_system=source, entity=entity, field=field, drift=drift, drift_at=at,
        occurrences=Metric[int](
            value=int(n), unit="sightings", source_system=RUN_LOG, as_of=at or now,
            query_ref=query_ref("connectors.drift_count", 1, source=source, entity=entity,
                                field=field),
            is_modelled=False),
    ) for source, entity, field, drift, at, n in rows]
    return DriftList(items=items, open=_metric(len(items), "fields", "connectors.drift_open",
                                               now, all=True))


@router.post("/drift/acknowledge", status_code=204, response_class=Response,
             responses={404: {"description": "No open drift on that field"}})
@requires(MANAGE)
async def acknowledge_drift(
    request: Request, body: AcknowledgeRequest, access: AccessDep,
) -> Response:
    """A person has seen the drift. The next drift on the same field raises it again."""
    _staff(access)
    async with _guarded(request, access) as conn:
        found = await conn.scalar(text(
            "SELECT access.acknowledge_drift(:actor, :source, :entity, :field)"),
            {"actor": access.identity.oid, "source": body.source, "entity": body.entity,
             "field": body.field})
    if not found:
        raise ApiError(404, "not_found")
    return Response(status_code=204)
