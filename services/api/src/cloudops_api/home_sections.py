"""Home dashboard sections (ADR 0038, amending ADR 0030).

Home is five sections, each over a date range the viewer chooses; the console
compares two ranges by calling the same endpoint twice. Every section runs under
the caller's scope narrowed by the customer selector (``ScopeDep``, ADR 0032), so
row-level security decides what exists, and every number is a ``Metric``.

- ``/v1/home/sections/operations`` (``tickets.read``): work received by kind and
  priority, how much the classifier triaged and where it routed it, auto
  remediation (always 0 until the execution plane exists, and said so), and SLA
  attainment against ADR 0030's modelled targets. Triage is staff-only.
- ``/v1/home/sections/revenue`` (``cost.read``): a customer gets the price of its
  own consumption (ADR 0021), closed months only, and nothing else. Staff revenue,
  cost and margin are the revenue table's (``/v1/commercial/revenue``) and project
  prices the project task ingest's: this module computes neither, and says
  ``no_data_source`` until they are served.
- ``/v1/home/sections/security`` (``vulnerabilities.read``): open vulnerabilities
  at the range's end by severity and age, found and fixed in the range; for staff,
  assessment scores (ADR 0020) and CoreStack's security events.
- ``/v1/home/sections/platform`` (``inventory.read``, staff): customers onboarded,
  split by cloud, and each customer's size band.
- ``/v1/home/sections/top-customers`` (``tickets.read``, staff): ten customers by
  a modelled health score (by revenue once the revenue table serves it).
- ``/v1/settings/size-bands``: the size band thresholds, set with
  ``sizebands.manage`` (migration 0036).

**Money** stays in its transaction currency, converted only at the daily average
rate in force on each line's own date: the customer's price, and the spend that
decides a size band (a month with any day lacking a rate has none). Nothing is
rounded here (CLAUDE.md rule 5).

**Modelled figures** say so: SLA attainment (ADR 0030 targets), and the health
score and its parts (``HEALTH_POLICY``).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_api.access import Access
from cloudops_api.bff import PRICE_SOURCE, PriceLine, TenantView, _converted, customer_price
from cloudops_api.home import POLICY_SOURCE, PRIORITIES, SLA_TARGET_HOURS, TICKET_SOURCE
from cloudops_api.pipeline import (
    ApiError,
    AuditedRoute,
    ScopeDep,
    current_access,
    platform,
    requires,
)
from cloudops_api.portal import AGE_BAND, AGE_BANDS, AgeBandName, Severity
from cloudops_api.provenance import ApiModel, Metric, query_ref
from cloudops_domain.assurance.scoring import ScoredResponse, score
from cloudops_tenancy import ScopedConnection

router = APIRouter(prefix="/v1", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]
Currency = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
Start = Annotated[date | None, Query(description="First day, inclusive (UTC).")]
End = Annotated[date | None, Query(description="Last day, inclusive (UTC); default today.")]

COST_SOURCE: Final = "billing_exports"
QUALYS: Final = "qualys"
CORESTACK: Final = "corestack"
ASSURANCE: Final = "assurance"
INVENTORY: Final = "inventory"
CLASSIFIER: Final = "triage_classifier"
EXECUTION: Final = "execution_plane"
SIZE_POLICY: Final = "size_band_policy"
RATIO: Final = "ratio"
MAX_RANGE_DAYS: Final = 366
TOP: Final = 10
# Work item kinds in the order the dashboard lists them; any other kind the table
# holds follows, so a kind ingested later is counted, never dropped.
KINDS: Final = ("incident", "service_request", "change_task", "project_task")
TRIAGED_KINDS: Final = ("incident", "service_request")


def _now() -> datetime:
    return datetime.now(UTC)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _count(value: Any, unit: str, source: str, as_of: datetime, name: str, *,
           modelled: bool = False, **params: Any) -> Metric[int]:
    return Metric[int](value=int(value or 0), unit=unit, source_system=source, as_of=as_of,
                       query_ref=query_ref(name, 1, **params), is_modelled=modelled)


def _dec(value: Any, unit: str | None, source: str, as_of: datetime, name: str, *,
         modelled: bool = False, **params: Any) -> Metric[Decimal] | None:
    if value is None or unit is None:
        return None
    return Metric[Decimal](value=Decimal(value), unit=unit, source_system=source, as_of=as_of,
                           query_ref=query_ref(name, 1, **params), is_modelled=modelled)


def _end_of(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time.min, UTC)


def _staff(access: Access) -> None:
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


# --- the date range --------------------------------------------------------------------


class DateRange(ApiModel):
    start: date
    end: date
    days: Metric[int]


def resolve_range(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    """Both days inclusive. Default: the month to date. More than a year, or an
    end before the start, is refused rather than clipped."""
    end = end or today
    start = start or end.replace(day=1)
    if start > end or (end - start).days >= MAX_RANGE_DAYS:
        raise ApiError(422, "invalid_range")
    return start, end


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Half-open timestamps: from the start day's midnight to the day after the end."""
    return datetime.combine(start, time.min, UTC), _end_of(end)


def _range_view(start: date, end: date, now: datetime) -> DateRange:
    return DateRange(start=start, end=end, days=_count((end - start).days + 1, "days",
                                                       POLICY_SOURCE, now, "home.range_days"))


def _rate(alias: str, day: str) -> str:
    """The daily average rate from ``alias``.currency to ``:rep`` in force on ``day``:
    NULL with no currency asked for or no rate in force, never a 1 or a 0."""
    return (
        f"SELECT CASE WHEN CAST(:rep AS text) IS NULL THEN NULL"  # noqa: S608 - constants
        f" WHEN {alias}.currency = :rep THEN 1::numeric ELSE ("
        f"  SELECT f.rate FROM cloudops.fx_rate f WHERE f.from_currency = {alias}.currency"
        f"    AND f.to_currency = :rep AND f.rate_type = 'average' AND f.effective_date <= {day}"
        f"  ORDER BY f.effective_date DESC LIMIT 1) END AS rate"
    )


