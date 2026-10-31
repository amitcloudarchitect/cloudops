"""Operations console endpoints (step 14, ADR 0014): the work queue, a ticket with
its triage and the reviews of it, a resource's context, and the customer overview.

Same rules as ``bff``: each endpoint declares its matrix permission, queries run
under the caller's scope so row-level security decides what exists, an object
outside scope is a 404 exactly like one that never existed, and every number is
a ``Metric`` with its source and as-of time.

Triage is staff-only: a customer's view of a ticket never carries the agents'
classification. Reviews are append-only; nothing here acts on a customer
environment (stage 4 and later are not built).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, get_args
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, model_validator
from sqlalchemy import text

from cloudops_api.access import Access
from cloudops_api.bff import (
    _RESOURCE_COLUMNS,
    CustomerPrice,
    ResourceStaffView,
    ResourceView,
    TenantView,
    _resource,
    customer_price,
)
from cloudops_api.pipeline import (
    AUTHENTICATED,
    ApiError,
    AuditedRoute,
    audit_only,
    current_access,
    platform,
    requires,
)
from cloudops_api.provenance import ApiModel, Metric, query_ref

router = APIRouter(prefix="/v1", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

MAX_LIST: Final = 500
RECENT_DAYS: Final = 30
CRITICAL_SEVERITY: Final = 4  # Qualys 4 and 5
TICKET_SOURCE: Final = "servicenow"

# The values the classifier allows (its output schema, cloudops_agents.classifier;
# the API image does not carry the agents package, so a test keeps the two equal).
Intent = Literal[
    "service_down", "performance", "disk_space", "backup_failure", "certificate_expiry",
    "patching", "capacity_change", "access_request", "connectivity", "security_alert",
    "decommission", "information_request", "other",
]
ActionClass = Literal["A", "B", "C", "D", "E"]
Risk = Literal["low", "medium", "high"]
INTENTS: Final = get_args(Intent)
ACTION_CLASSES: Final = get_args(ActionClass)
RISKS: Final = get_args(Risk)
MAX_OVERRIDE_TEXT: Final = 200


def _now() -> datetime:
    return datetime.now(UTC)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _count(value: int, unit: str, source: str, name: str, **params: Any) -> Metric[int]:
    return Metric[int](value=value, unit=unit, source_system=source, as_of=_now(),
                       query_ref=query_ref(name, 1, live=True, **params), is_modelled=False)


# --- views -------------------------------------------------------------------------


class WorkItemSummary(ApiModel):
    number: str
    kind: str
    tenant: TenantView
    state: str | None
    priority: str | None
    workgroup: str | None
    short_description: str | None
    configuration_item: str | None
    resource_id: UUID | None
    opened_at: datetime
    closed_at: datetime | None
    sla_breached: bool | None
    # Staff only: the latest triage's route, so the queue shows what needs a person.
    triage_route: str | None = None


class WorkItemList(ApiModel):
    items: list[WorkItemSummary]
    count: Metric[int]
    workgroups: list[str]


class ReviewView(ApiModel):
    id: UUID
    decision: Literal["accept", "override"]
    overrides: dict[str, str] | None
    reason: str | None
    reviewer_oid: UUID
    created_at: datetime


class TriageView(ApiModel):
    id: UUID
    route: str
    stage: Literal[1, 2, 3]  # the pipeline stage it stopped at, a label not a measure
    reasons: list[str]
    classification: dict[str, Any] | None
    enrichment: dict[str, Any] | None
    injection_signals: list[str]
    cost: Metric[Decimal]
    created_at: datetime
    reviews: list[ReviewView]
    # Stage 4 (planning) is not built: the panel says so instead of showing nothing.
    plan: None = None


class WorkItemDetail(WorkItemSummary):
    change_request: str | None
    assigned_to: str | None
    closure_code: str | None
    close_notes: str | None
    resolve_time: Metric[int] | None
    triage: TriageView | None  # staff only; None for customers or before any run


class TriageOverrides(ApiModel):
    """What a reviewer may change: the classification's fields, each to a value its
    schema allows. Typed here so the console's choices come from the contract."""

    intent: Intent | None = None
    action_class: ActionClass | None = None
    risk: Risk | None = None
    affected_ci: str | None = Field(default=None, min_length=1, max_length=MAX_OVERRIDE_TEXT)
    customer: str | None = Field(default=None, min_length=1, max_length=MAX_OVERRIDE_TEXT)

    def changes(self) -> dict[str, str]:
        return {k: v.strip() for k, v in self.model_dump(exclude_none=True).items()}


