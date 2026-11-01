"""Platform observability: how the platform itself is used and behaving (console
``/observability``; HLD, Observability; ADR 0022).

Five endpoints, one per section of the page, so a slow or refused section never
blanks the others. Each needs ``platform.observe`` (migration 0032), a staff
matrix permission no customer role can hold; ``_staff`` refuses a customer here as
well. Every query runs under the caller's resolved scope (``ScopeDep``), so
row-level security decides which tenants' rows exist, and ``tenant_id`` (the
console's customer selector, ADR 0032) narrows that scope to one of its own
tenants, never widens it.

- ``/usage``: active users by role per day and week, console sessions, and the API
  endpoints most used, from ``audit.action_log``. The log records API requests, not
  screens: a request is logged once per tenant in the caller's scope, so a request
  is counted once by (actor, action, time). Sessions are *modelled*: the console
  asks ``GET /v1/me`` once per page load, and that is all the platform sees of a
  sign-in (Entra holds the sign-ins themselves). Roles come from
  ``access.observed_roles``, for the actors already read, never others.
  Requests made with no tenant in scope (``audit.platform_action_log``) are not
  counted; only auditors read that log.
- ``/ai``: AI gateway calls, tokens and spend per day, per agent and per customer,
  from ``llm_call`` (ADR 0005), with each customer's month against the tenant quota
  and the assistant's sub-budget (ADR 0029). Spend is Getronics' cost: without
  ``cost.read`` it is absent, never zero.
- ``/triage``: triage runs and their route, and reviews accepted or overridden.
- ``/connectors``: connector health per source, from ``access.connector_health``
  (``connectors.read``; the Connectors page holds the detail). Per source, not per
  tenant: the selector does not narrow it.
- ``/platform-cost``: the platform's own resources' cost in the tenant that hosts it
  (ADR 0023, ADR 0031), as ``/v1/home/cost`` counts it. ``cost.read`` as well.

Nothing here writes a tenant id or a customer name into telemetry (ADR 0022): the
figures leave only in the response, under the audited pipeline.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_api import connectors, home
from cloudops_api.access import Access
from cloudops_api.bff import TenantView
from cloudops_api.home import AiSpend, CostPoint, CurrencyTrend
from cloudops_api.pipeline import (
    ApiError,
    AuditedRoute,
    ScopeDep,
    current_access,
    platform,
    requires,
)
from cloudops_api.provenance import ApiModel, Metric, query_ref
from cloudops_commercial.finops import PLATFORM_GROUP
from cloudops_tenancy import TenantScope

router = APIRouter(prefix="/v1/observability", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

OBSERVE: Final = "platform.observe"
AUDIT_SOURCE: Final = "audit_log"
GATEWAY_SOURCE: Final = home.GATEWAY_SOURCE
POLICY_SOURCE: Final = home.POLICY_SOURCE
TRIAGE_SOURCE: Final = home.CLASSIFIER_SOURCE
REVIEW_SOURCE: Final = "triage_review"
COST_SOURCE: Final = home.COST_SOURCE

DEFAULT_DAYS: Final = 30
MAX_DAYS: Final = 90
TOP_ENDPOINTS: Final = 10
# The console asks who it is once per page load: the nearest thing to a sign-in
# the platform records.
SESSION_ACTION: Final = "GET /v1/me"
# The assistant's sub-budget (cloudops_gateway.Limits.sub_budgets; a test keeps it equal).
ASSISTANT_PREFIX: Final = "assistant."
ASSISTANT_MONTHLY_SHARE: Final = Decimal("0.4")  # not money: a share of the tenant quota

Agent = Literal["classifier", "enricher", "drafter", "assistant", "other"]
AGENTS: Final[tuple[Agent, ...]] = ("classifier", "enricher", "drafter", "assistant", "other")
# A gateway task to the agent that made it. The cache's lookups carry "cache:".
_AGENT: Final = (
    "CASE regexp_replace(task, '^cache:', '')"
    " WHEN 'classify_ticket' THEN 'classifier'"
    " WHEN 'enrich_ticket' THEN 'enricher'"
    " ELSE CASE WHEN regexp_replace(task, '^cache:', '') LIKE 'draft\\_%' THEN 'drafter'"
    "           WHEN regexp_replace(task, '^cache:', '') LIKE 'assistant.%' THEN 'assistant'"
    "           ELSE 'other' END END"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _staff(access: Access) -> None:
    # No customer role holds platform.observe (migration 0032); this keeps it so.
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


def _chosen(access: Access, scope: TenantScope) -> UUID | None:
    """The one tenant the customer selector narrowed to (``narrow``), if any."""
    if scope.tenant_ids == access.scope.tenant_ids or len(scope.tenant_ids) != 1:
        return None
    return next(iter(scope.tenant_ids))


DaysParam = Annotated[int, Query(ge=1, le=MAX_DAYS)]


def _count(value: Any, unit: str, source: str, as_of: datetime, name: str, *,
           modelled: bool = False, **params: Any) -> Metric[int]:
    return Metric[int](value=int(value or 0), unit=unit, source_system=source, as_of=as_of,
                       query_ref=query_ref(name, 1, **params), is_modelled=modelled)


def _ratio(part: int, whole: Decimal | int, source: str, as_of: datetime, name: str, *,
           modelled: bool = False, **params: Any) -> Metric[Decimal] | None:
    if not whole:
        return None
    return Metric[Decimal](value=Decimal(part) / Decimal(whole), unit="ratio",
                           source_system=source, as_of=as_of,
                           query_ref=query_ref(name, 1, **params), is_modelled=modelled)


def _days(since: date, until: date) -> list[date]:
    return [since + timedelta(days=i) for i in range((until - since).days + 1)]


def _week(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def _tenant(conn: Any, tenant_id: UUID | None) -> TenantView | None:
    if tenant_id is None:
        return None
    name = await conn.scalar(text("SELECT canonical_name FROM cloudops.tenant WHERE id = :t"),
                             {"t": tenant_id})
    return None if name is None else TenantView(id=tenant_id, name=str(name))


class Window(ApiModel):
    since: date  # UTC, inclusive
    until: date  # UTC, today
    tenant: TenantView | None  # None: every tenant in the caller's scope


# --- usage ---------------------------------------------------------------------------


class RoleUsers(ApiModel):
    role: str  # the role's name in the matrix; "unknown" when no longer held
    users: Metric[int]


class UsageDay(ApiModel):
    day: date
    active_users: Metric[int]
    sessions: Metric[int]  # modelled: console page loads
    requests: Metric[int]
    by_role: list[RoleUsers]


class UsageWeek(ApiModel):
    week_start: date  # Monday
    partial: bool  # the window starts or ends inside this week
    active_users: Metric[int]
    by_role: list[RoleUsers]


class EndpointUse(ApiModel):
    action: str  # "GET /v1/...": a route, never its parameters
    requests: Metric[int]
    users: Metric[int]


class UsageView(ApiModel):
    window: Window
    active_users: Metric[int]
    staff_users: Metric[int]
    customer_users: Metric[int]
    sessions: Metric[int]
    requests: Metric[int]
    blocked: Metric[int]  # refused by the matrix
    by_role: list[RoleUsers]
    daily: list[UsageDay]
    weekly: list[UsageWeek]
    top_endpoints: list[EndpointUse]


# One row per request: a request in a scope of n tenants is logged n times, at one time.
_REQUESTS: Final = (
    "WITH r AS (SELECT DISTINCT actor, action, outcome, created_at FROM audit.action_log"
    " WHERE created_at >= :since AND actor LIKE 'user:%' AND NOT actor_is_agent)"
)
_USAGE_DAILY: Final = text(
    _REQUESTS + " SELECT (created_at AT TIME ZONE 'UTC')::date, actor, count(*),"  # noqa: S608
    "   count(*) FILTER (WHERE action = :session),"
    "   count(*) FILTER (WHERE outcome = 'blocked')"
    " FROM r GROUP BY 1, 2"
)
_USAGE_ENDPOINTS: Final = text(
    _REQUESTS + " SELECT action, count(*), count(DISTINCT actor) FROM r"  # noqa: S608
    " GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT :top"
)
_ROLES: Final = text("SELECT oid, kind, role_name FROM access.observed_roles(:actor, :oids)")


def _oid(actor: str) -> UUID | None:
    try:
        return UUID(actor.removeprefix("user:"))
    except ValueError:
        return None


def _by_role(actors: set[str], role_of: dict[str, str], as_of: datetime, name: str,
             **params: Any) -> list[RoleUsers]:
    counted: dict[str, int] = defaultdict(int)
    for a in actors:
        counted[role_of.get(a, "unknown")] += 1
    return [RoleUsers(role=role, users=_count(n, "users", AUDIT_SOURCE, as_of, name,
                                              role=role, **params))
            for role, n in sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))]


@router.get("/usage", response_model=UsageView)
@requires(OBSERVE)
async def usage(request: Request, access: AccessDep, scope: ScopeDep,
                days: DaysParam = DEFAULT_DAYS) -> UsageView:
    _staff(access)
    tenant_id = _chosen(access, scope)
    now = _now()
    since = now.date() - timedelta(days=days - 1)
    start = datetime.combine(since, time.min, UTC)
    async with platform(request).db.transaction(scope) as conn:
        tenant = await _tenant(conn, tenant_id)
        daily = _rows(await conn.execute(_USAGE_DAILY, {"since": start,
                                                         "session": SESSION_ACTION}))
        endpoints = _rows(await conn.execute(_USAGE_ENDPOINTS, {"since": start,
                                                                "top": TOP_ENDPOINTS}))
        oids = sorted({o for r in daily if (o := _oid(str(r[1]))) is not None}, key=str)
        try:
            roles = _rows(await conn.execute(_ROLES, {"actor": access.identity.oid,
                                                      "oids": oids}))
        except DBAPIError as exc:
            if str(getattr(exc.orig, "sqlstate", "")) == "42501":
                raise ApiError(403, "forbidden") from None
            raise
    role_of = {f"user:{r[0]}": str(r[2]) for r in roles}
    kind_of = {f"user:{r[0]}": str(r[1]) for r in roles}
    p: dict[str, Any] = {"tenant": str(tenant_id), "days": days}

    per_day: dict[date, dict[str, tuple[int, int, int]]] = defaultdict(dict)
    for day, actor, requests, sessions, blocked in daily:
        per_day[day][str(actor)] = (int(requests), int(sessions), int(blocked))
    everyone = {a for d in per_day.values() for a in d}

    def total(i: int) -> int:
        return sum(v[i] for d in per_day.values() for v in d.values())

    def c(value: int, unit: str, name: str, *, modelled: bool = False,
          **extra: Any) -> Metric[int]:
        return _count(value, unit, AUDIT_SOURCE, now, f"observability.{name}",
                      modelled=modelled, **p, **extra)

    days_out = [
        UsageDay(
            day=d,
            active_users=c(len(per_day.get(d, {})), "users", "active_users_day", day=d),
            sessions=c(sum(v[1] for v in per_day.get(d, {}).values()), "sessions",
                       "sessions_day", modelled=True, day=d),
            requests=c(sum(v[0] for v in per_day.get(d, {}).values()), "requests",
                       "requests_day", day=d),
            by_role=_by_role(set(per_day.get(d, {})), role_of, now,
                             "observability.role_users_day", day=d, **p))
        for d in _days(since, now.date())
    ]
    weeks: dict[date, set[str]] = defaultdict(set)
    for d in _days(since, now.date()):
        weeks[_week(d)] |= set(per_day.get(d, {}))
    weekly = [
        UsageWeek(
            week_start=w, partial=w < since or w + timedelta(days=6) > now.date(),
            active_users=c(len(actors), "users", "active_users_week", week=w),
            by_role=_by_role(actors, role_of, now, "observability.role_users_week",
                             week=w, **p))
        for w, actors in sorted(weeks.items())
    ]
    return UsageView(
        window=Window(since=since, until=now.date(), tenant=tenant),
        active_users=c(len(everyone), "users", "active_users"),
        staff_users=c(sum(1 for a in everyone if kind_of.get(a) == "staff"), "users",
                      "staff_users"),
        customer_users=c(sum(1 for a in everyone if kind_of.get(a) == "customer"), "users",
                         "customer_users"),
        sessions=c(total(1), "sessions", "sessions", modelled=True),
        requests=c(total(0), "requests", "requests"),
        blocked=c(total(2), "requests", "blocked"),
        by_role=_by_role(everyone, role_of, now, "observability.role_users", **p),
        daily=days_out,
        weekly=weekly,
        top_endpoints=[
            EndpointUse(action=str(action),
                        requests=c(n, "requests", "endpoint_requests", action=action),
                        users=c(u, "users", "endpoint_users", action=action))
            for action, n, u in endpoints
        ],
    )


# --- AI ------------------------------------------------------------------------------


class AiFigures(ApiModel):
    calls: Metric[int]
    refused: Metric[int]  # by a quota, the breaker or policy
    failed: Metric[int]
    tokens: Metric[int]
    spend: list[AiSpend] | None  # None: the caller does not hold cost.read


class AiDay(AiFigures):
    day: date


class AiAgent(AiFigures):
    agent: Agent


class AiCustomer(AiFigures):
    tenant: TenantView
    month_tokens: Metric[int]  # this calendar month (UTC), as the gateway counts it
    quota: Metric[int]
    quota_used: Metric[Decimal]
    assistant_month_tokens: Metric[int]
    assistant_budget: Metric[int]  # ADR 0029: a share of the tenant's quota
    assistant_budget_used: Metric[Decimal]


class AiView(ApiModel):
    window: Window
    month: str  # YYYY-MM
    spend_visible: bool
    totals: AiFigures
    alert_share: Metric[Decimal]  # the gateway warns from this share of a budget
    daily: list[AiDay]
    by_agent: list[AiAgent]
    by_customer: list[AiCustomer]


_AI: Final = text(
    f"SELECT tenant_id, (created_at AT TIME ZONE 'UTC')::date, {_AGENT}, cost_currency,"  # noqa: S608 - constants
    "   count(*), count(*) FILTER (WHERE outcome = 'refused'),"
    "   count(*) FILTER (WHERE outcome = 'failed'), sum(tokens_in + tokens_out), sum(cost),"
    "   max(created_at)"
    " FROM cloudops.llm_call WHERE created_at >= :since GROUP BY 1, 2, 3, 4"
)
# As the gateway counts a month (cloudops_gateway.gateway._USAGE): calendar month, UTC.
_AI_MONTH: Final = text(
    "SELECT tenant_id, sum(tokens_in + tokens_out),"
    "   coalesce(sum(tokens_in + tokens_out) FILTER (WHERE task LIKE :assistant), 0)"
    " FROM cloudops.llm_call WHERE created_at >= :month GROUP BY 1"
)

# (calls, refused, failed, tokens) and spend per currency: (sum, last call)
_Totals = list[int]
_Spend = dict[str, tuple[Decimal, datetime]]


def _add(totals: _Totals, spend: _Spend, row: tuple[Any, ...]) -> None:
    ccy, calls, refused, failed, tokens, cost, last = row
    for i, v in enumerate((calls, refused, failed, tokens)):
        totals[i] += int(v or 0)
    key = str(ccy).strip()
    prior = spend.get(key)
    amount = Decimal(cost or 0) + (prior[0] if prior else Decimal(0))
    spend[key] = (amount, max(last, prior[1]) if prior else last)


@router.get("/ai", response_model=AiView)
@requires(OBSERVE)
async def ai(request: Request, access: AccessDep, scope: ScopeDep,
             days: DaysParam = DEFAULT_DAYS) -> AiView:
    _staff(access)
    tenant_id = _chosen(access, scope)
    spend_visible = access.allows("cost.read")
    now = _now()
    since = now.date() - timedelta(days=days - 1)
    month = datetime(now.year, now.month, 1, tzinfo=UTC)
    async with platform(request).db.transaction(scope) as conn:
        tenant = await _tenant(conn, tenant_id)
        names = {UUID(str(r[0])): str(r[1]) for r in _rows(await conn.execute(text(
            "SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")))}
        rows = _rows(await conn.execute(_AI, {"since": datetime.combine(since, time.min, UTC)}))
        monthly = {UUID(str(r[0])): (int(r[1] or 0), int(r[2] or 0)) for r in _rows(
            await conn.execute(_AI_MONTH, {"month": month,
                                           "assistant": f"{ASSISTANT_PREFIX}%"}))}

    def empty() -> tuple[_Totals, _Spend]:
        return [0, 0, 0, 0], {}

    totals = empty()
    by_day: dict[date, tuple[_Totals, _Spend]] = defaultdict(empty)
    by_agent: dict[str, tuple[_Totals, _Spend]] = defaultdict(empty)
    by_tenant: dict[UUID, tuple[_Totals, _Spend]] = defaultdict(empty)
    for tid, day, agent, *values in rows:
        for t, s in (totals, by_day[day], by_agent[str(agent)], by_tenant[UUID(str(tid))]):
            _add(t, s, tuple(values))
    p: dict[str, Any] = {"tenant": str(tenant_id), "days": days}

    def figures(key: str, found: tuple[_Totals, _Spend], **extra: Any) -> dict[str, Any]:
        (calls, refused, failed, tokens), spend = found

        def c(value: int, unit: str, name: str) -> Metric[int]:
            return _count(value, unit, GATEWAY_SOURCE, now, f"observability.ai_{name}_{key}",
                          **p, **extra)

        return {
            "calls": c(calls, "calls", "calls"),
            "refused": c(refused, "calls", "refused"),
            "failed": c(failed, "calls", "failed"),
            "tokens": c(tokens, "tokens", "tokens"),
            "spend": None if not spend_visible else [
                AiSpend(currency=ccy, spend=Metric[Decimal](
                    value=amount, unit=ccy, source_system=GATEWAY_SOURCE, as_of=last,
                    query_ref=query_ref(f"observability.ai_spend_{key}", 1, **p, **extra),
                    is_modelled=False))
                for ccy, (amount, last) in sorted(spend.items())],
        }

    quota = home.TENANT_MONTHLY_TOKENS
    budget = int(Decimal(quota) * ASSISTANT_MONTHLY_SHARE)
    customers: list[AiCustomer] = []
    for tid, name in names.items():
        used, assistant = monthly.get(tid, (0, 0))
        who: dict[str, Any] = {"customer": str(tid)}
        customers.append(AiCustomer(
            tenant=TenantView(id=tid, name=name),
            **figures("customer", by_tenant.get(tid, empty()), **who),
            month_tokens=_count(used, "tokens", GATEWAY_SOURCE, now,
                                "observability.ai_month_tokens", **who),
            quota=_count(quota, "tokens", POLICY_SOURCE, now, "observability.ai_quota"),
            quota_used=_ratio(used, quota, GATEWAY_SOURCE, now, "observability.ai_quota_used",
                              **who) or _zero(now),
            assistant_month_tokens=_count(assistant, "tokens", GATEWAY_SOURCE, now,
                                          "observability.ai_assistant_tokens", **who),
            assistant_budget=_count(budget, "tokens", POLICY_SOURCE, now,
                                    "observability.ai_assistant_budget"),
            assistant_budget_used=_ratio(assistant, budget, GATEWAY_SOURCE, now,
                                         "observability.ai_assistant_used", **who) or _zero(now)))
    # Busiest first; a customer with no calls is still listed (zero is a finding).
    customers.sort(key=lambda c: (-c.month_tokens.value, -c.tokens.value, c.tenant.name))
    return AiView(
        window=Window(since=since, until=now.date(), tenant=tenant),
        month=f"{now:%Y-%m}",
        spend_visible=spend_visible,
        totals=AiFigures(**figures("total", totals)),
        alert_share=Metric[Decimal](value=home.ALERT_SHARE, unit="ratio",
                                    source_system=POLICY_SOURCE, as_of=now,
                                    query_ref=query_ref("observability.ai_alert_share", 1),
                                    is_modelled=False),
        daily=[AiDay(day=d, **figures("day", by_day.get(d, empty()), day=d))
               for d in _days(since, now.date())],
        by_agent=[AiAgent(agent=a, **figures("agent", by_agent.get(a, empty()), agent=a))
                  for a in AGENTS],
        by_customer=customers,
    )


def _zero(now: datetime) -> Metric[Decimal]:
    return Metric[Decimal](value=Decimal(0), unit="ratio", source_system=POLICY_SOURCE,
                           as_of=now, query_ref=query_ref("observability.zero", 1),
                           is_modelled=False)


# --- triage --------------------------------------------------------------------------


class TriageFigures(ApiModel):
    triaged: Metric[int]  # triage runs that finished
    ready_for_planning: Metric[int]
    human: Metric[int]
    accepted: Metric[int]  # reviews recorded in the window
    overridden: Metric[int]
    override_rate: Metric[Decimal] | None  # overridden over reviewed; None: no review


class TriageDay(TriageFigures):
    day: date


class TriageCustomer(TriageFigures):
    tenant: TenantView


class TriageThroughput(ApiModel):
    window: Window
    totals: TriageFigures
    daily: list[TriageDay]
    by_customer: list[TriageCustomer]


_TRIAGED: Final = text(
    "SELECT tenant_id, (created_at AT TIME ZONE 'UTC')::date,"
    "   count(*) FILTER (WHERE route = 'ready_for_planning'),"
    "   count(*) FILTER (WHERE route = 'human')"
    " FROM cloudops.triage_result WHERE created_at >= :since GROUP BY 1, 2"
)
_REVIEWED: Final = text(
    "SELECT tenant_id, (created_at AT TIME ZONE 'UTC')::date,"
    "   count(*) FILTER (WHERE decision = 'accept'),"
    "   count(*) FILTER (WHERE decision = 'override')"
    " FROM cloudops.triage_review WHERE created_at >= :since GROUP BY 1, 2"
)


@router.get("/triage", response_model=TriageThroughput)
@requires(OBSERVE)
async def triage(request: Request, access: AccessDep, scope: ScopeDep,
                 days: DaysParam = DEFAULT_DAYS) -> TriageThroughput:
    _staff(access)
    tenant_id = _chosen(access, scope)
    now = _now()
    since = now.date() - timedelta(days=days - 1)
    params = {"since": datetime.combine(since, time.min, UTC)}
    async with platform(request).db.transaction(scope) as conn:
        tenant = await _tenant(conn, tenant_id)
        names = {UUID(str(r[0])): str(r[1]) for r in _rows(await conn.execute(text(
            "SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")))}
        triaged = _rows(await conn.execute(_TRIAGED, params))
        reviewed = _rows(await conn.execute(_REVIEWED, params))

    # [ready, human, accepted, overridden]
    totals = [0, 0, 0, 0]
    by_day: dict[date, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    by_tenant: dict[UUID, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for offset, rows in ((0, triaged), (2, reviewed)):
        for tid, day, first, second in rows:
            for bucket in (totals, by_day[day], by_tenant[UUID(str(tid))]):
                bucket[offset] += int(first)
                bucket[offset + 1] += int(second)
    p: dict[str, Any] = {"tenant": str(tenant_id), "days": days}

    def figures(f: list[int], key: str, **extra: Any) -> dict[str, Any]:
        ready, human, accepted, overridden = f

        def c(value: int, unit: str, source: str, name: str) -> Metric[int]:
            return _count(value, unit, source, now, f"observability.triage_{name}_{key}",
                          **p, **extra)

        return {
            "triaged": c(ready + human, "tickets", TRIAGE_SOURCE, "triaged"),
            "ready_for_planning": c(ready, "tickets", TRIAGE_SOURCE, "ready"),
            "human": c(human, "tickets", TRIAGE_SOURCE, "human"),
            "accepted": c(accepted, "reviews", REVIEW_SOURCE, "accepted"),
            "overridden": c(overridden, "reviews", REVIEW_SOURCE, "overridden"),
            "override_rate": _ratio(overridden, accepted + overridden, REVIEW_SOURCE, now,
                                    f"observability.triage_override_rate_{key}", **p, **extra),
        }

    return TriageThroughput(
        window=Window(since=since, until=now.date(), tenant=tenant),
        totals=TriageFigures(**figures(totals, "total")),
        daily=[TriageDay(day=d, **figures(by_day.get(d, [0, 0, 0, 0]), "day", day=d))
               for d in _days(since, now.date())],
        by_customer=sorted(
            (TriageCustomer(tenant=TenantView(id=tid, name=name),
                            **figures(by_tenant.get(tid, [0, 0, 0, 0]), "customer",
                                      customer=str(tid)))
             for tid, name in names.items()),
            key=lambda c: (-c.triaged.value, c.tenant.name)),
    )


# --- connectors ----------------------------------------------------------------------


class SourceHealth(ApiModel):
    source_system: str
    band: connectors.Band  # the worst of its entities' bands (cloudops_api.connectors)
    entities: Metric[int]
    last_success_at: datetime | None  # the latest of its entities'
    runs_24h: Metric[int]
    failed_runs_24h: Metric[int]
    failed_run_rate_24h: Metric[Decimal] | None  # failed over finished; None: no run
    quarantine_depth: Metric[int]  # names and resources held for this source
    drift_open: Metric[int]


class ConnectorSummary(ApiModel):
    as_of: datetime
    visible: bool  # False: the caller does not hold connectors.read
    sources: list[SourceHealth]


@router.get("/connectors", response_model=ConnectorSummary)
@requires(OBSERVE)
async def connector_summary(request: Request, access: AccessDep) -> ConnectorSummary:
    """Per source, not per tenant: the run log is the platform's own."""
    _staff(access)
    now = _now()
    if not access.allows(connectors.READ):
        return ConnectorSummary(as_of=now, visible=False, sources=[])
    async with platform(request).db.transaction(access.scope) as conn:
        try:
            rows = _rows(await conn.execute(connectors.HEALTH_QUERY,
                                            {"actor": access.identity.oid}))
        except DBAPIError as exc:
            if str(getattr(exc.orig, "sqlstate", "")) == "42501":
                raise ApiError(403, "forbidden") from None
            raise
        now = await conn.scalar(text("SELECT now()"))
    grouped: dict[str, list[connectors.ConnectorHealth]] = defaultdict(list)
    for row in rows:
        h = connectors.health_row(row)
        grouped[h.source_system].append(h)

    def m(value: int, unit: str, name: str, source: str) -> Metric[int]:
        return _count(value, unit, connectors.RUN_LOG, now, f"observability.connector_{name}",
                      connector=source)

    sources: list[SourceHealth] = []
    for source, items in grouped.items():
        runs = sum(i.runs_24h.value for i in items)
        failed = sum(i.failed_runs_24h.value for i in items)
        successes = [i.last_success_at for i in items if i.last_success_at is not None]
        rate = connectors.rate(failed, runs)
        sources.append(SourceHealth(
            source_system=source,
            band=max((i.band for i in items), key=lambda b: connectors.RANK[b]),
            entities=m(len(items), "entities", "entities", source),
            last_success_at=max(successes) if successes else None,
            runs_24h=m(runs, "runs", "runs_24h", source),
            failed_runs_24h=m(failed, "runs", "failed_runs_24h", source),
            failed_run_rate_24h=None if rate is None else Metric[Decimal](
                value=rate, unit="ratio", source_system=connectors.RUN_LOG, as_of=now,
                query_ref=query_ref("observability.connector_failed_rate", 1, source=source),
                is_modelled=False),
            # Alias quarantine is per source (every entity carries the same depth);
            # resource quarantine likewise, under the caller's scope.
            quarantine_depth=m(items[0].alias_quarantine_depth.value
                               + items[0].resource_quarantine_depth.value,
                               "records", "quarantine_depth", source),
            drift_open=m(sum(i.drift_open.value for i in items), "fields", "drift_open",
                         source),
        ))
    sources.sort(key=lambda s: (-connectors.RANK[s.band], s.source_system))
    return ConnectorSummary(as_of=now, visible=True, sources=sources)