# --- operations ------------------------------------------------------------------------


class PriorityCount(ApiModel):
    priority: str  # "1".."5", or "none": a label, not a measure
    received: Metric[int]


class KindReceived(ApiModel):
    kind: str
    received: Metric[int]
    by_priority: list[PriorityCount]


class TriageFigures(ApiModel):
    """Incidents and requests received in the range that the classifier triaged,
    by the route of their latest triage."""

    eligible: Metric[int]  # incidents and requests received
    triaged: Metric[int]
    ready_for_planning: Metric[int]  # routed on automatically
    human: Metric[int]  # routed to a person
    share: Metric[Decimal] | None  # triaged over eligible; None: nothing received


class RemediationFigures(ApiModel):
    """Always 0 until the execution plane exists (CLAUDE.md: not before the
    security review). The figure is shown with its reason, never hidden."""

    available: bool
    reason: Literal["execution_plane_not_built"] | None
    auto_remediated: Metric[int]


class SlaLine(ApiModel):
    priority: str
    resolved: Metric[int]  # incidents resolved in the range
    target_hours: Metric[int] | None  # modelled; None: the priority has none
    within_target: Metric[int] | None  # modelled
    attainment: Metric[Decimal] | None  # modelled; None: no target or none resolved
    breached_flagged: Metric[int]  # ServiceNow's own flag, observed


class OperationsSection(ApiModel):
    range: DateRange
    received: list[KindReceived]
    received_total: Metric[int]
    triage: TriageFigures | None  # None: a customer (triage is staff-only, ADR 0014)
    remediation: RemediationFigures
    sla: list[SlaLine]
    attainment: Metric[Decimal] | None  # across priorities with a target; modelled


_PRIORITY: Final = "left(btrim(coalesce(w.priority, '')), 1)"
_RECEIVED: Final = text(
    f"SELECT w.kind, {_PRIORITY}, count(*) FROM cloudops.work_item w"  # noqa: S608 - constants
    " WHERE w.opened_at >= :lo AND w.opened_at < :hi GROUP BY 1, 2"
)
_TRIAGED: Final = text(
    "SELECT l.route, count(*) FROM cloudops.work_item w"
    " JOIN LATERAL (SELECT tr.route FROM cloudops.triage_result tr"
    "   WHERE tr.tenant_id = w.tenant_id AND tr.number = w.number"
    "   ORDER BY tr.created_at DESC LIMIT 1) l ON true"
    " WHERE w.kind = ANY(CAST(:kinds AS text[])) AND w.opened_at >= :lo AND w.opened_at < :hi"
    " GROUP BY 1"
)
# Resolve time as ServiceNow measured it, else wall clock from open to close.
_RESOLVE_SECONDS: Final = (
    "coalesce(w.resolve_time_seconds, extract(epoch FROM (w.closed_at - w.opened_at)))"
)
_SLA: Final = text(
    f"SELECT {_PRIORITY}, count(*),"  # noqa: S608 - constants
    f"   count(*) FILTER (WHERE tg.hours IS NOT NULL AND {_RESOLVE_SECONDS} <= tg.hours * 3600),"
    "   count(*) FILTER (WHERE w.sla_breached)"
    " FROM cloudops.work_item w"
    " LEFT JOIN unnest(CAST(:pri AS text[]), CAST(:hrs AS int[])) AS tg(p, hours)"
    f"   ON tg.p = {_PRIORITY}"
    " WHERE w.kind = 'incident' AND w.closed_at >= :lo AND w.closed_at < :hi GROUP BY 1"
)


def _priority_key(p: Any) -> str:
    return str(p) if str(p) in PRIORITIES else "none"


def _ratio(part: int, whole: int) -> Decimal | None:
    return None if whole == 0 else Decimal(part) / Decimal(whole)


