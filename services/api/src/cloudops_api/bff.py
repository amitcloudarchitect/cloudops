"""Backend-for-frontend endpoints (lifecycle stages 3 to 6).

Each endpoint declares the permission it needs from the matrix (stage 3) and
shapes its response for the caller's kind: customers get the customer view,
staff the staff view. Queries run through packages/tenancy under the
resolved scope (stage 4), so row-level security decides what exists (stage
5). A resource outside scope is indistinguishable from one that never
existed: 404 either way, never 403. Every number is a ``Metric`` (stage 6).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text

from cloudops_api.access import Access
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

COST_SOURCE: Final = {"azure": "azure_cost_management", "aws": "aws_cost_and_usage"}
PRICE_SOURCE: Final = "customer_price"
Currency = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
MAX_LIST: Final = 500


# --- views -------------------------------------------------------------------------


class TenantView(ApiModel):
    id: UUID
    name: str


class MeView(ApiModel):
    oid: UUID
    kind: str
    role: str
    permissions: list[str]
    tenants: list[TenantView]
    subscription_limited: bool
    tenant_admin: bool
    landing: str | None


class ResourceView(ApiModel):
    """What a customer sees of a resource."""

    id: UUID
    name: str
    type: str
    region: str | None
    state: str | None


class ResourceStaffView(ApiModel):
    """What Getronics staff see: provider identifiers and tags as well."""

    id: UUID
    tenant_id: UUID
    account_id: UUID
    native_id: str
    name: str
    type: str
    type_native: str
    region: str | None
    state: str | None
    tags: dict[str, str]


class ResourceList(ApiModel):
    items: list[ResourceStaffView | ResourceView]
    count: Metric[int]


class CostLine(ApiModel):
    """What Getronics staff see: the provider's charge to Getronics."""

    provider: str
    currency: str
    amortized: Metric[Decimal]
    converted: Metric[Decimal] | None  # None: no currency asked for, or no rate in force


class CostSummary(ApiModel):
    period_start: date
    period_end: date
    reporting_currency: str | None
    lines: list[CostLine]


class CustomerPrice(ApiModel):
    """What a customer sees instead of cost (ADR 0021): its consumption at the
    price Getronics charges. ``price`` is None, and ``not_captured`` says why,
    when any day in the period has no uplift in force: never the cost, never a
    zero."""

    currency: str
    price: Metric[Decimal] | None
    not_captured: Literal["no_uplift"] | None


class PriceLine(CustomerPrice):
    provider: str
    converted: Metric[Decimal] | None  # None: not asked for, not captured, or no rate


class PriceSummary(ApiModel):
    period_start: date
    period_end: date
    reporting_currency: str | None
    lines: list[PriceLine]


class SowView(ApiModel):
    reference: str
    effective_from: date
    effective_to: date | None
    currency: str
    monthly_manage_revenue: Metric[Decimal]


def _end_of(day: date) -> datetime:
    """Data for ``day`` is complete as of the following midnight, UTC."""
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


# --- endpoints ---------------------------------------------------------------------

_RESOURCE_COLUMNS: Final = (
    "r.id, r.tenant_id, r.account_id, r.native_id, r.name, r.type_canonical,"
    " r.type_native, r.region, r.state, r.tags"
)


def _resource(row: tuple[Any, ...], access: Access) -> ResourceStaffView | ResourceView:
    rid, tid, aid, native, name, canonical, type_native, region, state, tags = row
    if access.kind == "customer":
        return ResourceView(id=rid, name=name, type=canonical, region=region, state=state)
    return ResourceStaffView(
        id=rid, tenant_id=tid, account_id=aid, native_id=native, name=name,
        type=canonical, type_native=type_native, region=region, state=state,
        tags={str(k): str(v) for k, v in dict(tags or {}).items()},
    )


@router.get("/me", response_model=MeView)
@requires(AUTHENTICATED)
async def me(request: Request, access: AccessDep) -> MeView:
    async with platform(request).db.transaction(access.scope) as conn:
        rows: list[tuple[Any, ...]] = [
            tuple(r)
            for r in await conn.execute(
                text("SELECT id, canonical_name FROM cloudops.tenant ORDER BY 2")
            )
        ]
    return MeView(
        oid=access.identity.oid,
        kind=access.kind,
        role=access.role_name,
        permissions=sorted(access.permissions),
        tenants=[TenantView(id=r[0], name=r[1]) for r in rows],
        subscription_limited=access.scope.account_ids is not None,
        tenant_admin=access.is_tenant_admin,
        landing=access.landing,
    )


