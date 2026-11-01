"""Settings → Commercial setup: the SOW register, written through the platform
(HLD M1; ADR 0034), and a request to recompute a customer's margin now.

Same rules as ``commercial``: each endpoint declares its matrix permission,
queries run under the caller's scope so row-level security decides what exists,
and a customer outside scope is a 404 exactly like one that never existed.

Writes go through ``access.record_sow`` and ``access.request_margin_recompute``
(migration 0032), which check ``commercial.write`` and the tenant again in the
database, whatever this module did. A SOW is versioned: an amendment is a new
version effective from a later date, and the one before is closed the day before
it, never edited. Money is a decimal string on the way in (never a JSON number)
and a ``Metric`` in its contract currency on the way out, unrounded.

The rate card itself is published from the same Settings area through
``/v1/commercial/rate-cards`` (``commercial``, ADR 0033).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_api.access import Access
from cloudops_api.bff import TenantView
from cloudops_api.pipeline import (
    ApiError,
    AuditedRoute,
    audit_only,
    current_access,
    narrow,
    platform,
    requires,
)
from cloudops_api.provenance import ApiModel, Metric, query_ref
from cloudops_tenancy import ScopedConnection

router = APIRouter(prefix="/v1", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

SOURCE: Final = "sow_register"
RATIO: Final = "ratio"
# A decimal string: a JSON number would arrive as a float (CLAUDE.md rule 5).
Money = Annotated[str, Field(pattern=r"^\d{1,12}(\.\d{1,6})?$")]
Fraction = Annotated[str, Field(pattern=r"^-?0?\.\d{1,6}$|^0$")]
Frequency = Literal["monthly", "quarterly", "annual"]

# The functions' refusals (migration 0032), by SQLSTATE. Anything else is a 500.
_REFUSALS: Final = {
    "42501": (403, "forbidden"),
    "CMSCP": (404, "not_found"),
    "SWDAT": (422, "effective_date_not_after_current"),
    "22023": (422, "invalid_request"),
    "23514": (422, "invalid_request"),
}


class SowInput(BaseModel):
    """One SOW version as commercial records it. ``amendment_reason`` is required
    once the customer has a SOW: every later version is an amendment."""

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=100)
    effective_from: date
    term_start: date
    term_end: date | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    service_scope: str = Field(min_length=1, max_length=500)
    coverage: Literal["8x5", "16x5", "24x7"] | None = None
    support: Literal["Bronze", "Silver", "Gold"] | None = None
    recurring_fee: Money
    billing_frequency: Frequency
    target_margin: Fraction | None = None
    consumption_uplift: Annotated[str, Field(pattern=r"^\d?\.\d{1,6}$|^[1-9]$")] | None = None
    hosting_acv: Money | None = None
    manage_acv: Money | None = None
    professional_services: Money | None = None
    contracted_ci_count: Annotated[str, Field(pattern=r"^\d{1,7}$")] | None = None
    document_uri: str = Field(min_length=3, max_length=500)
    amendment_reason: str | None = Field(default=None, min_length=3, max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> SowInput:
        if self.term_end is not None and self.term_end < self.term_start:
            raise ValueError("the term ends before it starts")
        if self.term_end is not None and self.term_end < self.effective_from:
            raise ValueError("the version takes effect after the term ends")
        if self.target_margin is not None and not -1 < Decimal(self.target_margin) < 1:
            raise ValueError("the target margin is a fraction")
        if self.consumption_uplift is not None and not (
                0 < Decimal(self.consumption_uplift) < 10):
            raise ValueError("the uplift is above 0 (ADR 0021)")
        return self


class SowVersionView(ApiModel):
    id: UUID
    version: Metric[int]  # 1 for the first SOW, then one per amendment, by effective date
    reference: str
    effective_from: date
    effective_to: date | None  # inclusive
    in_force: bool
    term_start: date | None
    term_end: date | None
    currency: str
    service_scope: str | None
    coverage: str | None
    support: str | None
    recurring_fee: Metric[Decimal] | None
    billing_frequency: Frequency | None
    monthly_manage_revenue: Metric[Decimal]
    target_margin: Metric[Decimal] | None
    consumption_uplift: Metric[Decimal] | None
    hosting_acv: Metric[Decimal] | None
    manage_acv: Metric[Decimal] | None
    professional_services: Metric[Decimal]
    contracted_ci_count: Metric[int] | None
    document_uri: str | None
    amendment_reason: str | None
    entered_by: str | None  # a principal id; absent for a SOW loaded by migration or seed
    entered_at: datetime


class RecomputeView(ApiModel):
    id: UUID
    status: Literal["requested", "running", "succeeded", "failed"]
    requested_at: datetime
    finished_at: datetime | None


class SowRegister(ApiModel):
    tenant: TenantView
    versions: list[SowVersionView]  # newest first
    recompute: RecomputeView | None  # the latest request, if any


_VERSIONS: Final = text(
    "SELECT id, row_number() OVER (ORDER BY effective_from, created_at), reference,"
    "   effective_from, effective_to, term_start, term_end, currency, service_scope,"
    "   coverage::text, support::text, recurring_fee, billing_frequency,"
    "   monthly_manage_revenue, target_margin, consumption_uplift, hosting_acv, manage_acv,"
    "   professional_services, contracted_ci_count, document_uri, amendment_reason,"
    "   entered_by, created_at"
    " FROM cloudops.sow WHERE tenant_id = :t ORDER BY effective_from DESC, created_at DESC"
)
_RECOMPUTE: Final = text(
    "SELECT id, status, requested_at, finished_at FROM cloudops.margin_recompute"
    " WHERE tenant_id = :t ORDER BY requested_at DESC LIMIT 1"
)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _staff(access: Access) -> None:
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


def _version(row: tuple[Any, ...], today: date) -> SowVersionView:
    (sid, n, ref, start, end, t_start, t_end, ccy, scope, cov, sup, fee, freq, monthly,
     target, uplift, hosting, manage, services, cis, uri, why, by, at) = row
    ccy = str(ccy).strip()
    q = query_ref("commercial.sow", 1, sow=str(sid))

    def figure(v: Any, unit: str = ccy) -> Metric[Decimal]:
        return Metric[Decimal](value=Decimal(v), unit=unit, source_system=SOURCE, as_of=at,
                               query_ref=q, is_modelled=False)

    def money(v: Any, unit: str = ccy) -> Metric[Decimal] | None:
        return None if v is None else figure(v, unit)

    def whole(v: Any, unit: str) -> Metric[int]:
        return Metric[int](value=int(v), unit=unit, source_system=SOURCE, as_of=at,
                           query_ref=q, is_modelled=False)

    return SowVersionView(
        id=sid, version=whole(n, "version"), reference=ref, effective_from=start, effective_to=end,
        in_force=start <= today and (end is None or today <= end),
        term_start=t_start, term_end=t_end, currency=ccy, service_scope=scope, coverage=cov,
        support=sup, recurring_fee=money(fee), billing_frequency=freq,
        monthly_manage_revenue=figure(monthly), target_margin=money(target, RATIO),
        consumption_uplift=money(uplift, RATIO), hosting_acv=money(hosting),
        manage_acv=money(manage), professional_services=figure(services),
        contracted_ci_count=None if cis is None else whole(cis, "CIs"), document_uri=uri,
        amendment_reason=why, entered_by=by, entered_at=at)


async def _register(conn: ScopedConnection, tenant_id: UUID) -> SowRegister:
    name = await conn.scalar(text("SELECT canonical_name FROM cloudops.tenant WHERE id = :t"),
                             {"t": tenant_id})
    if name is None:
        raise ApiError(404, "not_found")
    today = await conn.scalar(text("SELECT current_date"))
    versions = [_version(r, today)
                for r in _rows(await conn.execute(_VERSIONS, {"t": tenant_id}))]
    latest = _rows(await conn.execute(_RECOMPUTE, {"t": tenant_id}))
    recompute = None if not latest else RecomputeView(
        id=latest[0][0], status=latest[0][1], requested_at=latest[0][2],
        finished_at=latest[0][3])
    return SowRegister(tenant=TenantView(id=tenant_id, name=str(name)), versions=versions,
                       recompute=recompute)


async def _write(request: Request, access: Access, sql: str,
                 params: dict[str, Any]) -> SowRegister:
    tenant_id: UUID = params["tenant"]
    try:
        # One customer's scope: the selector's narrowing, whatever page asks.
        async with platform(request).db.transaction(narrow(access, tenant_id)) as conn:
            await conn.scalar(text(sql), params)
            register = await _register(conn, tenant_id)
    except DBAPIError as exc:
        refusal = _REFUSALS.get(str(getattr(exc.orig, "sqlstate", "")))
        if refusal is None:
            raise
        raise ApiError(*refusal) from None
    return register


@router.get("/commercial/customers/{tenant_id}/sows", response_model=SowRegister)
@requires("commercial.read")
async def sow_register(request: Request, tenant_id: UUID, access: AccessDep) -> SowRegister:
    """One customer's SOW versions, newest first, and its latest margin recompute."""
    _staff(access)
    async with platform(request).db.transaction(narrow(access, tenant_id)) as conn:
        register = await _register(conn, tenant_id)
    audit_only(request, [tenant_id])
    return register