@router.get("/home/sections/operations", response_model=OperationsSection)
@requires("tickets.read")
async def operations(request: Request, access: AccessDep, scope: ScopeDep,
                     start: Start = None, end: End = None) -> OperationsSection:
    now = _now()
    first, last = resolve_range(start, end, now.date())
    lo, hi = _bounds(first, last)
    as_of = min(now, hi)
    staff = access.kind == "staff"
    span = {"lo": lo, "hi": hi}
    async with platform(request).db.transaction(scope) as conn:
        received = _rows(await conn.execute(_RECEIVED, span))
        triaged = _rows(await conn.execute(_TRIAGED, {**span, "kinds": list(TRIAGED_KINDS)})) \
            if staff else None
        sla = _rows(await conn.execute(_SLA, {**span, "pri": list(SLA_TARGET_HOURS),
                                              "hrs": list(SLA_TARGET_HOURS.values())}))
    params: dict[str, Any] = {"start": first, "end": last}

    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for kind, p, n in received:
        by_kind[str(kind)][_priority_key(p)] += int(n)
    kinds = [*KINDS, *sorted(k for k in by_kind if k not in KINDS)]
    lines = [
        KindReceived(
            kind=k,
            received=_count(sum(by_kind[k].values()), "work items", TICKET_SOURCE, as_of,
                            "home.received", kind=k, **params),
            by_priority=[PriorityCount(priority=p, received=_count(
                by_kind[k].get(p, 0), "work items", TICKET_SOURCE, as_of,
                "home.received_by_priority", kind=k, priority=p, **params))
                for p in (*PRIORITIES, "none")])
        for k in kinds
    ]

    triage = None
    if triaged is not None:
        routes = {str(r): int(n) for r, n in triaged}
        eligible = sum(sum(by_kind[k].values()) for k in TRIAGED_KINDS)
        total = sum(routes.values())

        def tc(v: int, name: str) -> Metric[int]:
            return _count(v, "tickets", CLASSIFIER, as_of, name, **params)

        triage = TriageFigures(
            eligible=_count(eligible, "tickets", TICKET_SOURCE, as_of, "home.triage_eligible",
                            **params),
            triaged=tc(total, "home.triaged"),
            ready_for_planning=tc(routes.get("ready_for_planning", 0), "home.triaged_ready"),
            human=tc(routes.get("human", 0), "home.triaged_human"),
            share=_dec(_ratio(total, eligible), RATIO, CLASSIFIER, as_of, "home.triaged_share",
                       **params))

    resolved: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for p, n, within, flagged in sla:
        key = _priority_key(p)
        resolved[key] = [a + int(b) for a, b in zip(resolved[key], (n, within, flagged),
                                                    strict=True)]
    sla_lines: list[SlaLine] = []
    with_target = met = 0
    for p in (*PRIORITIES, "none"):
        n, within, flagged = resolved.get(p, [0, 0, 0])
        hours = SLA_TARGET_HOURS.get(p)
        if hours is not None:
            with_target += n
            met += within
        sla_lines.append(SlaLine(
            priority=p,
            resolved=_count(n, "incidents", TICKET_SOURCE, as_of, "home.resolved", priority=p,
                            **params),
            target_hours=None if hours is None else _count(
                hours, "hours", POLICY_SOURCE, as_of, "home.sla_target", modelled=True,
                priority=p),
            within_target=None if hours is None else _count(
                within, "incidents", POLICY_SOURCE, as_of, "home.within_target",
                modelled=True, priority=p, **params),
            attainment=None if hours is None else _dec(
                _ratio(within, n), RATIO, POLICY_SOURCE, as_of, "home.attainment",
                modelled=True, priority=p, **params),
            breached_flagged=_count(flagged, "incidents", TICKET_SOURCE, as_of,
                                    "home.breached_flagged", priority=p, **params)))

    return OperationsSection(
        range=_range_view(first, last, now),
        received=lines,
        received_total=_count(sum(sum(v.values()) for v in by_kind.values()), "work items",
                              TICKET_SOURCE, as_of, "home.received_total", **params),
        triage=triage,
        remediation=RemediationFigures(
            available=False, reason="execution_plane_not_built",
            auto_remediated=_count(0, "work items", EXECUTION, now, "home.auto_remediated",
                                   **params)),
        sla=sla_lines,
        attainment=_dec(_ratio(met, with_target), RATIO, POLICY_SOURCE, as_of,
                        "home.attainment_all", modelled=True, **params),
    )


# --- revenue and margin ----------------------------------------------------------------


class SourcePending(ApiModel):
    """Figures another module serves and this one does not compute (ADR 0038): staff
    revenue, cost and margin come from the revenue table (``/v1/commercial/revenue``),
    project prices from the ServiceNow project task ingest. Until each is merged, the
    section says so rather than showing a figure of its own."""

    available: bool
    reason: Literal["no_data_source"] | None


PENDING: Final = SourcePending(available=False, reason="no_data_source")


class RevenueSection(ApiModel):
    range: DateRange
    view: Literal["staff", "customer"]
    reporting_currency: str | None
    # Staff: hosting, managed services and margin (the revenue table), and project
    # prices (project tasks). None for a customer.
    revenue: SourcePending | None
    projects: SourcePending | None
    customer_price: list[PriceLine] | None  # a customer's own price; None for staff


_CUSTOMER_PRICE: Final = text(
    "SELECT period, provider, currency, price, converted"
    " FROM cloudops.customer_monthly_price(:start, :end, CAST(:rep AS char(3)))"
    " ORDER BY period, provider, currency"
)


@router.get("/home/sections/revenue", response_model=RevenueSection)
@requires("cost.read")
async def revenue(request: Request, access: AccessDep, scope: ScopeDep,
                  start: Start = None, end: End = None,
                  currency: Currency = None) -> RevenueSection:
    """A customer: the price of its own consumption, closed months only, converted
    at each day's rate when ``currency`` is given (ADR 0021). Staff, with
    ``commercial.read``: which sources the section waits for."""
    now = _now()
    first, last = resolve_range(start, end, now.date())
    if access.kind != "customer":
        if not access.allows("commercial.read"):
            raise ApiError(403, "forbidden")
        return RevenueSection(range=_range_view(first, last, now), view="staff",
                              reporting_currency=currency, revenue=PENDING, projects=PENDING,
                              customer_price=None)
    async with platform(request).db.transaction(scope) as conn:
        rows = _rows(await conn.execute(_CUSTOMER_PRICE,
                                        {"start": first, "end": last, "rep": currency}))
    ref = query_ref("home.customer_price", 1, start=first, end=last)
    cref = query_ref("home.customer_price_converted", 1, start=first, end=last, rep=currency)
    prices: list[PriceLine] = []
    for period, provider, ccy, total, conv in rows:
        base = customer_price(period, ccy, total, ref)
        month_end = (period.replace(day=28) + timedelta(days=4)).replace(day=1) \
            - timedelta(days=1)
        prices.append(PriceLine(
            period=period, provider=provider, currency=ccy, price=base.price,
            not_captured=base.not_captured,
            converted=_converted(conv, total is not None, currency, PRICE_SOURCE,
                                 _end_of(month_end), cref)))
    return RevenueSection(range=_range_view(first, last, now), view="customer",
                          reporting_currency=currency, revenue=None, projects=None,
                          customer_price=prices)


# --- security and compliance -----------------------------------------------------------


class AgeCount(ApiModel):
    band: AgeBandName
    open: Metric[int]


class SeverityLine(ApiModel):
    severity: Severity  # Qualys 5 (urgent) .. 1: a label, not a measure
    open: Metric[int]  # open at the range's end
    by_age: list[AgeCount]  # age at the range's end
    found: Metric[int]  # first detected in the range
    fixed: Metric[int]  # remediated in the range


class AssessmentScore(ApiModel):
    tenant: TenantView
    framework: str
    title: str
    opened_at: datetime
    score: Metric[Decimal] | None  # percent; None: nothing approved to score yet


class EventLine(ApiModel):
    severity: Literal["high", "medium", "low"]
    detected: Metric[int]  # detected in the range
    open: Metric[int]  # open at the range's end