# --- platform cost -------------------------------------------------------------------


class PlatformCost(ApiModel):
    visible: bool  # False: the caller does not hold cost.read
    months: list[str]
    host: TenantView | None  # None: no tenant in scope hosts the platform
    captured: bool  # this month's host cost has resource-level lines to tell it apart
    currencies: list[CurrencyTrend]


@router.get("/platform-cost", response_model=PlatformCost)
@requires(OBSERVE)
async def platform_cost(request: Request, access: AccessDep,
                        scope: ScopeDep) -> PlatformCost:
    _staff(access)
    now = _now()
    periods = home.month_periods(now.date(), home.TREND_MONTHS)
    if not access.allows("cost.read"):
        return PlatformCost(visible=False, months=periods, host=None, captured=False,
                            currencies=[])
    async with platform(request).db.transaction(scope) as conn:
        hosts = _rows(await conn.execute(text(
            "SELECT id, canonical_name FROM cloudops.tenant WHERE hosts_platform"
            " ORDER BY canonical_name LIMIT 1")))
        if not hosts:
            return PlatformCost(visible=True, months=periods, host=None, captured=False,
                                currencies=[])
        host = TenantView(id=hosts[0][0], name=str(hosts[0][1]))
        rows = _rows(await conn.execute(home.PLATFORM_TREND, {
            "tenant": host.id, "periods": periods, "group": PLATFORM_GROUP}))
        captured = bool(await conn.scalar(home.RESOURCE_LINES,
                                          {"tenant": host.id, "period": periods[-1]}))
    own: dict[str, dict[str, tuple[Any, date]]] = defaultdict(dict)
    for period, ccy, total, last in rows:
        own[str(ccy).strip()][str(period)] = (total, last)

    def point(ccy: str, period: str) -> CostPoint:
        found = own[ccy].get(period)
        if found is None:
            return CostPoint(period=period, month_to_date=period == periods[-1], amortized=None)
        as_of = datetime.combine(found[1] + timedelta(days=1), time.min, UTC)
        return CostPoint(period=period, month_to_date=period == periods[-1],
                         amortized=Metric[Decimal](
                             value=Decimal(found[0]), unit=ccy, source_system=COST_SOURCE,
                             as_of=as_of, is_modelled=False,
                             query_ref=query_ref("observability.platform_cost", 1,
                                                 period=period)))

    return PlatformCost(
        visible=True, months=periods, host=host, captured=captured,
        currencies=[CurrencyTrend(currency=ccy, points=[point(ccy, p) for p in periods])
                    for ccy in sorted(own)])
