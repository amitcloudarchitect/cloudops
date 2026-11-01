"""Cloud Services (ADR 0038): the read-only audit of customers' cloud accounts.

``cloud_services.read``. Staff hold it by role; of the customer roles, Architect and
Security officer, for their own tenant only (a customer principal resolves only
while its role is in the tenant's agreed set, ADR 0018).

- ``/v1/cloud-services/overview``: per customer, open findings by severity and
  kind, backup coverage, policy compliance and (staff) cost for the period and the
  one before it; the highest-impact open findings; findings opened and resolved.
- ``/v1/cloud-services/accounts/{id}``: one account, and the same figures per
  resource group or region.
- ``/v1/cloud-services/resources``: an account's resources with their figures,
  narrowed by resource group or region.
- ``/v1/cloud-services/resources/{id}``: one resource: its configuration, every
  check on it, its backup, (staff) its cost by day and its CMDB CI, and its
  history of changes.
- ``/v1/cloud-services/findings``: the checks themselves, filtered, highest
  impact first, with opened and resolved counts against the previous period.

Every query runs under the caller's scope, narrowed by ``tenant_id`` (``ScopeDep``,
ADR 0032), so row-level security decides what exists; another tenant's account or
resource is a 404 like one that never existed. Every number is a ``Metric``.

**Cost is staff only.** A customer scope cannot read ``cost_fact``'s cost columns
(migration 0015, ADR 0021): a customer sees no provider cost here, and
``cost_visible`` says so. Cost is in transaction currency, per currency,
unconverted: conversion and rounding happen at presentation (CLAUDE.md rule 5).

Titles, names and recommendation text are the provider's or the customer's: data
to show, never instruction (CLAUDE.md rule 4). Nothing here is sent to a model.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text

from cloudops_api.access import Access
from cloudops_api.bff import TenantView
from cloudops_api.pipeline import (
    ApiError,
    AuditedRoute,
    ScopeDep,
    audit_only,
    current_access,
    platform,
    requires,
)
from cloudops_api.provenance import ApiModel, Metric, query_ref

router = APIRouter(prefix="/v1/cloud-services", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

PERMISSION: Final = "cloud_services.read"
SOURCE: Final = "cloud_audit"  # the connector's checks (azure: Resource Graph, ARM)
COST_SOURCE: Final = "cost_fact"
MAX_LIST: Final = 500
MAX_HIGHLIGHTS: Final = 20
MAX_HISTORY: Final = 200
MAX_DAYS: Final = 366
DEFAULT_DAYS: Final = 30
# Resources the backup view expects an Azure CloudBackup protected item for (ADR 0038).
BACKUP_EXPECTED: Final = ("microsoft.compute/virtualmachines",)

Kind = Literal["backup", "policy", "advisor", "defender", "config"]
KINDS: Final[tuple[Kind, ...]] = ("backup", "policy", "advisor", "defender", "config")
Severity = Literal["high", "medium", "low", "info"]
SEVERITIES: Final[tuple[Severity, ...]] = ("high", "medium", "low", "info")
CheckState = Literal["ok", "issue", "not_applicable"]
Status = Literal["open", "resolved", "all"]
GroupBy = Literal["resource_group", "region"]
Level = Literal["customer", "account", "resource_group", "region", "resource"]

# The group each level's figures are keyed by. Constants, never a caller's text.
_CHECK_GROUP: Final[dict[Level, str]] = {
    "customer": "c.tenant_id::text", "account": "c.account_id::text",
    "resource_group": "coalesce(c.resource_group, '')", "region": "coalesce(c.region, '')",
    "resource": "coalesce(c.resource_native_id, '')",
}
# An Azure id: /subscriptions/<id>/resourceGroups/<name>/...
_RG: Final = "lower(split_part(r.native_id, '/', 5))"
_RESOURCE_GROUP: Final[dict[Level, str]] = {
    "customer": "r.tenant_id::text", "account": "r.account_id::text",
    "resource_group": f"coalesce({_RG}, '')", "region": "coalesce(r.region, '')",
    "resource": "lower(r.native_id)",
}
_COST_GROUP: Final[dict[Level, str]] = {
    "customer": "c.tenant_id::text", "account": "c.account_id::text",
    "resource_group": f"coalesce({_RG}, '')", "region": "coalesce(r.region, '')",
    "resource": "coalesce(lower(r.native_id), '')",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _count(value: Any, unit: str, as_of: datetime, name: str, *, source: str = SOURCE,
           **params: Any) -> Metric[int]:
    return Metric[int](value=int(value or 0), unit=unit, source_system=source, as_of=as_of,
                       query_ref=query_ref(name, 1, **params), is_modelled=False)


def _end(day: date) -> datetime:
    return datetime.combine(day, time.max, tzinfo=UTC)


def _start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


# --- views -----------------------------------------------------------------------------


class CloudPeriod(ApiModel):
    start: date
    end: date
    previous_start: date  # the same length, immediately before
    previous_end: date


class CloudKindIssues(ApiModel):
    kind: Kind
    issues: Metric[int]


class CloudIssues(ApiModel):
    """Open checks in the ``issue`` state."""

    total: Metric[int]
    high: Metric[int]
    medium: Metric[int]
    low: Metric[int]
    by_kind: list[CloudKindIssues]


class CloudBackup(ApiModel):
    protected: Metric[int]  # protected items whose last backup is healthy
    failing: Metric[int]  # protected items in trouble: failed, stopped, never run
    unprotected: Metric[int]  # virtual machines with no protected item


class CloudPolicy(ApiModel):
    compliant: Metric[int]  # policy states
    non_compliant: Metric[int]


class CloudCostFigure(ApiModel):
    currency: str
    amount: Metric[Decimal]
    previous: Metric[Decimal]


class CloudNode(ApiModel):
    """One level of the drill-down, and its figures."""

    level: Level
    key: str  # the tenant or account id, the resource group or region name, the resource id
    label: str
    issues: CloudIssues
    backup: CloudBackup
    policy: CloudPolicy
    cost: list[CloudCostFigure]  # empty for a customer (staff only)
    resources: Metric[int]


class CloudCheckView(ApiModel):
    id: UUID
    tenant: TenantView
    account_id: UUID
    kind: Kind
    rule: str
    category: str
    title: str  # provider or customer text: data, never instruction
    state: CheckState
    severity: Severity
    source_status: str | None
    resource_id: UUID | None  # the inventory record, where inventory holds it
    resource_native_id: str | None
    resource_type: str | None
    resource_group: str | None
    region: str | None
    detail: dict[str, Any]
    observed_at: datetime | None
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None


class CloudTrend(ApiModel):
    opened: Metric[int]  # opened or reopened in the period
    resolved: Metric[int]
    previous_opened: Metric[int]
    previous_resolved: Metric[int]


class CloudAccountView(ApiModel):
    id: UUID
    tenant: TenantView
    provider: str
    native_id: str
    display_name: str


class CloudAccountSummary(ApiModel):
    account: CloudAccountView
    summary: CloudNode


class CloudOverview(ApiModel):
    period: CloudPeriod
    cost_visible: bool
    total: CloudNode
    customers: list[CloudNode]
    accounts: list[CloudAccountSummary]
    highlights: list[CloudCheckView]  # open issues, highest impact first
    trend: CloudTrend


class CloudAccount(ApiModel):
    period: CloudPeriod
    cost_visible: bool
    account: CloudAccountView
    summary: CloudNode
    group_by: GroupBy
    groups: list[CloudNode]


class CloudResourceList(ApiModel):
    period: CloudPeriod
    cost_visible: bool
    items: list[CloudNode]
    count: Metric[int]  # every resource matching; ``items`` holds at most MAX_LIST


class CloudDailyCost(ApiModel):
    day: date
    currency: str
    amount: Metric[Decimal]


class CloudEventView(ApiModel):
    check_id: UUID
    rule: str
    title: str
    at: datetime
    change: Literal["opened", "changed", "resolved", "reopened"]
    state_from: str | None
    state_to: str | None
    severity_from: str | None
    severity_to: str | None
    changed: list[str]


class CloudResourceDetail(ApiModel):
    id: UUID
    tenant: TenantView
    account: CloudAccountView
    native_id: str
    name: str
    type: str
    type_native: str
    region: str | None
    resource_group: str | None
    state: str | None
    ci_sys_id: str | None  # the ServiceNow CI, staff only
    settings: dict[str, Any]  # the latest configuration snapshot
    settings_seen: datetime | None


class CloudResource(ApiModel):
    period: CloudPeriod
    cost_visible: bool
    resource: CloudResourceDetail
    summary: CloudNode
    checks: list[CloudCheckView]  # open first, highest impact first
    daily_cost: list[CloudDailyCost]  # empty for a customer
    history: list[CloudEventView]  # newest first, at most MAX_HISTORY


class CloudFindings(ApiModel):
    period: CloudPeriod
    items: list[CloudCheckView]
    count: Metric[int]  # every check matching; ``items`` holds at most MAX_LIST
    trend: CloudTrend


# --- queries ---------------------------------------------------------------------------

_ISSUES: Final = (
    "SELECT {g}, c.kind, c.severity, count(*) FROM cloudops.cloud_check c"
    " WHERE c.resolved_at IS NULL AND c.state = 'issue' {where} GROUP BY 1, 2, 3"
)
_BACKUP: Final = (
    "SELECT {g}, count(*) FILTER (WHERE c.state = 'ok'),"
    " count(*) FILTER (WHERE c.state = 'issue') FROM cloudops.cloud_check c"
    " WHERE c.resolved_at IS NULL AND c.kind = 'backup' {where} GROUP BY 1"
)
_POLICY: Final = (
    "SELECT {g}, count(*) FILTER (WHERE c.state = 'ok'),"
    " count(*) FILTER (WHERE c.state = 'issue') FROM cloudops.cloud_check c"
    " WHERE c.resolved_at IS NULL AND c.kind = 'policy' {where} GROUP BY 1"
)
# A VM with no open protected item, in an account the audit has read.
_UNPROTECTED: Final = (
    "SELECT {g}, count(*) FROM cloudops.resource r"
    " WHERE r.deleted_at IS NULL AND lower(r.type_native) = ANY(CAST(:expected AS text[]))"
    "   AND EXISTS (SELECT 1 FROM cloudops.cloud_check a WHERE a.account_id = r.account_id)"
    "   AND NOT EXISTS (SELECT 1 FROM cloudops.cloud_check b WHERE b.kind = 'backup'"
    "     AND b.resolved_at IS NULL AND b.account_id = r.account_id"
    "     AND b.resource_native_id = lower(r.native_id)) {where} GROUP BY 1"
)
_RESOURCES: Final = (
    "SELECT {g}, count(*) FROM cloudops.resource r"
    " WHERE r.deleted_at IS NULL {where} GROUP BY 1"
)
_COST: Final = (
    "SELECT {g}, c.currency,"
    " coalesce(sum(c.amortized_cost) FILTER (WHERE c.usage_date BETWEEN :start AND :end), 0),"
    " coalesce(sum(c.amortized_cost) FILTER (WHERE c.usage_date BETWEEN :pstart AND :pend), 0)"
    " FROM cloudops.cost_fact c LEFT JOIN cloudops.resource r ON r.id = c.resource_id"
    " WHERE c.usage_date BETWEEN :pstart AND :end {where} GROUP BY 1, 2 ORDER BY 2"
)
_CHECK_COLUMNS: Final = (
    "c.id, t.id, t.canonical_name, c.account_id, c.kind, c.rule, c.category, c.title, c.state,"
    " c.severity, c.source_status, c.resource_id, c.resource_native_id, c.resource_type,"
    " c.resource_group, c.region, c.detail, c.observed_at, c.first_seen, c.last_seen,"
    " c.resolved_at"
)
_RANK: Final = (
    "CASE c.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END"
)
_FILTERS: Final = (
    " AND (CAST(:account AS uuid) IS NULL OR c.account_id = CAST(:account AS uuid))"
    " AND (CAST(:kind AS text) IS NULL OR c.kind = CAST(:kind AS text))"
    " AND (CAST(:severity AS text) IS NULL OR c.severity = CAST(:severity AS text))"
    " AND (CAST(:category AS text) IS NULL OR c.category = CAST(:category AS text))"
    " AND (CAST(:group AS text) IS NULL OR c.resource_group = CAST(:group AS text))"
    " AND (CAST(:region AS text) IS NULL OR c.region = CAST(:region AS text))"
    " AND (CAST(:resource AS uuid) IS NULL OR c.resource_id = CAST(:resource AS uuid))"
    " AND (CAST(:status AS text) = 'all'"
    "      OR (CAST(:status AS text) = 'open' AND c.resolved_at IS NULL AND c.state = 'issue')"
    "      OR (CAST(:status AS text) = 'resolved' AND c.resolved_at IS NOT NULL))"
)
_FINDINGS: Final = text(
    f"SELECT {_CHECK_COLUMNS} FROM cloudops.cloud_check c"  # noqa: S608 - constants
    f" JOIN cloudops.tenant t ON t.id = c.tenant_id WHERE true {_FILTERS}"
    f" ORDER BY (c.resolved_at IS NOT NULL), {_RANK}, c.last_seen DESC, c.id LIMIT :limit"
)
_FINDINGS_COUNT: Final = text(
    f"SELECT count(*) FROM cloudops.cloud_check c WHERE true {_FILTERS}"  # noqa: S608 - constants
)
_TREND: Final = text(
    "SELECT count(*) FILTER (WHERE e.change IN ('opened', 'reopened')"
    "     AND e.at >= :s AND e.at < :e),"
    "   count(*) FILTER (WHERE e.change = 'resolved' AND e.at >= :s AND e.at < :e),"
    "   count(*) FILTER (WHERE e.change IN ('opened', 'reopened')"
    "     AND e.at >= :ps AND e.at < :pe),"
    "   count(*) FILTER (WHERE e.change = 'resolved' AND e.at >= :ps AND e.at < :pe)"
    " FROM cloudops.cloud_check_event e JOIN cloudops.cloud_check c ON c.id = e.check_id"
    " WHERE e.at >= :ps AND e.at < :e"
    "   AND (CAST(:account AS uuid) IS NULL OR e.account_id = CAST(:account AS uuid))"
    "   AND (CAST(:kind AS text) IS NULL OR c.kind = CAST(:kind AS text))"
)
_TENANTS: Final = text(
    "SELECT t.id, t.canonical_name FROM cloudops.tenant t"
    " WHERE EXISTS (SELECT 1 FROM cloudops.cloud_account a WHERE a.tenant_id = t.id)"
    " ORDER BY t.canonical_name"
)
_ACCOUNTS: Final = text(
    "SELECT a.id, t.id, t.canonical_name, a.provider, a.native_id, a.display_name"
    " FROM cloudops.cloud_account a JOIN cloudops.tenant t ON t.id = a.tenant_id"
    " ORDER BY t.canonical_name, a.display_name, a.id"
)
_ACCOUNT: Final = text(
    "SELECT a.id, t.id, t.canonical_name, a.provider, a.native_id, a.display_name"
    " FROM cloudops.cloud_account a JOIN cloudops.tenant t ON t.id = a.tenant_id"
    " WHERE a.id = :id"
)
_GROUPS: Final[dict[GroupBy, str]] = {
    "resource_group": (
        "SELECT DISTINCT g FROM (SELECT coalesce(c.resource_group, '') AS g"  # noqa: S608 - constants
        " FROM cloudops.cloud_check c WHERE c.account_id = :account"
        f" UNION SELECT coalesce({_RG}, '') FROM cloudops.resource r"
        " WHERE r.account_id = :account AND r.deleted_at IS NULL) x ORDER BY 1"),
    "region": (
        "SELECT DISTINCT g FROM (SELECT coalesce(c.region, '') AS g"
        " FROM cloudops.cloud_check c WHERE c.account_id = :account"
        " UNION SELECT coalesce(r.region, '') FROM cloudops.resource r"
        " WHERE r.account_id = :account AND r.deleted_at IS NULL) x ORDER BY 1"),
}
_RESOURCE_LIST: Final = (
    "SELECT r.id, lower(r.native_id), r.name FROM cloudops.resource r"
    " WHERE r.deleted_at IS NULL AND r.account_id = :account {where}"
    " ORDER BY (SELECT count(*) FROM cloudops.cloud_check c WHERE c.account_id = r.account_id"
    "   AND c.resource_native_id = lower(r.native_id) AND c.resolved_at IS NULL"
    "   AND c.state = 'issue') DESC, r.name, r.id LIMIT :limit"
)
_RESOURCE_COUNT: Final = (
    "SELECT count(*) FROM cloudops.resource r"
    " WHERE r.deleted_at IS NULL AND r.account_id = :account {where}"
)
_RESOURCE: Final = text(
    "SELECT r.id, r.tenant_id, t.canonical_name, r.account_id, r.native_id, r.name,"  # noqa: S608 - constants
    f" r.type_canonical, r.type_native, r.region, {_RG}, r.state FROM cloudops.resource r"
    " JOIN cloudops.tenant t ON t.id = r.tenant_id WHERE r.id = :id"
)
_CI: Final = text("SELECT servicenow_ci_sys_id FROM cloudops.resource WHERE id = :id")
_SETTINGS: Final = text(
    "SELECT detail -> 'settings', last_seen FROM cloudops.cloud_check"
    " WHERE account_id = :account AND kind = 'config' AND rule = 'config.settings'"
    "   AND resource_native_id = :native AND resolved_at IS NULL"
)
_RESOURCE_CHECKS: Final = text(
    f"SELECT {_CHECK_COLUMNS} FROM cloudops.cloud_check c"  # noqa: S608 - constants
    " JOIN cloudops.tenant t ON t.id = c.tenant_id"
    " WHERE c.account_id = :account AND c.resource_native_id = :native"
    "   AND NOT (c.kind = 'config' AND c.rule = 'config.settings')"
    f" ORDER BY (c.resolved_at IS NOT NULL), (c.state <> 'issue'), {_RANK}, c.kind, c.title"
    " LIMIT :limit"
)
_HISTORY: Final = text(
    "SELECT e.check_id, c.rule, c.title, e.at, e.change, e.state_from, e.state_to,"
    " e.severity_from, e.severity_to, e.changed"
    " FROM cloudops.cloud_check_event e JOIN cloudops.cloud_check c ON c.id = e.check_id"
    " WHERE c.account_id = :account AND c.resource_native_id = :native"
    " ORDER BY e.at DESC, c.rule LIMIT :limit"
)
_DAILY_COST: Final = text(
    "SELECT usage_date, currency, sum(amortized_cost) FROM cloudops.cost_fact"
    " WHERE resource_id = :id AND usage_date BETWEEN :start AND :end"
    " GROUP BY 1, 2 ORDER BY 1, 2"
)


# --- building blocks -------------------------------------------------------------------


def _period(start: date | None, end: date | None) -> CloudPeriod:
    end = end or _now().date()
    start = start or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise ApiError(422, "period_reversed")
    days = (end - start).days + 1
    if days > MAX_DAYS:
        raise ApiError(422, "period_too_long")
    return CloudPeriod(start=start, end=end, previous_start=start - timedelta(days=days),
                  previous_end=start - timedelta(days=1))


def _can_cost(access: Access) -> bool:
    return access.kind == "staff" and access.allows("cost.read")


@router.get("/overview", response_model=CloudOverview)
@requires(PERMISSION)
async def overview(
    request: Request, access: AccessDep, scope: ScopeDep,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> CloudOverview:
    period = _period(start, end)
    now = _now()
    cost = _can_cost(access)
    async with platform(request).db.transaction(scope) as conn:
        tenants = _rows(await conn.execute(_TENANTS))
        accounts = [_account_view(r) for r in await conn.execute(_ACCOUNTS)]
        figures = await _figures(conn, "customer", {}, period, cost)
        by_account = await _figures(conn, "account", {}, period, cost)
        highlights = _rows(await conn.execute(_FINDINGS, _filters(
            status="open", limit=MAX_HIGHLIGHTS)))
        trend = await _trend(conn, period, None, None)
    customers = [_node("customer", str(tid), name, figures, now, period)
                 for tid, name in tenants]
    total = _total("customer", "all", "All customers", figures, now, period)
    return CloudOverview(period=period, cost_visible=cost, total=total, customers=customers,
                         accounts=[CloudAccountSummary(account=a, summary=_node(
                             "account", str(a.id), a.display_name, by_account, now, period))
                             for a in accounts],
                         highlights=[_check(r) for r in highlights],
                         trend=_trend_view(trend, now, period))


@router.get("/accounts/{account_id}", response_model=CloudAccount)
@requires(PERMISSION)
async def account(
    request: Request, access: AccessDep, account_id: UUID,
    group_by: GroupBy = "resource_group",
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> CloudAccount:
    period = _period(start, end)
    now = _now()
    cost = _can_cost(access)
    async with platform(request).db.transaction(access.scope) as conn:
        found = await _account(conn, request, account_id)
        where = {"account": account_id}
        groups = [str(r[0]) for r in await conn.execute(text(_GROUPS[group_by]), where)]
        summary_figures = await _figures(conn, "account", where, period, cost)
        figures = await _figures(conn, group_by, where, period, cost)
    return CloudAccount(
        period=period, cost_visible=cost, account=found,
        summary=_node("account", str(account_id), found.display_name, summary_figures, now,
                      period),
        group_by=group_by,
        groups=[_node(group_by, g, g or "(none)", figures, now, period) for g in groups])


@router.get("/resources", response_model=CloudResourceList)
@requires(PERMISSION)
async def resources(
    request: Request, access: AccessDep, account_id: UUID,
    resource_group: Annotated[str | None, Query(max_length=200)] = None,
    region: Annotated[str | None, Query(max_length=100)] = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> CloudResourceList:
    period = _period(start, end)
    now = _now()
    cost = _can_cost(access)
    where = ""
    params: dict[str, Any] = {"account": account_id, "limit": MAX_LIST}
    if resource_group is not None:
        where += f" AND {_RG} = :rg"
        params["rg"] = resource_group.lower()
    if region is not None:
        where += " AND r.region = :region"
        params["region"] = region
    async with platform(request).db.transaction(access.scope) as conn:
        await _account(conn, request, account_id)
        listed = _rows(await conn.execute(text(_RESOURCE_LIST.format(where=where)), params))
        total = (await conn.execute(text(_RESOURCE_COUNT.format(where=where)), params)).scalar()
        figures = await _figures(conn, "resource", {"account": account_id}, period, cost)
    items = [_node("resource", str(rid), name, figures, now, period, figure_key=native)
             for rid, native, name in listed]
    return CloudResourceList(period=period, cost_visible=cost, items=items,
                             count=_count(total, "resources", now, "cloud.resources",
                                          account=str(account_id)))


@router.get("/resources/{resource_id}", response_model=CloudResource)
@requires(PERMISSION)
async def resource(
    request: Request, access: AccessDep, resource_id: UUID,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> CloudResource:
    period = _period(start, end)
    now = _now()
    cost = _can_cost(access)
    async with platform(request).db.transaction(access.scope) as conn:
        found = (await conn.execute(_RESOURCE, {"id": resource_id})).one_or_none()
        if found is None:
            raise ApiError(404, "not_found")
        (rid, tid, tname, account_id, native, name, kind, kind_native, region, group,
         state) = tuple(found)
        audit_only(request, [tid])
        acct = await _account(conn, request, account_id)
        key = {"account": account_id, "native": str(native).lower()}
        ci = (await conn.execute(_CI, {"id": rid})).scalar() if access.kind == "staff" else None
        settings = (await conn.execute(_SETTINGS, key)).one_or_none()
        checks = _rows(await conn.execute(_RESOURCE_CHECKS, {**key, "limit": MAX_LIST}))
        history = _rows(await conn.execute(_HISTORY, {**key, "limit": MAX_HISTORY}))
        figures = await _figures(conn, "resource", {"account": account_id}, period, cost)
        daily = _rows(await conn.execute(_DAILY_COST, {
            "id": rid, "start": period.start, "end": period.end})) if cost else []
    detail = CloudResourceDetail(
        id=rid, tenant=TenantView(id=tid, name=tname), account=acct, native_id=native,
        name=name, type=kind, type_native=kind_native, region=region,
        resource_group=group or None, state=state, ci_sys_id=ci,
        settings=dict(settings[0] or {}) if settings is not None else {},
        settings_seen=None if settings is None else tuple(settings)[1])
    ref = query_ref("cloud.resource_daily_cost", 1, resource=str(rid), start=period.start,
                    end=period.end)
    return CloudResource(
        period=period, cost_visible=cost, resource=detail,
        summary=_node("resource", str(rid), name, figures, now, period,
                      figure_key=str(native).lower()),
        checks=[_check(r) for r in checks],
        daily_cost=[CloudDailyCost(day=d, currency=ccy, amount=Metric[Decimal](
            value=Decimal(v), unit=ccy, source_system=COST_SOURCE, as_of=_end(d),
            query_ref=ref, is_modelled=False)) for d, ccy, v in daily],
        history=[CloudEventView(check_id=h[0], rule=h[1], title=h[2], at=h[3], change=h[4],
                           state_from=h[5], state_to=h[6], severity_from=h[7],
                           severity_to=h[8], changed=list(h[9] or [])) for h in history])


@router.get("/findings", response_model=CloudFindings)
@requires(PERMISSION)
async def findings(
    request: Request, access: AccessDep, scope: ScopeDep,
    account_id: UUID | None = None,
    kind: Kind | None = None,
    severity: Severity | None = None,
    category: Annotated[str | None, Query(max_length=60)] = None,
    resource_group: Annotated[str | None, Query(max_length=200)] = None,
    region: Annotated[str | None, Query(max_length=100)] = None,
    status: Status = "open",
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> CloudFindings:
    period = _period(start, end)
    now = _now()
    params = _filters(account=account_id, kind=kind, severity=severity, category=category,
                      group=None if resource_group is None else resource_group.lower(),
                      region=region, status=status, limit=MAX_LIST)
    async with platform(request).db.transaction(scope) as conn:
        if account_id is not None:
            await _account(conn, request, account_id)
        rows = _rows(await conn.execute(_FINDINGS, params))
        total = (await conn.execute(_FINDINGS_COUNT, params)).scalar()
        trend = await _trend(conn, period, account_id, kind)
    return CloudFindings(
        period=period, items=[_check(r) for r in rows],
        count=_count(total, "checks", now, "cloud.findings", **{
            k: str(v) for k, v in params.items() if v is not None and k != "limit"}),
        trend=_trend_view(trend, now, period))


# --- helpers ---------------------------------------------------------------------------


def _filters(**given: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict.fromkeys(
        ("account", "kind", "severity", "category", "group", "region", "resource"))
    base["status"] = "open"
    return {**base, **given}


async def _account(conn: Any, request: Request, account_id: UUID) -> CloudAccountView:
    found = (await conn.execute(_ACCOUNT, {"id": account_id})).one_or_none()
    if found is None:
        # Another tenant's account and one that never existed look the same (RLS).
        raise ApiError(404, "not_found")
    view = _account_view(found)
    audit_only(request, [view.tenant.id])
    return view


def _account_view(row: Any) -> CloudAccountView:
    aid, tid, tname, provider, native, display = tuple(row)
    return CloudAccountView(id=aid, tenant=TenantView(id=tid, name=tname), provider=provider,
                       native_id=native, display_name=display or native)


Figures = dict[str, Any]


async def _figures(conn: Any, level: Level, where: dict[str, Any], period: CloudPeriod,
                   cost: bool) -> Figures:
    """Every figure of a level, keyed by its group: issues, backup, policy, unprotected
    VMs, resources and (staff) cost."""
    acct = "account" in where
    cw = " AND c.account_id = :account" if acct else ""
    rw = " AND r.account_id = :account" if acct else ""
    cg, rg, kg = _CHECK_GROUP[level], _RESOURCE_GROUP[level], _COST_GROUP[level]
    out: Figures = {
        "issues": _rows(await conn.execute(text(_ISSUES.format(g=cg, where=cw)), where)),
        "backup": _rows(await conn.execute(text(_BACKUP.format(g=cg, where=cw)), where)),
        "policy": _rows(await conn.execute(text(_POLICY.format(g=cg, where=cw)), where)),
        "unprotected": _rows(await conn.execute(text(_UNPROTECTED.format(g=rg, where=rw)),
                                                {**where, "expected": list(BACKUP_EXPECTED)})),
        "resources": _rows(await conn.execute(text(_RESOURCES.format(g=rg, where=rw)), where)),
        "cost": [],
        "level": level,
        "where": {k: str(v) for k, v in where.items()},
    }
    if cost:
        out["cost"] = _rows(await conn.execute(text(_COST.format(g=kg, where=cw)), {
            **where, "start": period.start, "end": period.end,
            "pstart": period.previous_start, "pend": period.previous_end}))
    return out


def _pick(rows: list[tuple[Any, ...]], key: str | None) -> list[tuple[Any, ...]]:
    return [r[1:] for r in rows if key is None or str(r[0]) == key]


def _issues(rows: list[tuple[Any, ...]], now: datetime, ref: dict[str, str]) -> CloudIssues:
    by_sev: dict[str, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    for kind, sev, n in rows:
        by_sev[sev] += int(n)
        by_kind[kind] += int(n)

    def c(value: int, name: str, **p: Any) -> Metric[int]:
        return _count(value, "checks", now, f"cloud.issues.{name}", **ref, **p)

    return CloudIssues(total=c(sum(by_sev.values()), "total"), high=c(by_sev["high"], "high"),
                  medium=c(by_sev["medium"], "medium"), low=c(by_sev["low"], "low"),
                  by_kind=[CloudKindIssues(kind=k, issues=c(by_kind[k], "kind", kind=k))
                           for k in KINDS])


def _node(level: Level, key: str, label: str, figures: Figures, now: datetime, period: CloudPeriod,
          *, figure_key: str | None = None) -> CloudNode:
    return _make(level, key, label, figures, now, period, figure_key or key)


def _total(level: Level, key: str, label: str, figures: Figures, now: datetime,
           period: CloudPeriod) -> CloudNode:
    return _make(level, key, label, figures, now, period, None)


def _make(level: Level, key: str, label: str, figures: Figures, now: datetime,
          period: CloudPeriod, pick: str | None) -> CloudNode:
    ref = {"level": str(figures["level"]), "key": str(pick), **figures["where"]}
    backup = _pick(figures["backup"], pick)
    policy = _pick(figures["policy"], pick)

    def c(rows: list[tuple[Any, ...]], i: int, unit: str, name: str) -> Metric[int]:
        return _count(sum(int(r[i]) for r in rows), unit, now, f"cloud.{name}", **ref)

    costs: dict[str, tuple[Decimal, Decimal]] = {}
    for ccy, amount, previous in _pick(figures["cost"], pick):
        a, p = costs.get(ccy, (Decimal(0), Decimal(0)))
        costs[ccy] = (a + Decimal(amount), p + Decimal(previous))
    cref = query_ref("cloud.cost", 1, start=period.start, end=period.end, **ref)
    pref = query_ref("cloud.cost_previous", 1, start=period.previous_start,
                     end=period.previous_end, **ref)
    return CloudNode(
        level=level, key=key, label=label,
        issues=_issues(_pick(figures["issues"], pick), now, ref),
        backup=CloudBackup(protected=c(backup, 0, "items", "backup.protected"),
                      failing=c(backup, 1, "items", "backup.failing"),
                      unprotected=c(_pick(figures["unprotected"], pick), 0, "resources",
                                    "backup.unprotected")),
        policy=CloudPolicy(compliant=c(policy, 0, "states", "policy.compliant"),
                      non_compliant=c(policy, 1, "states", "policy.non_compliant")),
        cost=[CloudCostFigure(
            currency=ccy,
            amount=Metric[Decimal](value=a, unit=ccy, source_system=COST_SOURCE,
                                   as_of=_end(period.end), query_ref=cref, is_modelled=False),
            previous=Metric[Decimal](value=p, unit=ccy, source_system=COST_SOURCE,
                                     as_of=_end(period.previous_end), query_ref=pref,
                                     is_modelled=False))
            for ccy, (a, p) in sorted(costs.items())],
        resources=c(_pick(figures["resources"], pick), 0, "resources", "resources"))


def _check(r: tuple[Any, ...]) -> CloudCheckView:
    return CloudCheckView(
        id=r[0], tenant=TenantView(id=r[1], name=r[2]), account_id=r[3], kind=r[4], rule=r[5],
        category=r[6], title=r[7], state=r[8], severity=r[9], source_status=r[10],
        resource_id=r[11], resource_native_id=r[12], resource_type=r[13], resource_group=r[14],
        region=r[15], detail=dict(r[16] or {}), observed_at=r[17], first_seen=r[18],
        last_seen=r[19], resolved_at=r[20])


async def _trend(conn: Any, period: CloudPeriod, account: UUID | None,
                 kind: str | None) -> tuple[Any, ...]:
    found = (await conn.execute(_TREND, {
        "s": _start(period.start), "e": _start(period.end + timedelta(days=1)),
        "ps": _start(period.previous_start), "pe": _start(period.start),
        "account": account, "kind": kind})).one()
    return tuple(found)


def _trend_view(row: tuple[Any, ...], now: datetime, period: CloudPeriod) -> CloudTrend:
    def c(i: int, name: str, start: date, end: date) -> Metric[int]:
        return _count(row[i], "checks", now, f"cloud.trend.{name}", start=start, end=end)

    return CloudTrend(opened=c(0, "opened", period.start, period.end),
                 resolved=c(1, "resolved", period.start, period.end),
                 previous_opened=c(2, "opened", period.previous_start, period.previous_end),
                 previous_resolved=c(3, "resolved", period.previous_start,
                                     period.previous_end))