class SecurityEvents(ApiModel):
    by_severity: list[EventLine]
    compliance_open: Metric[int]  # CoreStack compliance findings open at the end


class SecuritySection(ApiModel):
    range: DateRange
    vulnerabilities: list[SeverityLine]
    open_total: Metric[int]
    # Staff with assurance.read: the latest assessment of each customer opened by
    # the range's end, scored as it stands now. None: not available to the caller.
    assessments: list[AssessmentScore] | None
    assessment_average: Metric[Decimal] | None
    events: SecurityEvents | None  # staff only: no customer grant on the source


_OPEN_AT: Final = (
    "first_detected < :hi AND (remediated_at IS NULL OR remediated_at >= :hi)"
)
_VULNS: Final = text(
    f"SELECT severity, {AGE_BAND}, count(*) FILTER (WHERE {_OPEN_AT}),"  # noqa: S608 - constants
    "   count(*) FILTER (WHERE first_detected >= :lo AND first_detected < :hi),"
    "   count(*) FILTER (WHERE remediated_at >= :lo AND remediated_at < :hi)"
    " FROM cloudops.vulnerability"
    f" WHERE ({_OPEN_AT}) OR (first_detected >= :lo AND first_detected < :hi)"
    "   OR (remediated_at >= :lo AND remediated_at < :hi)"
    " GROUP BY 1, 2"
)
_ASSESSMENTS: Final = text(
    "SELECT DISTINCT ON (a.tenant_id) a.id, a.tenant_id, t.canonical_name, a.framework,"
    "   a.title, a.created_at"
    " FROM cloudops.assessment a JOIN cloudops.tenant t ON t.id = a.tenant_id"
    " WHERE a.created_at < :hi ORDER BY a.tenant_id, a.created_at DESC"
)
_RESPONSES: Final = text(
    "SELECT assessment_id, control_code, result, approved_at IS NOT NULL"
    " FROM cloudops.control_response WHERE assessment_id = ANY(CAST(:ids AS uuid[]))"
)
_EVENT_OPEN: Final = (
    "detected_at < :hi AND status <> 'dismissed' AND (resolved_at IS NULL OR resolved_at >= :hi)"
)
_EVENTS: Final = text(
    f"SELECT category, severity, count(*) FILTER (WHERE {_EVENT_OPEN}),"  # noqa: S608
    "   count(*) FILTER (WHERE detected_at >= :lo AND detected_at < :hi)"
    " FROM cloudops.security_finding GROUP BY 1, 2"
)


@router.get("/home/sections/security", response_model=SecuritySection)
@requires("vulnerabilities.read")
async def security(request: Request, access: AccessDep, scope: ScopeDep,
                   start: Start = None, end: End = None) -> SecuritySection:
    now = _now()
    first, last = resolve_range(start, end, now.date())
    lo, hi = _bounds(first, last)
    at = min(now, hi)
    staff = access.kind == "staff"
    assess = staff and access.allows("assurance.read")
    async with platform(request).db.transaction(scope) as conn:
        vulns = _rows(await conn.execute(_VULNS, {"lo": lo, "hi": hi, "now": at}))
        heads = _rows(await conn.execute(_ASSESSMENTS, {"hi": hi})) if assess else []
        responses = _rows(await conn.execute(_RESPONSES, {"ids": [h[0] for h in heads]})) \
            if heads else []
        events = _rows(await conn.execute(_EVENTS, {"lo": lo, "hi": hi})) if staff else None
    params: dict[str, Any] = {"start": first, "end": last}

    open_by: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    found: dict[int, int] = defaultdict(int)
    fixed: dict[int, int] = defaultdict(int)
    severities: tuple[Severity, ...] = (5, 4, 3, 2, 1)
    for sev, band, n_open, n_found, n_fixed in vulns:
        s = int(sev)
        if band is not None:
            open_by[s][str(band)] += int(n_open)
        found[s] += int(n_found)
        fixed[s] += int(n_fixed)

    def vc(v: int, name: str, **p: Any) -> Metric[int]:
        return _count(v, "vulnerabilities", QUALYS, at, name, **params, **p)

    lines = [
        SeverityLine(
            severity=s, open=vc(sum(open_by[s].values()), "home.vuln_open", severity=s),
            by_age=[AgeCount(band=b, open=vc(open_by[s].get(b, 0), "home.vuln_age",
                                              severity=s, band=b)) for b, _, _ in AGE_BANDS],
            found=vc(found[s], "home.vuln_found", severity=s),
            fixed=vc(fixed[s], "home.vuln_fixed", severity=s))
        for s in severities
    ]

    assessments: list[AssessmentScore] | None = None
    average = None
    if assess:
        by_id: dict[str, list[ScoredResponse]] = defaultdict(list)
        for aid, code, result, approved in responses:
            by_id[str(aid)].append(ScoredResponse(code, result, bool(approved)))
        assessments = []
        for aid, tid, name, framework, title, created in heads:
            scored_now = score(by_id.get(str(aid), []))
            assessments.append(AssessmentScore(
                tenant=TenantView(id=tid, name=name), framework=framework, title=title,
                opened_at=created,
                score=_dec(scored_now.percent, "percent", ASSURANCE, now, "home.assessment_score",
                           assessment=str(aid))))
        scored = [a.score.value for a in assessments if a.score is not None]
        average = _dec(sum(scored, Decimal(0)) / len(scored) if scored else None, "percent",
                       ASSURANCE, now, "home.assessment_average", **params)

    event_view = None
    if events is not None:
        sec: dict[str, tuple[int, int]] = {}
        compliance = 0
        for category, severity, n_open, n_detected in events:
            if category == "compliance":
                compliance += int(n_open)
            else:
                sec[str(severity)] = (int(n_open), int(n_detected))
        event_view = SecurityEvents(
            by_severity=[EventLine(
                severity=s,
                detected=_count(sec.get(s, (0, 0))[1], "events", CORESTACK, at,
                                "home.events_detected", severity=s, **params),
                open=_count(sec.get(s, (0, 0))[0], "events", CORESTACK, at, "home.events_open",
                            severity=s, **params))
                for s in ("high", "medium", "low")],
            compliance_open=_count(compliance, "findings", CORESTACK, at,
                                   "home.compliance_open", **params))

    return SecuritySection(
        range=_range_view(first, last, now), vulnerabilities=lines,
        open_total=vc(sum(sum(v.values()) for v in open_by.values()), "home.vuln_open_total"),
        assessments=assessments, assessment_average=average, events=event_view)


