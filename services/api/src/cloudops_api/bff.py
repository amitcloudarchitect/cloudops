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
from typing import Annotated, Any, Final
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
    provider: str
    currency: str
    amortized: Metric[Decimal]


class CostSummary(ApiModel):
    period_start: date
    period_end: date
    lines: list[CostLine]


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


@router.get("/cost/summary", response_model=CostSummary)
@requires("cost.read")
async def cost_summary(
    request: Request,
    access: AccessDep,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> CostSummary:
    end = _now().date()
    start = end - timedelta(days=days - 1)
    async with platform(request).db.transaction(access.scope) as conn:
        cost_rows: list[tuple[Any, ...]] = [
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT a.provider, c.currency, sum(c.amortized_cost), max(c.usage_date)"
                    " FROM cloudops.cost_fact c"
                    " JOIN cloudops.cloud_account a ON a.id = c.account_id"
                    " WHERE c.usage_date BETWEEN :start AND :end"
                    " GROUP BY a.provider, c.currency ORDER BY a.provider, c.currency"
                ),
                {"start": start, "end": end},
            )
        ]
    ref = query_ref("cost.amortized_by_provider_currency", 1, start=start, end=end)
    return CostSummary(
        period_start=start,
        period_end=end,
        lines=[
            CostLine(
                provider=provider,
                currency=currency,
                # In transaction currency, unconverted and unrounded: conversion
                # and rounding happen once, at presentation (CLAUDE.md rule 5).
                amortized=Metric[Decimal](
                    value=Decimal(total),
                    unit=currency,
                    source_system=COST_SOURCE.get(provider, provider),
                    as_of=_end_of(last_day),
                    query_ref=ref,
                    is_modelled=False,
                ),
            )
            for provider, currency, total, last_day in cost_rows
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