REVIEWABLE: Final = tuple(TriageOverrides.model_fields)


class ReviewRequest(ApiModel):
    decision: Literal["accept", "override"]
    overrides: TriageOverrides | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _shape(self) -> ReviewRequest:
        changes = self.overrides.changes() if self.overrides else {}
        if self.decision == "accept":
            if self.overrides is not None:
                raise ValueError("an acceptance changes nothing")
            return self
        if not changes or not (self.reason or "").strip():
            raise ValueError("an override says what changed and why")
        if any(not v for v in changes.values()):
            raise ValueError("an overridden value cannot be blank")
        return self


class VulnerabilityView(ApiModel):
    qid: str  # Qualys identifier, not a quantity
    title: str
    severity: Literal[1, 2, 3, 4, 5]
    first_detected: datetime
    last_detected: datetime | None
    remediated_at: datetime | None
    solution: str | None


class TelemetryStatus(ApiModel):
    available: bool
    reason: str


class ResourceContext(ApiModel):
    resource: ResourceStaffView | ResourceView
    vulnerabilities: list[VulnerabilityView] | None  # None: not permitted to see them
    open_vulnerabilities: Metric[int] | None
    recent_changes: list[WorkItemSummary] | None  # None: not permitted to see tickets
    open_incidents: list[WorkItemSummary] | None
    metrics: TelemetryStatus


class CustomerOverview(ApiModel):
    tenant: TenantView
    resources: Metric[int] | None
    open_incidents: Metric[int] | None
    sla_breached_open: Metric[int] | None
    critical_vulnerabilities: Metric[int] | None
    cost_30d: list[Metric[Decimal]] | None  # staff: the provider's charge to Getronics
    price_30d: list[CustomerPrice] | None  # a customer: the price it is charged (ADR 0021)


# --- work items --------------------------------------------------------------------

_ITEM_COLUMNS: Final = (
    "w.number, w.kind, t.id, t.canonical_name, w.state, w.priority, w.assignment_workgroup,"
    " w.short_description, w.configuration_item, w.resource_id, w.opened_at, w.closed_at,"
    " w.sla_breached"
)
# A customer scope runs as cloudops_customer, which is refused the Getronics
# workgroup and assignee (migration 0011): naming them would fail, not hide.
_CUSTOMER_ITEM_COLUMNS: Final = _ITEM_COLUMNS.replace("w.assignment_workgroup", "NULL::text")


def _item_columns(staff: bool) -> str:
    return _ITEM_COLUMNS if staff else _CUSTOMER_ITEM_COLUMNS
_LATEST_ROUTE: Final = (
    "(SELECT tr.route FROM cloudops.triage_result tr WHERE tr.tenant_id = w.tenant_id"
    " AND tr.number = w.number ORDER BY tr.created_at DESC LIMIT 1)"
)


def _summary(row: tuple[Any, ...], staff: bool, route: str | None = None) -> WorkItemSummary:
    (number, kind, tid, tname, state, priority, workgroup, short, ci, rid, opened, closed,
     breached) = row[:13]
    return WorkItemSummary(
        number=number, kind=kind, tenant=TenantView(id=tid, name=tname), state=state,
        priority=priority, workgroup=workgroup, short_description=short,
        configuration_item=ci, resource_id=rid, opened_at=opened, closed_at=closed,
        sla_breached=breached, triage_route=route if staff else None,
    )