@router.get("/resources", response_model=ResourceList)
@requires("inventory.read")
async def list_resources(request: Request, access: AccessDep) -> ResourceList:
    async with platform(request).db.transaction(access.scope) as conn:
        rows = (
            await conn.execute(
                text(
                    f"SELECT {_RESOURCE_COLUMNS} FROM cloudops.resource r"  # noqa: S608
                    " WHERE r.deleted_at IS NULL ORDER BY r.name, r.id LIMIT :limit"
                ),
                {"limit": MAX_LIST},
            )
        ).all()
        total = await conn.scalar(
            text("SELECT count(*) FROM cloudops.resource WHERE deleted_at IS NULL")
        )
    return ResourceList(
        items=[_resource(tuple(r), access) for r in rows],
        count=Metric[int](
            value=int(total or 0),
            unit="resources",
            source_system="platform_inventory",
            as_of=_now(),
            query_ref=query_ref("inventory.resource_count", 1, live=True),
            is_modelled=False,
        ),
    )


@router.get("/resources/{resource_id}", response_model=ResourceStaffView | ResourceView)
@requires("inventory.read")
async def get_resource(
    request: Request, resource_id: UUID, access: AccessDep
) -> ResourceStaffView | ResourceView:
    async with platform(request).db.transaction(access.scope) as conn:
        found = (
            await conn.execute(
                text(f"SELECT {_RESOURCE_COLUMNS} FROM cloudops.resource r WHERE r.id = :id"),  # noqa: S608
                {"id": resource_id},
            )
        ).one_or_none()
    if found is None:
        # Another tenant's resource and a resource that never existed look the
        # same here, because RLS made them the same (LLD isolation case 1).
        raise ApiError(404, "not_found")
    row: tuple[Any, ...] = tuple(found)
    audit_only(request, [row[1]])
    return _resource(row, access)


# Each figure is also converted, given ``rep``, at the daily average rate in
# force on its usage date (HLD, Multi-currency): NULL with no currency asked
# for or no rate in force, never a 1 or a 0.
_STAFF_COST: Final = text(
    "SELECT a.provider, c.currency, sum(c.amortized_cost), max(c.usage_date),"
    "   sum(c.amortized_cost * r.rate), bool_and(r.rate IS NOT NULL)"
    " FROM cloudops.cost_fact c"
    " JOIN cloudops.cloud_account a ON a.id = c.account_id"
    " LEFT JOIN LATERAL (SELECT CASE WHEN CAST(:rep AS text) IS NULL THEN NULL"
    "   WHEN c.currency = :rep THEN 1::numeric ELSE ("
    "     SELECT f.rate FROM cloudops.fx_rate f WHERE f.from_currency = c.currency"
    "       AND f.to_currency = :rep AND f.rate_type = 'average'"
    "       AND f.effective_date <= c.usage_date"
    "     ORDER BY f.effective_date DESC LIMIT 1) END AS rate) r ON true"
    " WHERE c.usage_date BETWEEN :start AND :end"
    " GROUP BY a.provider, c.currency ORDER BY a.provider, c.currency"
)
# A customer scope cannot read cost_fact's cost columns (migration 0015): the
# price comes from cloudops.customer_price alone (0014, ADR 0021).
_CUSTOMER_PRICE: Final = text(
    "SELECT a.provider, p.currency, sum(p.price), max(p.usage_date),"
    "   bool_and(p.price IS NOT NULL), sum(p.price * r.rate), bool_and(r.rate IS NOT NULL)"
    " FROM cloudops.customer_price(:start, :end) p"
    " JOIN cloudops.cloud_account a ON a.id = p.account_id"
    " LEFT JOIN LATERAL (SELECT CASE WHEN CAST(:rep AS text) IS NULL THEN NULL"
    "   WHEN p.currency = :rep THEN 1::numeric ELSE ("
    "     SELECT f.rate FROM cloudops.fx_rate f WHERE f.from_currency = p.currency"
    "       AND f.to_currency = :rep AND f.rate_type = 'average'"
    "       AND f.effective_date <= p.usage_date"
    "     ORDER BY f.effective_date DESC LIMIT 1) END AS rate) r ON true"
    " GROUP BY a.provider, p.currency ORDER BY a.provider, p.currency"
)


