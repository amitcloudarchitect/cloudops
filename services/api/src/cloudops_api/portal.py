"""Customer portal endpoints (step 18, HLD M2 and M22, ADR 0018).

The portal is the console seen through a customer role. These are the views
the customer roles land on that the console did not have yet, plus the two
administrative flows the portal needs:

- ``/v1/performance`` (VP or director), ``/v1/posture`` (security officer) and
  ``/v1/approvals`` (approver). Same rules as ``bff``: a matrix permission,
  queries under the caller's scope, a ``Metric`` for every number. Staff may use
  them too, across their tenants. A customer's queries run as
  ``cloudops_customer``, so they name only the columns that role is granted.
- ``/v1/tenant/...``: a customer administrator manages its own tenant's users
  (ADR 0018, D2). The access functions decide who the caller administers; they
  are called in the resolution scope because the customer database role cannot
  reach the access schema at all.
- ``/v1/onboarding/...``: a platform administrator onboards a tenant to the
  portal (M2): federation, the agreed role set, the first customer administrator,
  then go-live. Staff only; ``access.manage``.

Refusals from the functions map to structured codes. Nothing from the database
reaches the client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_api.access import Access
from cloudops_api.bff import TenantView
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
from cloudops_tenancy import ScopedConnection, TenantScope

router = APIRouter(prefix="/v1", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

MAX_LIST: Final = 500
TICKET_SOURCE: Final = "servicenow"
PERFORMANCE_MONTHS: Final = 6
ONBOARDING: Final = "access.manage"

# The access functions' refusals (migration 0011), by SQLSTATE.
_REFUSALS: Final = {
    "42501": (403, "forbidden"),
    "P0002": (404, "not_found"),
    "23503": (404, "not_found"),  # an unknown tenant fails its foreign key
    "POSEQ": (409, "step_out_of_order"),
    "POLST": (409, "last_administrator"),
    "23505": (409, "conflict"),
    "23514": (422, "invalid_request"),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _metric(value: int, unit: str, source: str, name: str, **params: Any) -> Metric[int]:
    return Metric[int](value=value, unit=unit, source_system=source, as_of=_now(),
                       query_ref=query_ref(name, 1, live=True, **params), is_modelled=False)


@asynccontextmanager
async def _calling(request: Request, scope: TenantScope) -> AsyncIterator[ScopedConnection]:
    try:
        async with platform(request).db.transaction(scope) as conn:
            yield conn
    except DBAPIError as exc:
        refusal = _REFUSALS.get(str(getattr(exc.orig, "sqlstate", "")))
        if refusal is None:
            raise
        raise ApiError(*refusal) from None


# --- performance (VP or director) --------------------------------------------------


class MonthPerformance(ApiModel):
    month: date
    incidents_opened: Metric[int]
    incidents_resolved: Metric[int]
    # Resolved incidents that met their SLA, as a percentage. None: none resolved.
    sla_attainment: Metric[Decimal] | None
    changes: Metric[int]
    service_requests: Metric[int]


class Performance(ApiModel):
    months: list[MonthPerformance]
    open_incidents: Metric[int]
    sla_breached_open: Metric[int]


# The UTC calendar month a timestamp falls in, as a date.
_OPENED_MONTH: Final = "(date_trunc('month', opened_at, 'UTC') AT TIME ZONE 'UTC')::date"
_CLOSED_MONTH: Final = "(date_trunc('month', closed_at, 'UTC') AT TIME ZONE 'UTC')::date"


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _months_back(start: date, n: int) -> date:
    year, month = divmod(start.year * 12 + start.month - 1 - n, 12)
    return date(year, month + 1, 1)


@router.get("/performance", response_model=Performance)
@requires("tickets.read")
async def performance(request: Request, access: AccessDep) -> Performance:
    first = _months_back(_month_start(_now().date()), PERFORMANCE_MONTHS - 1)
    since = datetime.combine(first, datetime.min.time(), UTC)
    async with platform(request).db.transaction(access.scope) as conn:
        opened = {(r[0], r[1]): int(r[2]) for r in _rows(await conn.execute(text(
            f"SELECT {_OPENED_MONTH}, kind, count(*)"  # noqa: S608
            " FROM cloudops.work_item WHERE opened_at >= :since GROUP BY 1, 2"),
            {"since": since}))}
        resolved = {r[0]: (int(r[1]), int(r[2]), int(r[3])) for r in _rows(await conn.execute(
            text(f"SELECT {_CLOSED_MONTH}, count(*),"  # noqa: S608
                 " count(*) FILTER (WHERE made_sla), count(*) FILTER (WHERE made_sla IS NOT NULL)"
                 " FROM cloudops.work_item WHERE kind = 'incident' AND closed_at >= :since"
                 " GROUP BY 1"), {"since": since}))}
        open_now, breached_now = tuple((await conn.execute(text(
            "SELECT count(*) FILTER (WHERE kind = 'incident'),"
            " count(*) FILTER (WHERE sla_breached)"
            " FROM cloudops.work_item WHERE closed_at IS NULL"))).one())

    def count(month: date, kind: str) -> Metric[int]:
        return _metric(opened.get((month, kind), 0), kind.replace("_", " ") + "s",
                       TICKET_SOURCE, f"tickets.opened_{kind}", month=month)

    months: list[MonthPerformance] = []
    for i in range(PERFORMANCE_MONTHS):
        month = _months_back(first, -i)
        total, made, measured = resolved.get(month, (0, 0, 0))
        attainment = None if measured == 0 else Metric[Decimal](
            value=(Decimal(made) * 100 / Decimal(measured)), unit="percent",
            source_system=TICKET_SOURCE, as_of=_now(),
            query_ref=query_ref("tickets.sla_attainment", 1, month=month), is_modelled=False)
        months.append(MonthPerformance(
            month=month,
            incidents_opened=count(month, "incident"),
            incidents_resolved=_metric(total, "incidents", TICKET_SOURCE,
                                       "tickets.resolved_incident", month=month),
            sla_attainment=attainment,
            changes=count(month, "change_task"),
            service_requests=count(month, "service_request"),
        ))
    return Performance(
        months=months,
        open_incidents=_metric(int(open_now or 0), "incidents", TICKET_SOURCE,
                               "tickets.open_incidents"),
        sla_breached_open=_metric(int(breached_now or 0), "work items", TICKET_SOURCE,
                                  "tickets.sla_breached_open"),
    )


# --- posture (security officer) ----------------------------------------------------


Severity = Literal[1, 2, 3, 4, 5]
AgeBandName = Literal["0-30", "31-90", "91+"]


class SeverityCount(ApiModel):
    severity: Severity
    open: Metric[int]


class AgeBand(ApiModel):
    band: AgeBandName
    open: Metric[int]


class OpenFinding(ApiModel):
    qid: str  # Qualys identifier, not a quantity
    title: str
    severity: Severity
    asset_name: str | None
    resource_id: UUID | None
    first_detected: datetime
    age: Metric[int]


class Posture(ApiModel):
    by_severity: list[SeverityCount]
    by_age: list[AgeBand]
    findings: list[OpenFinding]
    open_total: Metric[int]


_AGE_BANDS: Final[tuple[tuple[AgeBandName, int, int | None], ...]] = (
    ("0-30", 0, 30), ("31-90", 31, 90), ("91+", 91, None))


@router.get("/posture", response_model=Posture)
@requires("vulnerabilities.read")
async def posture(request: Request, access: AccessDep) -> Posture:
    now = _now()
    async with platform(request).db.transaction(access.scope) as conn:
        severities = {int(r[0]): int(r[1]) for r in _rows(await conn.execute(text(
            "SELECT severity, count(*) FROM cloudops.vulnerability"
            " WHERE remediated_at IS NULL GROUP BY 1")))}
        ages = [int(r[0]) for r in _rows(await conn.execute(text(
            "SELECT floor(extract(epoch FROM (:now - first_detected)) / 86400)::int"
            " FROM cloudops.vulnerability WHERE remediated_at IS NULL"), {"now": now}))]
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT qid, title, severity, asset_name, resource_id, first_detected"
            " FROM cloudops.vulnerability WHERE remediated_at IS NULL"
            " ORDER BY severity DESC, first_detected, qid LIMIT :limit"), {"limit": MAX_LIST})]
    return Posture(
        by_severity=[
            SeverityCount(severity=s, open=_metric(severities.get(s, 0), "vulnerabilities",
                                                   "qualys", "vulnerabilities.open_by_severity",
                                                   severity=s))
            for s in (5, 4, 3, 2, 1)
        ],
        by_age=[
            AgeBand(band=band, open=_metric(
                sum(1 for a in ages if a >= low and (high is None or a <= high)),
                "vulnerabilities", "qualys", "vulnerabilities.open_by_age", band=band))
            for band, low, high in _AGE_BANDS
        ],
        findings=[
            OpenFinding(qid=str(qid), title=title, severity=sev, asset_name=asset,
                        resource_id=rid, first_detected=first,
                        age=Metric[int](value=max((now - first).days, 0), unit="days",
                                        source_system="qualys", as_of=now,
                                        query_ref=query_ref("vulnerabilities.age", 1, qid=qid),
                                        is_modelled=False))
            for qid, title, sev, asset, rid, first in rows
        ],
        open_total=_metric(sum(severities.values()), "vulnerabilities", "qualys",
                           "vulnerabilities.open_total"),
    )


# --- approvals (approver) ----------------------------------------------------------


class PendingChange(ApiModel):
    number: str
    tenant: TenantView
    change_request: str | None
    short_description: str | None
    state: str | None
    priority: str | None
    configuration_item: str | None
    resource_id: UUID | None
    opened_at: datetime


class Approvals(ApiModel):
    items: list[PendingChange]
    count: Metric[int]
    # Approving and rejecting arrive with the approval state machine (LLD); until
    # then the approver sees what is coming and cannot act from here.
    can_act: Literal[False] = False


@router.get("/approvals", response_model=Approvals)
@requires("approvals.read")
async def approvals(request: Request, access: AccessDep) -> Approvals:
    async with platform(request).db.transaction(access.scope) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT w.number, t.id, t.canonical_name, w.change_request, w.short_description,"
            " w.state, w.priority, w.configuration_item, w.resource_id, w.opened_at"
            " FROM cloudops.work_item w JOIN cloudops.tenant t ON t.id = w.tenant_id"
            " WHERE w.kind = 'change_task' AND w.closed_at IS NULL"
            " ORDER BY w.opened_at, w.number LIMIT :limit"), {"limit": MAX_LIST})]
        total = await conn.scalar(text(
            "SELECT count(*) FROM cloudops.work_item"
            " WHERE kind = 'change_task' AND closed_at IS NULL"))
    return Approvals(
        items=[PendingChange(number=r[0], tenant=TenantView(id=r[1], name=r[2]),
                             change_request=r[3], short_description=r[4], state=r[5],
                             priority=r[6], configuration_item=r[7], resource_id=r[8],
                             opened_at=r[9]) for r in rows],
        count=_metric(int(total or 0), "changes", TICKET_SOURCE, "tickets.open_changes"),
    )


# --- the customer's own users (tenant administrator) --------------------------------


DisplayName = Annotated[str, Field(min_length=1, max_length=200)]
RoleName = Annotated[str, Field(min_length=1, max_length=100)]


class TenantUser(ApiModel):
    oid: UUID
    display_name: str
    role: str
    tenant_admin: bool
    subscription_limited: bool
    disabled_at: datetime | None
    created_at: datetime


class RoleOption(ApiModel):
    name: str
    description: str | None
    landing: str | None


class TenantUserRequest(ApiModel):
    oid: UUID
    display_name: DisplayName
    role: RoleName
    tenant_admin: bool = False


def _as_admin(access: Access) -> TenantScope:
    """The resolution scope, carrying the caller's identity. Only the functions'
    own check of the oid decides what the caller administers."""
    if access.kind != "customer" or not access.is_tenant_admin:
        raise ApiError(403, "forbidden")
    return TenantScope.for_resolution(access.identity.principal_id)


@router.get("/tenant/users", response_model=list[TenantUser])
@requires(AUTHENTICATED)
async def tenant_users(request: Request, access: AccessDep) -> list[TenantUser]:
    async with _calling(request, _as_admin(access)) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT oid, display_name, role_name, is_tenant_admin, subscription_limited,"
            " disabled_at, created_at FROM access.tenant_users(:a)"),
            {"a": access.identity.oid})]
    return [TenantUser(oid=r[0], display_name=r[1], role=r[2], tenant_admin=r[3],
                       subscription_limited=r[4], disabled_at=r[5], created_at=r[6])
            for r in rows]


@router.get("/tenant/roles", response_model=list[RoleOption])
@requires(AUTHENTICATED)
async def tenant_roles(request: Request, access: AccessDep) -> list[RoleOption]:
    async with _calling(request, _as_admin(access)) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT name, description, landing FROM access.tenant_roles(:a)"),
            {"a": access.identity.oid})]
    return [RoleOption(name=r[0], description=r[1], landing=r[2]) for r in rows]


@router.post("/tenant/users", response_model=list[TenantUser])
@requires(AUTHENTICATED)
async def set_tenant_user(
    request: Request, body: TenantUserRequest, access: AccessDep,
) -> list[TenantUser]:
    async with _calling(request, _as_admin(access)) as conn:
        await conn.execute(text("SELECT access.tenant_set_user(:a, :o, :n, :r, :admin)"), {
            "a": access.identity.oid, "o": body.oid, "n": body.display_name.strip(),
            "r": body.role, "admin": body.tenant_admin})
    return await tenant_users(request, access)


@router.post("/tenant/users/{oid}/disable", response_model=list[TenantUser])
@requires(AUTHENTICATED)
async def disable_tenant_user(request: Request, oid: UUID, access: AccessDep) -> list[TenantUser]:
    async with _calling(request, _as_admin(access)) as conn:
        await conn.execute(text("SELECT access.tenant_disable_user(:a, :o)"),
                           {"a": access.identity.oid, "o": oid})
    return await tenant_users(request, access)


# --- onboarding (platform administrator, HLD M2) ------------------------------------


IdpKind = Literal["entra", "saml", "wsfed"]


class Onboarding(ApiModel):
    tenant: TenantView
    status: Literal["not_started", "setup", "live", "suspended"]
    idp_kind: IdpKind | None
    idp_reference: str | None
    domains: list[str]
    federation_recorded_at: datetime | None
    agreed_roles: list[str]
    roles_agreement_ref: str | None
    roles_agreed_at: datetime | None
    first_admin_oid: UUID | None
    first_admin_at: datetime | None
    live_at: datetime | None
    suspended_at: datetime | None
    assistant_enabled: bool  # AMARA for the portal's users (ADR 0029)


class CatalogueRole(ApiModel):
    name: str
    description: str | None
    landing: str | None
    permissions: list[str]


class FederationRequest(ApiModel):
    idp_kind: IdpKind
    # The customer's Entra tenant id, or its SAML / WS-Fed issuer: an identifier.
    idp_reference: Annotated[str, Field(min_length=1, max_length=300)]
    domains: Annotated[list[Annotated[str, Field(min_length=3, max_length=253)]],
                       Field(min_length=1, max_length=20)]


class RoleAgreementRequest(ApiModel):
    roles: Annotated[list[RoleName], Field(min_length=1, max_length=50)]
    # Where the customer's agreement is recorded (a document or ticket reference).
    agreement_ref: Annotated[str, Field(min_length=1, max_length=200)]


class FirstAdminRequest(ApiModel):
    oid: UUID
    display_name: DisplayName
    role: RoleName


class LiveRequest(ApiModel):
    live: bool


def _staff_manager(access: Access) -> None:
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


def _in_scope(request: Request, access: Access, tenant_id: UUID) -> None:
    # A tenant outside the caller's scope reads as one that does not exist.
    if tenant_id not in access.scope.tenant_ids:
        raise ApiError(404, "not_found")
    audit_only(request, [tenant_id])


async def _onboarding(
    conn: ScopedConnection, actor: UUID, tenant_id: UUID | None,
) -> list[Onboarding]:
    # Role names come from the catalogue: the application cannot read the
    # access schema's tables, only call its functions.
    names = {r[0]: str(r[1]) for r in _rows(await conn.execute(text(
        "SELECT id, name FROM access.customer_role_catalogue(:a)"), {"a": actor}))}
    where = "" if tenant_id is None else " WHERE t.id = :t"
    rows = [tuple(r) for r in await conn.execute(text(
        "SELECT t.id, t.canonical_name, o.status, o.idp_kind, o.idp_reference,"  # noqa: S608
        " coalesce(o.domains, '{}'), o.federation_recorded_at,"
        " coalesce((SELECT array_agg(g.role_id) FROM cloudops.portal_role_agreement g"
        "           WHERE g.tenant_id = t.id), '{}'),"
        " o.roles_agreement_ref, o.roles_agreed_at, o.first_admin_oid, o.first_admin_at,"
        " o.live_at, o.suspended_at, t.assistant_enabled"
        " FROM cloudops.tenant t LEFT JOIN cloudops.portal_onboarding o ON o.tenant_id = t.id"
        f"{where} ORDER BY t.canonical_name"), {"t": tenant_id})]
    return [Onboarding(
        tenant=TenantView(id=r[0], name=r[1]), status=r[2] or "not_started", idp_kind=r[3],
        idp_reference=r[4], domains=list(r[5]), federation_recorded_at=r[6],
        agreed_roles=sorted(names.get(i, "unknown role") for i in r[7]),
        roles_agreement_ref=r[8], roles_agreed_at=r[9],
        first_admin_oid=r[10], first_admin_at=r[11], live_at=r[12], suspended_at=r[13],
        assistant_enabled=bool(r[14]),
    ) for r in rows]


@router.get("/onboarding", response_model=list[Onboarding])
@requires(ONBOARDING)
async def list_onboarding(request: Request, access: AccessDep) -> list[Onboarding]:
    _staff_manager(access)
    async with _calling(request, access.scope) as conn:
        return await _onboarding(conn, access.identity.oid, None)


@router.get("/onboarding/roles", response_model=list[CatalogueRole])
@requires(ONBOARDING)
async def role_catalogue(request: Request, access: AccessDep) -> list[CatalogueRole]:
    _staff_manager(access)
    async with _calling(request, access.scope) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT name, description, landing, permissions"
            " FROM access.customer_role_catalogue(:a)"), {"a": access.identity.oid})]
    return [CatalogueRole(name=r[0], description=r[1], landing=r[2], permissions=list(r[3]))
            for r in rows]


async def _step(request: Request, access: AccessDep, tenant_id: UUID, sql: str,
                **params: Any) -> Onboarding:
    _staff_manager(access)
    _in_scope(request, access, tenant_id)
    async with _calling(request, access.scope) as conn:
        await conn.execute(text(sql), {"a": access.identity.oid, "t": tenant_id, **params})
        found = await _onboarding(conn, access.identity.oid, tenant_id)
    if not found:
        raise ApiError(404, "not_found")
    return found[0]


@router.post("/onboarding/{tenant_id}/federation", response_model=Onboarding)
@requires(ONBOARDING)
async def record_federation(
    request: Request, tenant_id: UUID, body: FederationRequest, access: AccessDep,
) -> Onboarding:
    return await _step(
        request, access, tenant_id,
        "SELECT access.portal_record_federation(:a, :t, :kind, :ref, :domains)",
        kind=body.idp_kind, ref=body.idp_reference.strip(),
        domains=[d.strip().lower() for d in body.domains])


@router.post("/onboarding/{tenant_id}/roles", response_model=Onboarding)
@requires(ONBOARDING)
async def agree_roles(
    request: Request, tenant_id: UUID, body: RoleAgreementRequest, access: AccessDep,
) -> Onboarding:
    return await _step(
        request, access, tenant_id,
        "SELECT access.portal_agree_roles(:a, :t, :roles, :ref)",
        roles=sorted(set(body.roles)), ref=body.agreement_ref.strip())


@router.post("/onboarding/{tenant_id}/first-admin", response_model=Onboarding)
@requires(ONBOARDING)
async def assign_first_admin(
    request: Request, tenant_id: UUID, body: FirstAdminRequest, access: AccessDep,
) -> Onboarding:
    return await _step(
        request, access, tenant_id,
        "SELECT access.portal_assign_first_admin(:a, :t, :o, :n, :r)",
        o=body.oid, n=body.display_name.strip(), r=body.role)


@router.post("/onboarding/{tenant_id}/live", response_model=Onboarding)
@requires(ONBOARDING)
async def set_live(
    request: Request, tenant_id: UUID, body: LiveRequest, access: AccessDep,
) -> Onboarding:
    return await _step(request, access, tenant_id,
                       "SELECT access.portal_set_live(:a, :t, :live)", live=body.live)