# --- size bands ------------------------------------------------------------------------


SizeBand = Literal["large", "medium", "small"]
RANK: Final[dict[SizeBand, int]] = {"small": 0, "medium": 1, "large": 2}


@dataclass(frozen=True)
class SizePolicy:
    version: int
    currency: str
    spend_medium_from: Decimal
    spend_large_above: Decimal
    resources_medium_from: int
    resources_large_above: int
    set_at: datetime


def _band(value: Decimal | int, medium_from: Decimal | int, large_above: Decimal | int
          ) -> SizeBand:
    if value > large_above:
        return "large"
    return "medium" if value >= medium_from else "small"


def size_band(spend: Decimal | None, resources: int, policy: SizePolicy
              ) -> tuple[SizeBand | None, SizeBand, SizeBand]:
    """(spend band, resource band, band): the band is the higher of the two. A
    spend that is not known leaves the resource band to decide alone."""
    by_resources = _band(resources, policy.resources_medium_from, policy.resources_large_above)
    if spend is None:
        return None, by_resources, by_resources
    by_spend = _band(spend, policy.spend_medium_from, policy.spend_large_above)
    higher = by_spend if RANK[by_spend] >= RANK[by_resources] else by_resources
    return by_spend, by_resources, higher


def spend_month(end: date) -> date:
    """The first day of the last whole calendar month the range reaches: the end's
    own month when the end is its last day, else the month before."""
    if (end + timedelta(days=1)).day == 1:
        return end.replace(day=1)
    return (end.replace(day=1) - timedelta(days=1)).replace(day=1)


class SizePolicyView(ApiModel):
    version: Metric[int]
    currency: str
    spend_medium_from: Metric[Decimal]  # monthly; Medium from here, inclusive
    spend_large_above: Metric[Decimal]  # Large above this
    resources_medium_from: Metric[int]
    resources_large_above: Metric[int]
    set_at: datetime


class SizePolicyVersion(SizePolicyView):
    reason: str
    set_by_you: bool


_POLICY: Final = text(
    "SELECT version, spend_currency, spend_medium_from, spend_large_above,"
    "   resources_medium_from, resources_large_above, created_at, reason, set_by"
    " FROM cloudops.size_band_policy ORDER BY version DESC"
)


def _policy(row: tuple[Any, ...]) -> SizePolicy:
    v, ccy, smf, sla, rmf, rla, at = row[:7]
    return SizePolicy(int(v), str(ccy).strip(), Decimal(smf), Decimal(sla), int(rmf), int(rla),
                      at)


def _policy_view(p: SizePolicy) -> SizePolicyView:
    def m(v: int, unit: str, name: str) -> Metric[int]:
        return _count(v, unit, SIZE_POLICY, p.set_at, name, policy=p.version)

    def d(v: Decimal, name: str) -> Metric[Decimal]:
        return Metric[Decimal](value=v, unit=p.currency, source_system=SIZE_POLICY,
                               as_of=p.set_at, query_ref=query_ref(name, 1, policy=p.version),
                               is_modelled=False)

    return SizePolicyView(
        version=m(p.version, "version", "home.size_version"), currency=p.currency,
        spend_medium_from=d(p.spend_medium_from, "home.size_spend_medium"),
        spend_large_above=d(p.spend_large_above, "home.size_spend_large"),
        resources_medium_from=m(p.resources_medium_from, "resources",
                                "home.size_resources_medium"),
        resources_large_above=m(p.resources_large_above, "resources",
                                "home.size_resources_large"),
        set_at=p.set_at)


async def _current_policy(conn: ScopedConnection) -> SizePolicy:
    rows = _rows(await conn.execute(_POLICY))
    if not rows:
        raise ApiError(500, "no_size_policy")  # migration 0036 seeds version 1
    return _policy(rows[0])


# --- platform --------------------------------------------------------------------------


class ProviderCustomers(ApiModel):
    provider: Literal["azure", "aws"]
    customers: Metric[int]


class BandCustomers(ApiModel):
    band: SizeBand
    customers: Metric[int]


class CustomerSize(ApiModel):
    tenant: TenantView
    providers: list[str]
    resources: Metric[int]
    # The month's spend in the policy's currency. None: see spend_reason.
    spend: Metric[Decimal] | None
    spend_reason: Literal["not_permitted", "no_cost", "no_fx_rate"] | None
    spend_band: SizeBand | None
    resource_band: SizeBand
    band: SizeBand


class PlatformSection(ApiModel):
    range: DateRange
    customers: Metric[int]  # onboarded by the range's end, internal tenants apart
    internal: Metric[int]
    by_provider: list[ProviderCustomers]  # a customer on both clouds counts in each
    both_clouds: Metric[int]
    no_cloud: Metric[int]
    spend_month: str  # YYYY-MM: the month the spend band reads
    bands: list[BandCustomers]
    sizes: list[CustomerSize]  # largest first
    policy: SizePolicyView