def customer_price(
    currency: str, total: Any, priced: bool, last_day: date, ref: str
) -> CustomerPrice:
    """One currency's price, or not captured when any day of it had no uplift."""
    if not priced:
        return CustomerPrice(currency=currency, price=None, not_captured="no_uplift")
    return CustomerPrice(
        currency=currency, not_captured=None,
        # In transaction currency, unconverted and unrounded: rounding happens
        # once, at presentation (CLAUDE.md rule 5).
        price=Metric[Decimal](value=Decimal(total), unit=currency, source_system=PRICE_SOURCE,
                              as_of=_end_of(last_day), query_ref=ref, is_modelled=False))


def _converted(value: Any, complete: bool, rep: str | None, source: str, as_of: datetime,
               ref: str) -> Metric[Decimal] | None:
    if rep is None or value is None or not complete:
        return None
    return Metric[Decimal](value=Decimal(value), unit=rep, source_system=source, as_of=as_of,
                           query_ref=ref, is_modelled=False)


@router.get("/cost/summary", response_model=CostSummary | PriceSummary)
@requires("cost.read")
async def cost_summary(
    request: Request,
    access: AccessDep,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
    currency: Currency = None,
) -> CostSummary | PriceSummary:
    """Cloud consumption by provider and transaction currency. Staff see the
    provider's charge to Getronics; a customer sees the price Getronics charges
    it (ADR 0021). Given ``currency``, each line is also converted at the rate
    in force on each usage date; a line with any day unconverted has none."""
    end = _now().date()
    start = end - timedelta(days=days - 1)
    params = {"start": start, "end": end, "rep": currency}
    customer = access.kind == "customer"
    async with platform(request).db.transaction(access.scope) as conn:
        rows: list[tuple[Any, ...]] = [
            tuple(r) for r in await conn.execute(_CUSTOMER_PRICE if customer else _STAFF_COST,
                                                 params)
        ]
    if customer:
        ref = query_ref("cost.customer_price_by_provider_currency", 1, start=start, end=end)
        cref = query_ref("cost.customer_price_converted", 1, start=start, end=end, rep=currency)
        lines: list[PriceLine] = []
        for provider, ccy, total, last_day, priced, conv, conv_ok in rows:
            base = customer_price(ccy, total, priced, last_day, ref)
            lines.append(PriceLine(
                provider=provider, currency=base.currency, price=base.price,
                not_captured=base.not_captured,
                converted=_converted(conv, priced and conv_ok, currency, PRICE_SOURCE,
                                     _end_of(last_day), cref)))
        return PriceSummary(period_start=start, period_end=end, reporting_currency=currency,
                            lines=lines)
    ref = query_ref("cost.amortized_by_provider_currency", 1, start=start, end=end)
    cref = query_ref("cost.amortized_converted", 1, start=start, end=end, rep=currency)
    return CostSummary(
        period_start=start,
        period_end=end,
        reporting_currency=currency,
        lines=[
            CostLine(
                provider=provider,
                currency=ccy,
                # In transaction currency, unconverted and unrounded: conversion
                # and rounding happen once, at presentation (CLAUDE.md rule 5).
                amortized=Metric[Decimal](
                    value=Decimal(total),
                    unit=ccy,
                    source_system=COST_SOURCE.get(provider, provider),
                    as_of=_end_of(last_day),
                    query_ref=ref,
                    is_modelled=False,
                ),
                converted=_converted(conv, conv_ok, currency,
                                     COST_SOURCE.get(provider, provider), _end_of(last_day),
                                     cref),
            )
            for provider, ccy, total, last_day, conv, conv_ok in rows
        ],
    )


@router.get("/commercial/sows", response_model=list[SowView])
@requires("commercial.read")
async def list_sows(request: Request, access: AccessDep) -> list[SowView]:
    # The matrix gate above is the first lock; commercial_visibility RLS is the
    # second, and holds even if the matrix were wrong (D10).
    async with platform(request).db.transaction(access.scope) as conn:
        sow_rows: list[tuple[Any, ...]] = [
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT id, reference, effective_from, effective_to, currency,"
                    " monthly_manage_revenue, created_at"
                    " FROM cloudops.sow ORDER BY effective_from, reference"
                )
            )
        ]
    return [
        SowView(
            reference=reference,
            effective_from=start,
            effective_to=end,
            currency=currency,
            monthly_manage_revenue=Metric[Decimal](
                value=Decimal(mrr),
                unit=currency,
                source_system="sow_register",
                as_of=created,
                query_ref=query_ref("commercial.sow", 1, sow=str(sow_id)),
                is_modelled=False,
            ),
        )
        for sow_id, reference, start, end, currency, mrr, created in sow_rows
    ]