@router.get("/work-items", response_model=WorkItemList)
@requires("tickets.read")
async def list_work_items(
    request: Request,
    access: AccessDep,
    kind: Annotated[Literal["incident", "change_task", "service_request", "project_task"]
                    | None, Query()] = None,
    workgroup: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[Literal["open", "closed", "all"], Query()] = "open",
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> WorkItemList:
    staff = access.kind == "staff"
    where = ["true"]
    params: dict[str, Any] = {"limit": MAX_LIST}
    if kind:
        where.append("w.kind = :kind")
        params["kind"] = kind
    if workgroup and staff:
        where.append("w.assignment_workgroup = :wg")
        params["wg"] = workgroup
    if status == "open":
        where.append("w.closed_at IS NULL")
    elif status == "closed":
        where.append("w.closed_at IS NOT NULL")
    if q:
        where.append("(w.number ILIKE :q OR w.short_description ILIKE :q)")
        params["q"] = f"%{q.replace('%', '').replace('_', '')}%"
    clause = " AND ".join(where)
    route = f", {_LATEST_ROUTE}" if staff else ", NULL"
    async with platform(request).db.transaction(access.scope) as conn:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
            f"SELECT {_item_columns(staff)}{route} FROM cloudops.work_item w"  # noqa: S608
            " JOIN cloudops.tenant t ON t.id = w.tenant_id"
            f" WHERE {clause} ORDER BY w.opened_at DESC, w.number LIMIT :limit"), params)]
        total = await conn.scalar(text(
            f"SELECT count(*) FROM cloudops.work_item w WHERE {clause}"),  # noqa: S608
            {k: v for k, v in params.items() if k != "limit"})
        groups = [str(r[0]) for r in await conn.execute(text(
            "SELECT DISTINCT assignment_workgroup FROM cloudops.work_item"
            " WHERE assignment_workgroup IS NOT NULL ORDER BY 1"))] if staff else []
    return WorkItemList(
        items=[_summary(r, staff, r[13]) for r in rows],
        count=_count(int(total or 0), "work items", TICKET_SOURCE, "tickets.work_item_count",
                     kind=kind, status=status),
        workgroups=groups,
    )


async def _triage(conn: Any, tenant: UUID, number: str) -> TriageView | None:
    found = (await conn.execute(text(
        "SELECT id, route, stage, reasons, classification, enrichment, injection_signals, cost,"
        " cost_currency, created_at FROM cloudops.triage_result"
        " WHERE tenant_id = :t AND number = :n ORDER BY created_at DESC LIMIT 1"),
        {"t": tenant, "n": number})).one_or_none()
    if found is None:
        return None
    (tid, route, stage, reasons, cls, enr, signals, cost, currency, created) = tuple(found)
    reviews: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
        "SELECT id, decision, overrides, reason, reviewer_oid, created_at"
        " FROM cloudops.triage_review WHERE triage_result_id = :id ORDER BY created_at"),
        {"id": tid})]
    return TriageView(
        id=tid, route=route, stage=stage, reasons=list(reasons or []), classification=cls,
        enrichment=enr, injection_signals=list(signals or []),
        cost=Metric[Decimal](value=Decimal(cost), unit=str(currency).strip(),
                             source_system="cloudops_ai_gateway", as_of=created,
                             query_ref=query_ref("ai.triage_cost", 1, triage=str(tid)),
                             is_modelled=False),
        created_at=created,
        reviews=[ReviewView(id=r[0], decision=r[1], overrides=r[2], reason=r[3],
                            reviewer_oid=r[4], created_at=r[5]) for r in reviews],
    )


@router.get("/work-items/{number}", response_model=WorkItemDetail)
@requires("tickets.read")
async def get_work_item(request: Request, number: str, access: AccessDep) -> WorkItemDetail:
    staff = access.kind == "staff"
    async with platform(request).db.transaction(access.scope) as conn:
        found = (await conn.execute(text(
            f"SELECT {_item_columns(staff)}, w.change_request,"  # noqa: S608
            f" {'w.assigned_to' if staff else 'NULL::text'}, w.closure_code,"
            " w.close_notes, w.resolve_time_seconds FROM cloudops.work_item w"
            " JOIN cloudops.tenant t ON t.id = w.tenant_id WHERE w.number = :n"
            " ORDER BY w.opened_at DESC LIMIT 1"), {"n": number})).one_or_none()
        if found is None:
            raise ApiError(404, "not_found")
        row: tuple[Any, ...] = tuple(found)
        triage = await _triage(conn, row[2], number) if staff else None
    audit_only(request, [row[2]])
    base = _summary(row, staff, triage.route if triage else None)
    resolve = row[17]
    return WorkItemDetail(
        **base.model_dump(),
        change_request=row[13], assigned_to=row[14] if staff else None,
        closure_code=row[15], close_notes=row[16],
        resolve_time=None if resolve is None else Metric[int](
            value=int(resolve), unit="seconds", source_system=TICKET_SOURCE,
            as_of=row[11] or _now(), query_ref=query_ref("tickets.resolve_time", 1, number=number),
            is_modelled=False),
        triage=triage,
    )