@router.post("/commercial/customers/{tenant_id}/sows", response_model=SowRegister,
             status_code=201)
@requires("commercial.write")
async def record_sow(
    request: Request, tenant_id: UUID, body: SowInput, access: AccessDep
) -> SowRegister:
    """Record a SOW version: the customer's first, or an amendment effective after
    the current one. Nothing already recorded changes, except that the current
    version now ends the day before the amendment."""
    _staff(access)
    narrow(access, tenant_id)  # 404 outside scope, before any write
    register = await _write(
        request, access, "SELECT access.record_sow(:actor, :tenant, CAST(:sow AS jsonb))",
        {"actor": access.identity.oid, "tenant": tenant_id,
         "sow": json.dumps(body.model_dump(mode="json"))})
    audit_only(request, [tenant_id])
    return register


@router.post("/commercial/customers/{tenant_id}/margin-recompute",
             response_model=RecomputeView, status_code=202)
@requires("commercial.write")
async def request_recompute(request: Request, tenant_id: UUID,
                            access: AccessDep) -> RecomputeView:
    """Ask the FinOps worker to recompute this customer's margin now (this month,
    the month closing, and the restatement window) rather than at the next daily
    run. The API starts nothing itself: the worker claims the request."""
    _staff(access)
    narrow(access, tenant_id)  # 404 outside scope, before any write
    register = await _write(
        request, access, "SELECT access.request_margin_recompute(:actor, :tenant)",
        {"actor": access.identity.oid, "tenant": tenant_id})
    audit_only(request, [tenant_id])
    if register.recompute is None:  # just written, in the same transaction
        raise ApiError(500, "internal_error")
    return register.recompute