# Onboarded: not a prospect and not exited, created by the end.
_TENANTS: Final = text(
    "SELECT t.id, t.canonical_name, t.internal,"
    "   array_remove(array_agg(DISTINCT a.provider), NULL)"
    " FROM cloudops.tenant t"
    " LEFT JOIN cloudops.cloud_account a ON a.tenant_id = t.id"
    "   AND (a.onboarded_at IS NULL OR a.onboarded_at < :hi)"
    " WHERE t.status NOT IN ('prospect', 'exited') AND t.created_at < :hi"
    " GROUP BY 1, 2, 3 ORDER BY 2"
)
_RESOURCES: Final = text(
    "SELECT tenant_id, count(*) FROM cloudops.resource"
    " WHERE first_seen < :hi AND (deleted_at IS NULL OR deleted_at >= :hi) GROUP BY 1"
)
_SPEND: Final = text(
    "SELECT c.tenant_id, CASE WHEN bool_and(r.rate IS NOT NULL)"  # noqa: S608 - constants
    "   THEN sum(c.amortized_cost * r.rate) END"
    " FROM cloudops.cost_fact c"
    " LEFT JOIN LATERAL (" + _rate("c", "c.usage_date") + ") r ON true"
    " WHERE c.usage_date BETWEEN :start AND :end GROUP BY 1"
)