@router.post("/work-items/{number}/triage/{triage_id}/reviews", response_model=ReviewView,
             status_code=201)
@requires("triage.review")
async def review_triage(
    request: Request, number: str, triage_id: UUID, body: ReviewRequest, access: AccessDep,
) -> ReviewView:
    if access.kind != "staff":
        raise ApiError(404, "not_found")
    async with platform(request).db.transaction(access.scope) as conn:
        found = (await conn.execute(text(
            "SELECT tenant_id FROM cloudops.triage_result WHERE id = :id AND number = :n"),
            {"id": triage_id, "n": number})).one_or_none()
        if found is None:
            raise ApiError(404, "not_found")
        tenant = found[0]
        saved: tuple[Any, ...] = tuple((await conn.execute(text(
            "INSERT INTO cloudops.triage_review (tenant_id, triage_result_id, decision,"
            " overrides, reason, reviewer_oid) VALUES (:t, :id, :d, CAST(:o AS jsonb), :r, :who)"
            " RETURNING id, created_at"),
            {"t": tenant, "id": triage_id, "d": body.decision,
             "o": None if body.overrides is None else json.dumps(body.overrides.changes()),
             "r": (body.reason or "").strip() or None, "who": access.identity.oid})).one())
    audit_only(request, [tenant])
    return ReviewView(id=saved[0], decision=body.decision,
                      overrides=None if body.overrides is None else body.overrides.changes(),
                      reason=(body.reason or "").strip() or None,
                      reviewer_oid=access.identity.oid, created_at=saved[1])


# --- resource context --------------------------------------------------------------


@router.get("/resources/{resource_id}/context", response_model=ResourceContext)
@requires("inventory.read")
async def resource_context(
    request: Request, resource_id: UUID, access: AccessDep,
) -> ResourceContext:
    staff = access.kind == "staff"
    since = _now() - timedelta(days=RECENT_DAYS)
    vulns: list[VulnerabilityView] | None = None
    open_vulns: Metric[int] | None = None
    changes: list[WorkItemSummary] | None = None
    incidents: list[WorkItemSummary] | None = None
    async with platform(request).db.transaction(access.scope) as conn:
        found = (await conn.execute(text(
            f"SELECT {_RESOURCE_COLUMNS} FROM cloudops.resource r WHERE r.id = :id"),  # noqa: S608
            {"id": resource_id})).one_or_none()
        if found is None:
            raise ApiError(404, "not_found")
        row: tuple[Any, ...] = tuple(found)
        if access.allows("vulnerabilities.read"):
            vulns = [VulnerabilityView(qid=str(r[0]), title=r[1], severity=r[2],
                                       first_detected=r[3], last_detected=r[4],
                                       remediated_at=r[5], solution=r[6])
                     for r in _rows(await conn.execute(text(
                         "SELECT qid, title, severity, first_detected, last_detected,"
                         " remediated_at, solution FROM cloudops.vulnerability"
                         " WHERE resource_id = :id ORDER BY remediated_at IS NOT NULL,"
                         " severity DESC, first_detected DESC LIMIT :limit"),
                         {"id": resource_id, "limit": MAX_LIST}))]
            open_vulns = _count(sum(1 for v in vulns if v.remediated_at is None),
                                "vulnerabilities", "qualys", "vulnerabilities.open_for_resource",
                                resource=str(resource_id))
        if access.allows("tickets.read"):
            linked: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
                f"SELECT {_item_columns(staff)}, NULL FROM cloudops.work_item w"  # noqa: S608
                " JOIN cloudops.tenant t ON t.id = w.tenant_id WHERE w.resource_id = :id"
                " AND (w.kind = 'change_task' AND w.opened_at >= :since"
                "      OR w.kind = 'incident' AND w.closed_at IS NULL)"
                " ORDER BY w.opened_at DESC LIMIT :limit"),
                {"id": resource_id, "since": since, "limit": MAX_LIST})]
            changes = [_summary(r, staff) for r in linked if r[1] == "change_task"]
            incidents = [_summary(r, staff) for r in linked if r[1] == "incident"]
    audit_only(request, [row[1]])
    return ResourceContext(
        resource=_resource(row, access),
        vulnerabilities=vulns, open_vulnerabilities=open_vulns,
        recent_changes=changes, open_incidents=incidents,
        metrics=TelemetryStatus(
            available=False,
            reason="Live metrics need the telemetry store, which is not built yet (LLD L1).",
        ),
    )


