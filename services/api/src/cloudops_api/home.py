"""Home dashboard summaries (ADR 0030; HLD, UI patterns and role-driven navigation).

The first screen after sign-in is a dashboard per role, built from widgets. A
widget reuses an existing endpoint where one answers it (``/v1/performance``,
``/v1/posture``, ``/v1/connectors/health``, ``/v1/commercial/margin`` and so on).
These four answer what no endpoint did, each in one call with aggregate queries
(no query per row):

- ``/v1/home/operations`` (``tickets.read``): open incidents by priority and SLA
  risk, recent changes, noisy CIs, backlog and MTTR by customer, and, for staff,
  by workgroup. Customers get the same view of their own tenant, naming only the
  columns ``cloudops_customer`` is granted (migration 0011): never a workgroup.
- ``/v1/home/triage`` (``triage.review``, staff): triage awaiting a person's review.
- ``/v1/home/cost`` (``cost.read``, staff): Getronics' cost by customer and month,
  anomalies, and reconciliation with provisional months marked. Internal tenants
  are listed like customers (ADR 0031). The platform's own cost is separate: the
  lines of its own resources (``rg-cloudops-*``) in the tenant that hosts it, and
  whether the host's feed has resource-level lines at all (when not, the
  platform's part is not captured).
- ``/v1/home/ai-usage`` (``cost.read``, staff): AI gateway tokens against the
  tenant's monthly quota, and spend. Spend is Getronics' cost, so it follows
  ``cost.read``; no customer role reads it (and ``staff_only`` hides the rows).

Same rules as ``bff``: a matrix permission per endpoint, queries under the
caller's resolved scope so row-level security decides what exists, and every
number a ``Metric`` with its source and as-of time.

**SLA risk is modelled** (user decision, ADR 0030). The platform holds whether
ServiceNow flagged a breach, but no due time. "At risk" is an open incident that
ServiceNow has not flagged, with at least ``AT_RISK_FRACTION`` of its priority's
modelled resolution target elapsed since it opened (wall clock; pauses are not
known). Those figures are ``is_modelled`` and name the platform policy as their
source. A priority without a target is never at risk, and says so.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text

from cloudops_api.access import Access
from cloudops_api.bff import TenantView
from cloudops_api.pipeline import ApiError, AuditedRoute, current_access, platform, requires
from cloudops_api.provenance import ApiModel, Metric, query_ref
from cloudops_commercial.finops import PLATFORM_GROUP

router = APIRouter(prefix="/v1/home", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

TICKET_SOURCE: Final = "servicenow"
POLICY_SOURCE: Final = "cloudops_policy"
COST_SOURCE: Final = "billing_exports"
GATEWAY_SOURCE: Final = "cloudops_ai_gateway"
CLASSIFIER_SOURCE: Final = "triage_classifier"

# Modelled resolution targets by ServiceNow priority digit, in hours (user decision,
# ADR 0030). Priority 5 (planning) has none.
SLA_TARGET_HOURS: Final[dict[str, int]] = {"1": 4, "2": 8, "3": 72, "4": 120}
AT_RISK_FRACTION: Final = Decimal("0.75")  # not money: elapsed over target
PRIORITIES: Final = ("1", "2", "3", "4", "5")
RECENT_DAYS: Final = 30
CHANGE_DAYS: Final = 7
TOP: Final = 5
TRIAGE_TOP: Final = 8
TREND_MONTHS: Final = 6
RECONCILED_MONTHS: Final = 3
# The classifier's own threshold (cloudops_agents.classifier; a test keeps them equal).
CONFIDENCE_THRESHOLD: Final = Decimal("0.7")  # not money: a classifier confidence
# The gateway's tenant quota (cloudops_gateway.Limits; a test keeps them equal).
TENANT_MONTHLY_TOKENS: Final = 5_000_000
ALERT_SHARE: Final = Decimal("0.8")  # not money: cloudops_gateway.Limits.alert_fraction
SECONDS_PER_HOUR: Final = Decimal(3600)  # not money: a unit conversion


def _now() -> datetime:
    return datetime.now(UTC)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _count(value: Any, unit: str, source: str, as_of: datetime, name: str, *,
           modelled: bool = False, **params: Any) -> Metric[int]:
    return Metric[int](value=int(value or 0), unit=unit, source_system=source, as_of=as_of,
                       query_ref=query_ref(name, 1, **params), is_modelled=modelled)


def _decimal(value: Any, unit: str, source: str, as_of: datetime, name: str, *,
              modelled: bool = False, **params: Any) -> Metric[Decimal] | None:
    if value is None:
        return None
    return Metric[Decimal](value=Decimal(value), unit=unit, source_system=source, as_of=as_of,
                           query_ref=query_ref(name, 1, **params), is_modelled=modelled)


def _staff(access: Access) -> None:
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


# --- operations ----------------------------------------------------------------------


Risk = Literal["breached", "at_risk", "within", "no_target"]


class PriorityLine(ApiModel):
    priority: str  # "1".."5", or "none": a label, not a measure
    open: Metric[int]
    breached: Metric[int]  # ServiceNow's flag, observed
    at_risk: Metric[int] | None  # modelled; None: the priority has no target
    target_hours: Metric[int] | None


class ClosestItem(ApiModel):
    number: str
    tenant: TenantView
    priority: str | None
    short_description: str | None
    opened_at: datetime
    risk: Risk
    elapsed_hours: Metric[Decimal]
    target_hours: Metric[int] | None


class ChangeRef(ApiModel):
    number: str
    tenant: TenantView
    short_description: str | None
    state: str | None
    configuration_item: str | None
    opened_at: datetime
    closed_at: datetime | None


class ChangeWeek(ApiModel):
    opened: Metric[int]
    closed: Metric[int]
    open_now: Metric[int]
    recent: list[ChangeRef]


class NoisyCi(ApiModel):
    configuration_item: str
    tenant: TenantView
    resource_id: UUID | None
    incidents: Metric[int]


class Backlog(ApiModel):
    open_items: Metric[int]
    open_incidents: Metric[int]
    breached_open: Metric[int]
    resolved_30d: Metric[int]
    # Mean resolve time of incidents closed in the last 30 days. None: none closed.
    mttr_hours: Metric[Decimal] | None


class TenantBacklog(Backlog):
    tenant: TenantView


class WorkgroupBacklog(Backlog):
    workgroup: str | None  # None: unassigned


class OperationsHome(ApiModel):
    as_of: datetime
    open_incidents: Metric[int]
    breached_open: Metric[int]  # every kind, ServiceNow's flag
    at_risk: Metric[int]  # modelled
    at_risk_fraction: Metric[Decimal]  # the policy: elapsed share that counts as at risk
    by_priority: list[PriorityLine]
    closest: list[ClosestItem]  # open incidents nearest (or past) their target
    changes: ChangeWeek
    noisy_cis: list[NoisyCi]
    by_customer: list[TenantBacklog]
    by_workgroup: list[WorkgroupBacklog] | None  # staff only: a customer never sees one


# The modelled target per priority digit, joined in from the policy above.
_TARGETS: Final = (
    "LEFT JOIN unnest(CAST(:pri AS text[]), CAST(:hrs AS int[])) AS tg(p, hours)"
    " ON tg.p = left(btrim(coalesce(w.priority, '')), 1)"
)
_ELAPSED: Final = "extract(epoch FROM (CAST(:now AS timestamptz) - w.opened_at))"
_AT_RISK: Final = (
    f"(NOT coalesce(w.sla_breached, false) AND tg.hours IS NOT NULL"
    f" AND {_ELAPSED} >= tg.hours * 3600 * CAST(:frac AS numeric))"
)
_BY_PRIORITY: Final = text(
    "SELECT left(btrim(coalesce(w.priority, '')), 1), count(*),"  # noqa: S608 - constants
    "   count(*) FILTER (WHERE w.sla_breached), count(*) FILTER (WHERE " + _AT_RISK + ")"
    " FROM cloudops.work_item w " + _TARGETS +
    " WHERE w.kind = 'incident' AND w.closed_at IS NULL GROUP BY 1"
)
_CLOSEST: Final = text(
    "SELECT w.number, t.id, t.canonical_name, w.priority, w.short_description, w.opened_at,"  # noqa: S608 - constants
    "   coalesce(w.sla_breached, false), tg.hours, " + _ELAPSED +
    " FROM cloudops.work_item w JOIN cloudops.tenant t ON t.id = w.tenant_id " + _TARGETS +
    " WHERE w.kind = 'incident' AND w.closed_at IS NULL"
    # Breached first, then the largest share of the target used, then the oldest.
    " ORDER BY coalesce(w.sla_breached, false) DESC,"
    "   " + _ELAPSED + " / nullif(tg.hours * 3600, 0) DESC NULLS LAST, w.opened_at, w.number"
    " LIMIT :top"
)
_BREACHED_OPEN: Final = text(
    "SELECT count(*) FROM cloudops.work_item WHERE closed_at IS NULL AND sla_breached"
)
_CHANGE_COUNTS: Final = text(
    "SELECT count(*) FILTER (WHERE opened_at >= :since),"
    "   count(*) FILTER (WHERE closed_at >= :since), count(*) FILTER (WHERE closed_at IS NULL)"
    " FROM cloudops.work_item WHERE kind = 'change_task'"
    "   AND (closed_at IS NULL OR closed_at >= :since OR opened_at >= :since)"
)
_RECENT_CHANGES: Final = text(
    "SELECT w.number, t.id, t.canonical_name, w.short_description, w.state,"
    "   w.configuration_item, w.opened_at, w.closed_at"
    " FROM cloudops.work_item w JOIN cloudops.tenant t ON t.id = w.tenant_id"
    " WHERE w.kind = 'change_task' AND (w.opened_at >= :since OR w.closed_at >= :since)"
    " ORDER BY greatest(w.opened_at, coalesce(w.closed_at, w.opened_at)) DESC, w.number"
    " LIMIT :top"
)
_NOISY: Final = text(
    "SELECT w.configuration_item, t.id, t.canonical_name,"
    "   (array_agg(w.resource_id ORDER BY w.opened_at DESC)"
    "     FILTER (WHERE w.resource_id IS NOT NULL))[1], count(*)"
    " FROM cloudops.work_item w JOIN cloudops.tenant t ON t.id = w.tenant_id"
    " WHERE w.kind = 'incident' AND w.opened_at >= :since"
    "   AND nullif(btrim(w.configuration_item), '') IS NOT NULL"
    " GROUP BY 1, 2, 3 ORDER BY 5 DESC, 1 LIMIT :top"
)
_BACKLOG_COLUMNS: Final = (
    "count(*) FILTER (WHERE closed_at IS NULL),"
    " count(*) FILTER (WHERE closed_at IS NULL AND kind = 'incident'),"
    " count(*) FILTER (WHERE closed_at IS NULL AND sla_breached),"
    " count(*) FILTER (WHERE kind = 'incident' AND closed_at >= :since),"
    " avg(resolve_time_seconds) FILTER (WHERE kind = 'incident' AND closed_at >= :since)"
)
# Open items, or incidents resolved in the window: nothing else moves these figures.
_BACKLOG_WHERE: Final = (
    "closed_at IS NULL OR (kind = 'incident' AND closed_at >= :since)"
)
_BY_TENANT: Final = text(
    f"SELECT tenant_id, {_BACKLOG_COLUMNS} FROM cloudops.work_item"  # noqa: S608 - constants
    f" WHERE {_BACKLOG_WHERE} GROUP BY 1"
)
# Staff only: cloudops_customer is refused the workgroup column (migration 0011).
_BY_WORKGROUP: Final = text(
    f"SELECT assignment_workgroup, {_BACKLOG_COLUMNS} FROM cloudops.work_item"  # noqa: S608
    f" WHERE {_BACKLOG_WHERE} GROUP BY 1 ORDER BY 2 DESC, 1 NULLS LAST"
)


def _backlog(row: tuple[Any, ...], as_of: datetime, **params: Any) -> dict[str, Any]:
    open_items, open_incidents, breached, resolved, mean_seconds = row

    def count(v: Any, unit: str, name: str) -> Metric[int]:
        return _count(v, unit, TICKET_SOURCE, as_of, name, **params)

    return {
        "open_items": count(open_items, "work items", "home.backlog_open"),
        "open_incidents": count(open_incidents, "incidents", "home.backlog_open_incidents"),
        "breached_open": count(breached, "work items", "home.backlog_breached"),
        "resolved_30d": count(resolved, "incidents", "home.resolved_30d"),
        "mttr_hours": _decimal(None if mean_seconds is None
                               else Decimal(mean_seconds) / SECONDS_PER_HOUR,
                               "hours", TICKET_SOURCE, as_of, "home.mttr_30d", **params),
    }


@router.get("/operations", response_model=OperationsHome)
@requires("tickets.read")
async def operations(request: Request, access: AccessDep) -> OperationsHome:
    staff = access.kind == "staff"
    now = _now()
    recent = now - timedelta(days=RECENT_DAYS)
    week = now - timedelta(days=CHANGE_DAYS)
    policy = {"pri": list(SLA_TARGET_HOURS), "hrs": list(SLA_TARGET_HOURS.values()),
              "frac": AT_RISK_FRACTION, "now": now}
    async with platform(request).db.transaction(access.scope) as conn:
        by_priority = _rows(await conn.execute(_BY_PRIORITY, policy))
        closest = _rows(await conn.execute(_CLOSEST, {**policy, "top": TOP}))
        breached_open = await conn.scalar(_BREACHED_OPEN)
        change_counts = tuple((await conn.execute(_CHANGE_COUNTS, {"since": week})).one())
        changes = _rows(await conn.execute(_RECENT_CHANGES, {"since": week, "top": TOP}))
        noisy = _rows(await conn.execute(_NOISY, {"since": recent, "top": TOP}))
        tenants = {UUID(str(r[0])): str(r[1]) for r in _rows(await conn.execute(text(
            "SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")))}
        by_tenant = {UUID(str(r[0])): r[1:] for r in _rows(
            await conn.execute(_BY_TENANT, {"since": recent}))}
        by_workgroup = _rows(await conn.execute(_BY_WORKGROUP, {"since": recent})) if staff \
            else None

    def target(p: str) -> Metric[int] | None:
        hours = SLA_TARGET_HOURS.get(p)
        if hours is None:
            return None
        return _count(hours, "hours", POLICY_SOURCE, now, "home.sla_target", modelled=True,
                      priority=p)

    # A priority that is not a ServiceNow digit counts as none, never dropped.
    counted: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for p, *figures in by_priority:
        key = str(p) if str(p) in PRIORITIES else "none"
        counted[key] = [a + int(b) for a, b in zip(counted[key], figures, strict=True)]
    lines: list[PriorityLine] = []
    for p in (*PRIORITIES, "none"):
        if p not in counted and p not in PRIORITIES:
            continue
        total, breached, risk = counted.get(p, [0, 0, 0])
        lines.append(PriorityLine(
            priority=p,
            open=_count(total, "incidents", TICKET_SOURCE, now, "home.open_by_priority",
                        priority=p),
            breached=_count(breached, "incidents", TICKET_SOURCE, now,
                            "home.breached_by_priority", priority=p),
            at_risk=_count(risk, "incidents", POLICY_SOURCE, now, "home.at_risk_by_priority",
                           modelled=True, priority=p) if p in SLA_TARGET_HOURS else None,
            target_hours=target(p)))

    def risk_of(breached: bool, hours: int | None, elapsed: Decimal) -> Risk:
        if breached:
            return "breached"
        if hours is None:
            return "no_target"
        return "at_risk" if elapsed >= hours * 3600 * AT_RISK_FRACTION else "within"

    return OperationsHome(
        as_of=now,
        open_incidents=_count(sum(int(r[1]) for r in by_priority), "incidents", TICKET_SOURCE,
                              now, "home.open_incidents"),
        breached_open=_count(breached_open, "work items", TICKET_SOURCE, now,
                             "home.breached_open"),
        at_risk=_count(sum(int(r[3]) for r in by_priority), "incidents", POLICY_SOURCE, now,
                       "home.at_risk", modelled=True),
        at_risk_fraction=Metric[Decimal](
            value=AT_RISK_FRACTION, unit="ratio", source_system=POLICY_SOURCE, as_of=now,
            query_ref=query_ref("home.at_risk_fraction", 1), is_modelled=True),
        by_priority=lines,
        closest=[
            ClosestItem(
                number=number, tenant=TenantView(id=tid, name=tname), priority=priority,
                short_description=short, opened_at=opened,
                risk=risk_of(bool(breached), hours, Decimal(elapsed)),
                elapsed_hours=Metric[Decimal](
                    value=Decimal(elapsed) / SECONDS_PER_HOUR, unit="hours",
                    source_system=TICKET_SOURCE, as_of=now,
                    query_ref=query_ref("home.elapsed", 1, number=number), is_modelled=False),
                target_hours=target(str(priority or "").strip()[:1]))
            for number, tid, tname, priority, short, opened, breached, hours, elapsed in closest
        ],
        changes=ChangeWeek(
            opened=_count(change_counts[0], "changes", TICKET_SOURCE, now, "home.changes_opened",
                          days=CHANGE_DAYS),
            closed=_count(change_counts[1], "changes", TICKET_SOURCE, now, "home.changes_closed",
                          days=CHANGE_DAYS),
            open_now=_count(change_counts[2], "changes", TICKET_SOURCE, now, "home.changes_open"),
            recent=[ChangeRef(number=r[0], tenant=TenantView(id=r[1], name=r[2]),
                              short_description=r[3], state=r[4], configuration_item=r[5],
                              opened_at=r[6], closed_at=r[7]) for r in changes]),
        noisy_cis=[
            NoisyCi(configuration_item=ci, tenant=TenantView(id=tid, name=tname),
                    resource_id=rid,
                    incidents=_count(n, "incidents", TICKET_SOURCE, now, "home.noisy_ci",
                                     days=RECENT_DAYS, ci=ci))
            for ci, tid, tname, rid, n in noisy
        ],
        by_customer=[
            TenantBacklog(tenant=TenantView(id=tid, name=name),
                          **_backlog(by_tenant.get(tid, (0, 0, 0, 0, None)), now,
                                     tenant=str(tid)))
            for tid, name in tenants.items()
        ],
        by_workgroup=None if by_workgroup is None else [
            WorkgroupBacklog(workgroup=r[0], **_backlog(r[1:], now, workgroup=r[0]))
            for r in by_workgroup
        ],
    )


# --- triage --------------------------------------------------------------------------


class TriageQueueItem(ApiModel):
    number: str
    tenant: TenantView
    route: Literal["ready_for_planning", "human"]
    intent: str | None
    short_description: str | None
    priority: str | None
    confidence: Metric[Decimal] | None  # None: the classifier gave none
    below_threshold: bool
    triaged_at: datetime


class TriageHome(ApiModel):
    awaiting: Metric[int]
    needs_person: Metric[int]  # routed to a person by the pipeline
    low_confidence: Metric[int]
    threshold: Metric[Decimal]
    items: list[TriageQueueItem]


# The latest triage of each open ticket, when no person has reviewed it yet.
_AWAITING: Final = (
    "WITH latest AS ("
    "  SELECT DISTINCT ON (tr.tenant_id, tr.number) tr.id, tr.tenant_id, tr.number, tr.route,"
    "    tr.classification, tr.created_at"
    "  FROM cloudops.triage_result tr ORDER BY tr.tenant_id, tr.number, tr.created_at DESC),"
    " awaiting AS ("
    "  SELECT l.*, CASE WHEN jsonb_typeof(l.classification -> 'confidence') = 'number'"
    "      THEN (l.classification ->> 'confidence')::numeric END AS confidence,"
    "    w.short_description, w.priority"
    "  FROM latest l"
    "  JOIN cloudops.work_item w ON w.tenant_id = l.tenant_id AND w.number = l.number"
    "   AND w.closed_at IS NULL"
    "  WHERE NOT EXISTS (SELECT 1 FROM cloudops.triage_review r WHERE r.triage_result_id = l.id))"
)
_TRIAGE_COUNTS: Final = text(
    _AWAITING + " SELECT count(*), count(*) FILTER (WHERE route = 'human'),"  # noqa: S608
    " count(*) FILTER (WHERE confidence IS NULL OR confidence < :threshold) FROM awaiting"
)
_TRIAGE_ITEMS: Final = text(
    _AWAITING + " SELECT a.number, t.id, t.canonical_name, a.route,"  # noqa: S608
    "   a.classification ->> 'intent', a.short_description, a.priority, a.confidence,"
    "   a.created_at"
    " FROM awaiting a JOIN cloudops.tenant t ON t.id = a.tenant_id"
    # A person's queue first, least confident first, then the longest waiting.
    " ORDER BY a.route = 'human' DESC, a.confidence ASC NULLS FIRST, a.created_at, a.number"
    " LIMIT :top"
)


@router.get("/triage", response_model=TriageHome)
@requires("triage.review")
async def triage(request: Request, access: AccessDep) -> TriageHome:
    _staff(access)  # triage is staff-only (ADR 0014); staff_only RLS holds it anyway
    now = _now()
    params = {"threshold": CONFIDENCE_THRESHOLD}
    async with platform(request).db.transaction(access.scope) as conn:
        total, person, low = tuple((await conn.execute(_TRIAGE_COUNTS, params)).one())
        items = _rows(await conn.execute(_TRIAGE_ITEMS, {"top": TRIAGE_TOP}))

    def confidence(value: Any, number: str) -> Metric[Decimal] | None:
        return _decimal(value, "ratio", CLASSIFIER_SOURCE, now, "home.triage_confidence",
                        number=number)

    return TriageHome(
        awaiting=_count(total, "tickets", CLASSIFIER_SOURCE, now, "home.triage_awaiting"),
        needs_person=_count(person, "tickets", CLASSIFIER_SOURCE, now, "home.triage_human"),
        low_confidence=_count(low, "tickets", CLASSIFIER_SOURCE, now, "home.triage_low"),
        threshold=Metric[Decimal](value=CONFIDENCE_THRESHOLD, unit="ratio",
                                  source_system=POLICY_SOURCE, as_of=now,
                                  query_ref=query_ref("home.triage_threshold", 1),
                                  is_modelled=False),
        items=[
            TriageQueueItem(
                number=number, tenant=TenantView(id=tid, name=tname), route=route,
                intent=intent, short_description=short, priority=priority,
                confidence=confidence(conf, number),
                below_threshold=conf is None or Decimal(conf) < CONFIDENCE_THRESHOLD,
                triaged_at=created)
            for number, tid, tname, route, intent, short, priority, conf, created in items
        ],
    )


# --- cost ----------------------------------------------------------------------------


ReconciliationState = Literal["final", "provisional", "unverified", "unchecked", "no_cost"]


class CostPoint(ApiModel):
    period: str  # YYYY-MM
    month_to_date: bool  # the current month: not complete
    amortized: Metric[Decimal] | None  # None: no cost rows that month, not zero


class CurrencyTrend(ApiModel):
    currency: str
    points: list[CostPoint]


class MonthReconciliation(ApiModel):
    """``final``: every account with cost checked against its invoice and verified.
    ``provisional``: verified against the provider's running total, no invoice yet.
    ``unverified``: a check failed. ``unchecked``: an account with cost has no check."""

    period: str
    state: ReconciliationState
    accounts: Metric[int]  # accounts with cost that month
    verified: Metric[int]


class CustomerCostHome(ApiModel):
    tenant: TenantView
    internal: bool  # internal to Getronics; reported like a customer (ADR 0031)
    hosts_platform: bool  # the platform runs in its subscription
    currencies: list[CurrencyTrend]
    anomalies_30d: Metric[int]
    last_anomaly_on: date | None
    reconciliation: list[MonthReconciliation]


class PlatformCostHome(ApiModel):
    """The platform's own resources' cost in its host tenant (ADR 0031)."""

    host: TenantView
    captured: bool  # this month's host cost has resource-level lines to tell it apart
    currencies: list[CurrencyTrend]


class CostHome(ApiModel):
    months: list[str]
    customers: list[CustomerCostHome]
    platform: PlatformCostHome | None  # None: no tenant hosts the platform


_COST_TREND: Final = text(
    "SELECT tenant_id, invoice_period, currency, sum(amortized_cost), max(usage_date)"
    " FROM cloudops.cost_fact WHERE invoice_period = ANY(CAST(:periods AS text[]))"
    " GROUP BY 1, 2, 3"
)
_RECON_CHECKS: Final = text(
    "SELECT DISTINCT ON (r.account_id, r.invoice_period) r.tenant_id, r.account_id,"
    "   r.invoice_period, r.kind, r.status"
    " FROM cloudops.cost_reconciliation r WHERE r.invoice_period = ANY(CAST(:periods AS text[]))"
    " ORDER BY r.account_id, r.invoice_period, (r.kind = 'invoice') DESC, r.checked_on DESC"
)
_ACCOUNTS_WITH_COST: Final = text(
    "SELECT DISTINCT tenant_id, account_id, invoice_period FROM cloudops.cost_fact"
    " WHERE invoice_period = ANY(CAST(:periods AS text[]))"
)
_ANOMALIES: Final = text(
    "SELECT tenant_id, count(*), max(observed_on) FROM cloudops.cost_anomaly"
    " WHERE observed_on >= :since GROUP BY 1"
)
# As platform_metrics counts it: lines of resources in a platform resource group.
PLATFORM_TREND: Final = text(
    "SELECT c.invoice_period, c.currency, sum(c.amortized_cost), max(c.usage_date)"
    " FROM cloudops.cost_fact c JOIN cloudops.resource r ON r.id = c.resource_id"
    " WHERE c.tenant_id = :tenant AND c.invoice_period = ANY(CAST(:periods AS text[]))"
    "   AND lower(r.native_id) ~ :group GROUP BY 1, 2"
)
RESOURCE_LINES: Final = text(
    "SELECT EXISTS (SELECT 1 FROM cloudops.cost_fact c WHERE c.tenant_id = :tenant"
    " AND c.invoice_period = :period AND c.resource_id IS NOT NULL)"
)


def month_periods(today: date, months: int) -> list[str]:
    out: list[str] = []
    y, m = today.year, today.month
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def _month_state(accounts: set[UUID], checks: dict[UUID, tuple[str, str]]) -> ReconciliationState:
    if not accounts:
        return "no_cost"
    if any(a not in checks for a in accounts):
        return "unchecked"
    if any(status != "verified" for _, status in checks.values()):
        return "unverified"
    if all(kind == "invoice" for kind, _ in checks.values()):
        return "final"
    return "provisional"


@router.get("/cost", response_model=CostHome)
@requires("cost.read")
async def cost(request: Request, access: AccessDep) -> CostHome:
    """Getronics' cost per customer: staff only. A customer reads the price it is
    charged through ``/v1/cost/summary`` (ADR 0021), never this."""
    _staff(access)
    now = _now()
    periods = month_periods(now.date(), TREND_MONTHS)
    checked = periods[-RECONCILED_MONTHS:]
    async with platform(request).db.transaction(access.scope) as conn:
        tenants = [(UUID(str(r[0])), str(r[1]), bool(r[2]), bool(r[3]))
                   for r in _rows(await conn.execute(text(
                       "SELECT id, canonical_name, internal, hosts_platform FROM cloudops.tenant"
                       " ORDER BY canonical_name")))]
        trend = _rows(await conn.execute(_COST_TREND, {"periods": periods}))
        checks = _rows(await conn.execute(_RECON_CHECKS, {"periods": checked}))
        with_cost = _rows(await conn.execute(_ACCOUNTS_WITH_COST, {"periods": checked}))
        anomalies = {UUID(str(r[0])): (int(r[1]), r[2]) for r in _rows(await conn.execute(
            _ANOMALIES, {"since": now.date() - timedelta(days=RECENT_DAYS)}))}
        host = next(((t, name) for t, name, _, hosts in tenants if hosts), None)
        platform_rows: list[tuple[Any, ...]] = []
        captured = False
        if host is not None:
            params = {"tenant": host[0], "periods": periods, "group": PLATFORM_GROUP}
            platform_rows = _rows(await conn.execute(PLATFORM_TREND, params))
            captured = bool(await conn.scalar(RESOURCE_LINES,
                                              {"tenant": host[0], "period": periods[-1]}))

    amounts: dict[UUID, dict[str, dict[str, tuple[Any, date]]]] = defaultdict(
        lambda: defaultdict(dict))
    for tid, period, ccy, total, last in trend:
        amounts[UUID(str(tid))][str(ccy).strip()][str(period)] = (total, last)
    check_by: dict[tuple[UUID, str], dict[UUID, tuple[str, str]]] = defaultdict(dict)
    for tid, acct, period, kind, status in checks:
        check_by[(UUID(str(tid)), str(period))][UUID(str(acct))] = (str(kind), str(status))
    accounts_by: dict[tuple[UUID, str], set[UUID]] = defaultdict(set)
    for tid, acct, period in with_cost:
        accounts_by[(UUID(str(tid)), str(period))].add(UUID(str(acct)))
    current = periods[-1]

    def point(tid: UUID, ccy: str, period: str) -> CostPoint:
        found = amounts[tid][ccy].get(period)
        as_of = (datetime.combine(found[1] + timedelta(days=1), time.min, UTC)
                 if found else now)
        return CostPoint(period=period, month_to_date=period == current,
                         amortized=None if found is None else _decimal(
                             found[0], ccy, COST_SOURCE, as_of, "home.cost_month",
                             tenant=str(tid), period=period))

    def month(tid: UUID, period: str) -> MonthReconciliation:
        accounts = accounts_by.get((tid, period), set())
        seen = {a: c for a, c in check_by.get((tid, period), {}).items() if a in accounts}
        verified = sum(1 for _, status in seen.values() if status == "verified")
        params: dict[str, Any] = {"tenant": str(tid), "period": period}
        return MonthReconciliation(
            period=period, state=_month_state(accounts, seen),
            accounts=_count(len(accounts), "accounts", COST_SOURCE, now,
                            "home.reconciled_accounts", **params),
            verified=_count(verified, "accounts", "reconciliation", now,
                            "home.verified_accounts", **params))

    customers: list[CustomerCostHome] = []
    for tid, name, internal, hosts in tenants:
        count, last_on = anomalies.get(tid, (0, None))
        customers.append(CustomerCostHome(
            tenant=TenantView(id=tid, name=name), internal=internal, hosts_platform=hosts,
            currencies=[CurrencyTrend(currency=ccy, points=[point(tid, ccy, p) for p in periods])
                        for ccy in sorted(amounts.get(tid, {}))],
            anomalies_30d=_count(count, "anomalies", COST_SOURCE, now, "home.anomalies_30d",
                                 tenant=str(tid)),
            last_anomaly_on=last_on,
            reconciliation=[month(tid, p) for p in checked]))
    platform_cost = None
    if host is not None:
        own: dict[str, dict[str, tuple[Any, date]]] = defaultdict(dict)
        for period, ccy, total, last in platform_rows:
            own[str(ccy).strip()][str(period)] = (total, last)

        def own_point(ccy: str, period: str) -> CostPoint:
            found = own[ccy].get(period)
            as_of = (datetime.combine(found[1] + timedelta(days=1), time.min, UTC)
                     if found else now)
            return CostPoint(period=period, month_to_date=period == current,
                             amortized=None if found is None else _decimal(
                                 found[0], ccy, COST_SOURCE, as_of, "home.platform_cost_month",
                                 period=period))

        platform_cost = PlatformCostHome(
            host=TenantView(id=host[0], name=host[1]), captured=captured,
            currencies=[CurrencyTrend(currency=ccy, points=[own_point(ccy, p) for p in periods])
                        for ccy in sorted(own)])
    return CostHome(months=periods, customers=customers, platform=platform_cost)


# --- AI gateway usage ----------------------------------------------------------------


class AiSpend(ApiModel):
    currency: str
    spend: Metric[Decimal]


class AiTenantUsage(ApiModel):
    tenant: TenantView
    tokens: Metric[int]
    quota: Metric[int]
    used_share: Metric[Decimal]  # tokens over quota
    calls: Metric[int]
    refused: Metric[int]
    spend: list[AiSpend]


class AiUsageHome(ApiModel):
    month: str  # YYYY-MM, UTC, as the gateway counts it
    alert_share: Metric[Decimal]  # the gateway warns from here
    tenants: list[AiTenantUsage]


# As the gateway counts a tenant's month (cloudops_gateway.gateway._USAGE).
_AI_USAGE: Final = text(
    "SELECT tenant_id, cost_currency, sum(tokens_in + tokens_out), count(*),"
    "   count(*) FILTER (WHERE outcome = 'refused'), sum(cost), max(created_at)"
    " FROM cloudops.llm_call WHERE created_at >= date_trunc('month', now())"
    " GROUP BY 1, 2"
)


@router.get("/ai-usage", response_model=AiUsageHome)
@requires("cost.read")
async def ai_usage(request: Request, access: AccessDep) -> AiUsageHome:
    _staff(access)
    now = _now()
    async with platform(request).db.transaction(access.scope) as conn:
        names = {UUID(str(r[0])): str(r[1]) for r in _rows(await conn.execute(text(
            "SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")))}
        rows = _rows(await conn.execute(_AI_USAGE))
    totals: dict[UUID, list[int]] = defaultdict(lambda: [0, 0, 0])
    spend: dict[UUID, list[tuple[str, Any, datetime]]] = defaultdict(list)
    for tid, ccy, tokens, calls, refused, cost_sum, last in rows:
        key = UUID(str(tid))
        t = totals[key]
        t[0] += int(tokens or 0)
        t[1] += int(calls or 0)
        t[2] += int(refused or 0)
        spend[key].append((str(ccy).strip(), cost_sum, last))
    out: list[AiTenantUsage] = []
    for tid, name in names.items():
        tokens, calls, refused = totals.get(tid, [0, 0, 0])
        as_of = now
        params: dict[str, Any] = {"tenant": str(tid)}
        out.append(AiTenantUsage(
            tenant=TenantView(id=tid, name=name),
            tokens=_count(tokens, "tokens", GATEWAY_SOURCE, as_of, "home.ai_tokens", **params),
            quota=_count(TENANT_MONTHLY_TOKENS, "tokens", POLICY_SOURCE, as_of, "home.ai_quota"),
            used_share=Metric[Decimal](
                value=Decimal(tokens) / Decimal(TENANT_MONTHLY_TOKENS), unit="ratio",
                source_system=GATEWAY_SOURCE, as_of=as_of,
                query_ref=query_ref("home.ai_share", 1, **params), is_modelled=False),
            calls=_count(calls, "calls", GATEWAY_SOURCE, as_of, "home.ai_calls", **params),
            refused=_count(refused, "calls", GATEWAY_SOURCE, as_of, "home.ai_refused", **params),
            spend=[AiSpend(currency=ccy, spend=Metric[Decimal](
                value=Decimal(total), unit=ccy, source_system=GATEWAY_SOURCE, as_of=last_at,
                query_ref=query_ref("home.ai_spend", 1, **params), is_modelled=False))
                for ccy, total, last_at in sorted(spend.get(tid, []))]))
    return AiUsageHome(
        month=f"{now:%Y-%m}",
        alert_share=Metric[Decimal](value=ALERT_SHARE, unit="ratio",
                                    source_system=POLICY_SOURCE, as_of=now,
                                    query_ref=query_ref("home.ai_alert_share", 1),
                                    is_modelled=False),
        tenants=out)