@router.get("/home/sections/platform", response_model=PlatformSection)
@requires("inventory.read")
async def platform_section(request: Request, access: AccessDep, scope: ScopeDep,
                           start: Start = None, end: End = None) -> PlatformSection:
    _staff(access)
    now = _now()
    first, last = resolve_range(start, end, now.date())
    _, hi = _bounds(first, last)
    month = spend_month(last)
    month_end = (month + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    may_spend = access.allows("cost.read")
    async with platform(request).db.transaction(scope) as conn:
        policy = await _current_policy(conn)
        tenants = _rows(await conn.execute(_TENANTS, {"hi": hi}))
        resources = {UUID(str(t)): int(n) for t, n in _rows(
            await conn.execute(_RESOURCES, {"hi": hi}))}
        spend = {UUID(str(t)): s for t, s in _rows(await conn.execute(_SPEND, {
            "start": month, "end": month_end, "rep": policy.currency}))} if may_spend else {}
    at = min(now, hi)
    params: dict[str, Any] = {"start": first, "end": last}

    customers = [(UUID(str(t)), str(n), sorted(str(p) for p in provs))
                 for t, n, internal, provs in tenants if not internal]
    sizes: list[CustomerSize] = []
    for tid, name, provs in customers:
        n = resources.get(tid, 0)
        reason: Literal["not_permitted", "no_cost", "no_fx_rate"] | None = None
        amount: Decimal | None = None
        if not may_spend:
            reason = "not_permitted"
        elif tid not in spend:
            reason = "no_cost"
        elif spend[tid] is None:
            reason = "no_fx_rate"
        else:
            amount = Decimal(spend[tid])
        by_spend, by_resources, band = size_band(amount, n, policy)
        sizes.append(CustomerSize(
            tenant=TenantView(id=tid, name=name), providers=provs,
            resources=_count(n, "resources", INVENTORY, at, "home.size_resources",
                             tenant=str(tid), **params),
            spend=_dec(amount, policy.currency, COST_SOURCE, _end_of(month_end),
                       "home.size_spend", tenant=str(tid), month=month),
            spend_reason=reason, spend_band=by_spend, resource_band=by_resources, band=band))
    sizes.sort(key=lambda s: (-RANK[s.band], -(s.spend.value if s.spend else Decimal(-1)),
                              -s.resources.value, s.tenant.name))

    def cc(v: int, name: str, **p: Any) -> Metric[int]:
        return _count(v, "customers", INVENTORY, at, name, **params, **p)

    return PlatformSection(
        range=_range_view(first, last, now),
        customers=cc(len(customers), "home.customers"),
        internal=cc(sum(1 for t in tenants if t[2]), "home.internal_tenants"),
        by_provider=[ProviderCustomers(provider=p, customers=cc(
            sum(1 for _, _, provs in customers if p in provs), "home.customers_on", provider=p))
            for p in ("azure", "aws")],
        both_clouds=cc(sum(1 for _, _, provs in customers if {"azure", "aws"} <= set(provs)),
                       "home.customers_both"),
        no_cloud=cc(sum(1 for _, _, provs in customers if not provs), "home.customers_none"),
        spend_month=f"{month:%Y-%m}",
        bands=[BandCustomers(band=b, customers=cc(sum(1 for s in sizes if s.band == b),
                                                  "home.customers_in_band", band=b))
               for b in ("large", "medium", "small")],
        sizes=sizes, policy=_policy_view(policy))


# --- top customers ---------------------------------------------------------------------


Part = Literal["p1_p2", "availability", "vulnerabilities", "security_events"]
HealthBand = Literal["healthy", "watch", "at-risk", "critical"]


@dataclass(frozen=True)
class HealthPolicy:
    """The modelled customer health score (ADR 0038): 100 less four capped
    penalties, never below 0. Observed as at the range's end."""

    p1_points: int = 20  # each open P1 incident
    p2_points: int = 10  # each open P2 incident
    tickets_cap: int = 40
    availability_cap: int = 20  # times the share of critical resources unavailable
    vulnerability_points: int = 2  # each open severity 4 or 5 vulnerability
    vulnerabilities_cap: int = 20
    event_points: int = 5  # each open high-severity security event
    events_cap: int = 20
    healthy_from: int = 80
    watch_from: int = 60
    at_risk_from: int = 40


HEALTH_POLICY: Final = HealthPolicy()
# Inventory states that mean a resource is not serving (Azure power states, AWS
# instance states and the connectors' own); anything else, or none, counts as up.
UNAVAILABLE_STATES: Final = ("stopped", "deallocated", "failed", "unavailable", "degraded",
                             "terminated", "error")


@dataclass(frozen=True)
class HealthInputs:
    p1: int
    p2: int
    critical: int  # critical resources in inventory
    unavailable: int  # of which in an unavailable state
    major_vulnerabilities: int | None  # None: not permitted
    security_events: int | None  # None: not permitted


@dataclass(frozen=True)
class Health:
    score: Decimal
    band: HealthBand
    penalties: dict[Part, Decimal | None]  # None: the part was not observed


def health_score(i: HealthInputs, policy: HealthPolicy = HEALTH_POLICY) -> Health:
    tickets = Decimal(min(policy.tickets_cap, policy.p1_points * i.p1 + policy.p2_points * i.p2))
    availability = (None if i.critical == 0 else
                    Decimal(policy.availability_cap) * Decimal(i.unavailable) / Decimal(i.critical))
    vulns = (None if i.major_vulnerabilities is None else Decimal(min(
        policy.vulnerabilities_cap, policy.vulnerability_points * i.major_vulnerabilities)))
    events = (None if i.security_events is None else Decimal(min(
        policy.events_cap, policy.event_points * i.security_events)))
    penalties: dict[Part, Decimal | None] = {
        "p1_p2": tickets, "availability": availability, "vulnerabilities": vulns,
        "security_events": events}
    value = max(Decimal(0), Decimal(100) - sum((p for p in penalties.values() if p is not None),
                                                Decimal(0)))
    band: HealthBand = ("healthy" if value >= policy.healthy_from else
                        "watch" if value >= policy.watch_from else
                        "at-risk" if value >= policy.at_risk_from else "critical")
    return Health(value, band, penalties)


class HealthPart(ApiModel):
    part: Part
    observed: Metric[int] | None  # what was counted; None: not available to the caller
    penalty: Metric[Decimal] | None  # modelled; None: not observed, so no penalty
    cap: Metric[int]


class TopCustomer(ApiModel):
    rank: Metric[int]
    tenant: TenantView
    health: Metric[Decimal]  # modelled, 0..100
    health_band: HealthBand
    parts: list[HealthPart]


class TopCustomers(ApiModel):
    range: DateRange
    sort: Literal["health"]
    # Ranking by revenue reads the revenue table (ADR 0038); until it is merged, None
    # for a role without commercial.read, else pending.
    revenue: SourcePending | None
    ranked: Metric[int]  # customers considered
    customers: list[TopCustomer]


_TICKETS_AT: Final = text(
    f"SELECT w.tenant_id, count(*) FILTER (WHERE {_PRIORITY} = '1'),"  # noqa: S608 - constants
    f"   count(*) FILTER (WHERE {_PRIORITY} = '2')"
    " FROM cloudops.work_item w WHERE w.kind = 'incident' AND w.opened_at < :hi"
    "   AND (w.closed_at IS NULL OR w.closed_at >= :hi) GROUP BY 1"
)
_AVAILABILITY: Final = text(
    "SELECT tenant_id, count(*),"
    "   count(*) FILTER (WHERE lower(btrim(coalesce(state, ''))) = ANY(CAST(:down AS text[])))"
    " FROM cloudops.resource WHERE is_critical_ci"
    "   AND first_seen < :hi AND (deleted_at IS NULL OR deleted_at >= :hi) GROUP BY 1"
)
_MAJOR_VULNS: Final = text(
    f"SELECT tenant_id, count(*) FROM cloudops.vulnerability"  # noqa: S608 - constants
    f" WHERE severity >= 4 AND {_OPEN_AT} GROUP BY 1"
)
_HIGH_EVENTS: Final = text(
    f"SELECT tenant_id, count(*) FROM cloudops.security_finding"  # noqa: S608 - constants
    f" WHERE category = 'security' AND severity = 'high' AND {_EVENT_OPEN} GROUP BY 1"
)
_CUSTOMERS: Final = text(
    "SELECT id, canonical_name FROM cloudops.tenant WHERE NOT internal"
    " AND status NOT IN ('prospect', 'exited') ORDER BY canonical_name"
)
SortKey = Annotated[Literal["health"] | None, Query()]


@router.get("/home/sections/top-customers", response_model=TopCustomers)
@requires("tickets.read")
async def top_customers(request: Request, access: AccessDep, scope: ScopeDep,
                        start: Start = None, end: End = None, sort: SortKey = None,
                        ) -> TopCustomers:
    """The ten least healthy customers first: what needs attention. Ranking by
    revenue joins once the revenue table serves it."""
    _staff(access)
    order: Literal["health"] = sort or "health"
    now = _now()
    first, last = resolve_range(start, end, now.date())
    _, hi = _bounds(first, last)
    vulns_ok = access.allows("vulnerabilities.read")
    async with platform(request).db.transaction(scope) as conn:
        names = [(UUID(str(t)), str(n)) for t, n in _rows(await conn.execute(_CUSTOMERS))]
        tickets = {UUID(str(t)): (int(a), int(b)) for t, a, b in _rows(
            await conn.execute(_TICKETS_AT, {"hi": hi}))}
        avail = {UUID(str(t)): (int(a), int(b)) for t, a, b in _rows(await conn.execute(
            _AVAILABILITY, {"hi": hi, "down": list(UNAVAILABLE_STATES)}))}
        vulns = {UUID(str(t)): int(n) for t, n in _rows(
            await conn.execute(_MAJOR_VULNS, {"hi": hi}))} if vulns_ok else None
        events = {UUID(str(t)): int(n) for t, n in _rows(
            await conn.execute(_HIGH_EVENTS, {"hi": hi}))}
    at = min(now, hi)
    params: dict[str, Any] = {"start": first, "end": last}
    policy = HEALTH_POLICY

    rows: list[tuple[UUID, str, Health, HealthInputs, Decimal | None]] = []
    for tid, name in names:
        p1, p2 = tickets.get(tid, (0, 0))
        critical, down = avail.get(tid, (0, 0))
        inputs = HealthInputs(p1, p2, critical, down,
                              None if vulns is None else vulns.get(tid, 0), events.get(tid, 0))
        rows.append((tid, name, health_score(inputs, policy), inputs, None))
    rows = rank_customers(rows, order)[:TOP]

    def parts(tid: UUID, h: Health, i: HealthInputs) -> list[HealthPart]:
        def obs(v: int | None, unit: str, source: str, part: str) -> Metric[int] | None:
            return None if v is None else _count(v, unit, source, at, "home.health_observed",
                                                 part=part, tenant=str(tid), **params)

        def pen(part: Part) -> Metric[Decimal] | None:
            return _dec(h.penalties[part], "points", POLICY_SOURCE, at, "home.health_penalty",
                        modelled=True, part=part, tenant=str(tid), **params)

        def cap(v: int, part: str) -> Metric[int]:
            return _count(v, "points", POLICY_SOURCE, at, "home.health_cap", modelled=True,
                          part=part)

        return [
            HealthPart(part="p1_p2", observed=obs(i.p1 + i.p2, "incidents", TICKET_SOURCE,
                                                  "p1_p2"),
                       penalty=pen("p1_p2"), cap=cap(policy.tickets_cap, "p1_p2")),
            HealthPart(part="availability", observed=obs(i.unavailable, "resources", INVENTORY,
                                                         "availability"),
                       penalty=pen("availability"),
                       cap=cap(policy.availability_cap, "availability")),
            HealthPart(part="vulnerabilities",
                       observed=obs(i.major_vulnerabilities, "vulnerabilities", QUALYS,
                                    "vulnerabilities"),
                       penalty=pen("vulnerabilities"),
                       cap=cap(policy.vulnerabilities_cap, "vulnerabilities")),
            HealthPart(part="security_events",
                       observed=obs(i.security_events, "events", CORESTACK, "security_events"),
                       penalty=pen("security_events"),
                       cap=cap(policy.events_cap, "security_events")),
        ]

    out: list[TopCustomer] = []
    for n, (tid, name, health, inputs, _) in enumerate(rows, start=1):
        out.append(TopCustomer(
            rank=_count(n, "rank", POLICY_SOURCE, at, "home.top_rank", sort=order, **params),
            tenant=TenantView(id=tid, name=name),
            health=Metric[Decimal](value=health.score, unit="points", source_system=POLICY_SOURCE,
                                   as_of=at, query_ref=query_ref("home.health", 1,
                                                                 tenant=str(tid), **params),
                                   is_modelled=True),
            health_band=health.band, parts=parts(tid, health, inputs)))
    return TopCustomers(
        range=_range_view(first, last, now), sort=order,
        revenue=PENDING if access.allows("commercial.read") else None,
        ranked=_count(len(names), "customers", POLICY_SOURCE, at, "home.top_ranked", **params),
        customers=out)


def rank_customers(rows: list[tuple[UUID, str, Health, HealthInputs, Decimal | None]],
                   order: Literal["health", "revenue"]
                   ) -> list[tuple[UUID, str, Health, HealthInputs, Decimal | None]]:
    """``revenue``: largest first, unknown revenue last. ``health``: least healthy
    first, then the larger revenue. Ties fall to the name, so the order is stable.
    Revenue is the revenue table's figure in one currency once it is served; until
    then every row's is None and health decides."""
    def revenue_key(r: Decimal | None) -> tuple[int, Decimal]:
        return (1, Decimal(0)) if r is None else (0, -r)

    if order == "revenue":
        return sorted(rows, key=lambda r: (*revenue_key(r[4]), r[2].score, r[1]))
    return sorted(rows, key=lambda r: (r[2].score, *revenue_key(r[4]), r[1]))


# --- settings: size band thresholds ----------------------------------------------------


Money = Annotated[str, Field(pattern=r"^\d{1,12}(\.\d{1,6})?$")]
Count = Annotated[int, Field(ge=1, le=10_000_000)]


class SizeBandsInput(BaseModel):
    """A whole set of thresholds. Spend is a decimal string: a JSON number would
    arrive as a float (CLAUDE.md rule 5)."""

    model_config = ConfigDict(extra="forbid")

    spend_currency: str = Field(pattern=r"^[A-Z]{3}$")
    spend_medium_from: Money
    spend_large_above: Money
    resources_medium_from: Count
    resources_large_above: Count
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def _ordered(self) -> SizeBandsInput:
        if Decimal(self.spend_medium_from) <= 0:
            raise ValueError("Medium spend starts above zero")
        if Decimal(self.spend_large_above) < Decimal(self.spend_medium_from):
            raise ValueError("Large spend starts at or above Medium")
        if self.resources_large_above < self.resources_medium_from:
            raise ValueError("Large resources start at or above Medium")
        return self


class SizeBandSettings(ApiModel):
    current: SizePolicyView
    history: list[SizePolicyVersion]  # newest first
    can_edit: bool


async def _settings(conn: ScopedConnection, access: Access) -> SizeBandSettings:
    rows = _rows(await conn.execute(_POLICY))
    if not rows:
        raise ApiError(500, "no_size_policy")
    me = str(access.identity.oid)
    history = [SizePolicyVersion(**_policy_view(_policy(r)).model_dump(), reason=str(r[7]),
                                 set_by_you=str(r[8]) == me) for r in rows]
    return SizeBandSettings(current=_policy_view(_policy(rows[0])), history=history,
                            can_edit=access.allows("sizebands.manage"))


@router.get("/settings/size-bands", response_model=SizeBandSettings)
@requires("inventory.read")
async def size_bands(request: Request, access: AccessDep) -> SizeBandSettings:
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        return await _settings(conn, access)


_SET_REFUSALS: Final = {"42501": (403, "forbidden"), "23514": (422, "invalid_request"),
                        "22023": (422, "invalid_request")}


@router.post("/settings/size-bands", response_model=SizeBandSettings, status_code=201)
@requires("sizebands.manage")
async def set_size_bands(request: Request, body: SizeBandsInput,
                         access: AccessDep) -> SizeBandSettings:
    """A new version of the thresholds, in force from now. The version before it
    stays, with its reason; the request is in the audit log."""
    _staff(access)
    doc = body.model_dump(exclude={"reason"})
    try:
        async with platform(request).db.transaction(access.scope) as conn:
            await conn.scalar(text("SELECT access.set_size_bands(:actor, CAST(:doc AS jsonb),"
                                   " :why)"), {"actor": access.identity.oid,
                                               "doc": json.dumps(doc), "why": body.reason})
            return await _settings(conn, access)
    except DBAPIError as exc:
        refusal = _SET_REFUSALS.get(str(getattr(exc.orig, "sqlstate", "")))
        if refusal is None:
            raise
        raise ApiError(*refusal) from None