# --- customers ---------------------------------------------------------------------


@router.get("/customers", response_model=list[CustomerOverview])
@requires(AUTHENTICATED)
async def list_customers(request: Request, access: AccessDep) -> list[CustomerOverview]:
    """One row per tenant in scope. Each figure is present only when the caller
    holds the permission it comes from; absent is not zero."""
    since = (_now() - timedelta(days=RECENT_DAYS)).date()
    can = access.allows
    async with platform(request).db.transaction(access.scope) as conn:
        tenants: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
            "SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name"))]

        async def per_tenant(sql: str, **params: Any) -> dict[UUID, Any]:
            return {r[0]: r[1] for r in _rows(await conn.execute(text(sql), params))}

        resources = await per_tenant(
            "SELECT tenant_id, count(*) FROM cloudops.resource WHERE deleted_at IS NULL"
            " GROUP BY 1") if can("inventory.read") else None
        incidents = await per_tenant(
            "SELECT tenant_id, count(*) FROM cloudops.work_item WHERE kind = 'incident'"
            " AND closed_at IS NULL GROUP BY 1") if can("tickets.read") else None
        breached = await per_tenant(
            "SELECT tenant_id, count(*) FROM cloudops.work_item WHERE closed_at IS NULL"
            " AND sla_breached GROUP BY 1") if can("tickets.read") else None
        critical = await per_tenant(
            "SELECT tenant_id, count(*) FROM cloudops.vulnerability WHERE remediated_at IS NULL"
            " AND severity >= :sev GROUP BY 1", sev=CRITICAL_SEVERITY,
        ) if can("vulnerabilities.read") else None
        # A customer reads the price, never the cost (migrations 0014-0015, ADR 0021).
        customer = access.kind == "customer"
        cost_rows = [tuple(r) for r in await conn.execute(text(
            "SELECT tenant_id, currency, sum(amortized_cost), max(usage_date)"
            " FROM cloudops.cost_fact WHERE usage_date >= :since GROUP BY 1, 2 ORDER BY 1, 2"),
            {"since": since})] if can("cost.read") and not customer else None
        price_rows = [tuple(r) for r in await conn.execute(text(
            "SELECT tenant_id, currency, sum(price), max(usage_date), bool_and(price IS NOT NULL)"
            " FROM cloudops.customer_price(:since, :today) GROUP BY 1, 2 ORDER BY 1, 2"),
            {"since": since, "today": _now().date()})] if can("cost.read") and customer else None

    def metric(counts: dict[UUID, Any] | None, tid: UUID, unit: str, source: str,
               name: str) -> Metric[int] | None:
        if counts is None:
            return None
        return _count(int(counts.get(tid, 0)), unit, source, name, tenant=str(tid))

    out: list[CustomerOverview] = []
    for tid, name in tenants:
        costs = None if cost_rows is None else [
            Metric[Decimal](value=Decimal(total), unit=currency,
                            source_system="cloud_cost", as_of=datetime.combine(
                                last, datetime.max.time(), UTC),
                            query_ref=query_ref("cost.amortized_30d", 1, tenant=str(tid)),
                            is_modelled=False)
            for t, currency, total, last in cost_rows if t == tid]
        pref = query_ref("cost.customer_price_30d", 1, tenant=str(tid))
        prices = None if price_rows is None else [
            customer_price(currency, total, priced, last, pref)
            for t, currency, total, last, priced in price_rows if t == tid]
        out.append(CustomerOverview(
            tenant=TenantView(id=tid, name=name),
            resources=metric(resources, tid, "resources", "platform_inventory",
                             "inventory.resource_count"),
            open_incidents=metric(incidents, tid, "incidents", TICKET_SOURCE,
                                  "tickets.open_incidents"),
            sla_breached_open=metric(breached, tid, "work items", TICKET_SOURCE,
                                     "tickets.sla_breached_open"),
            critical_vulnerabilities=metric(critical, tid, "vulnerabilities", "qualys",
                                            "vulnerabilities.critical_open"),
            cost_30d=costs,
            price_30d=prices,
        ))
    return out
