"""FinOps, margin and pricing endpoints (step 16; HLD M10-M12; ADR 0019).

Same rules as ``bff``: each endpoint declares its matrix permission, queries run
under the caller's scope so row-level security decides what exists, a customer
outside scope is a 404 exactly like one that never existed, and every number is
a ``Metric`` with its source and as-of time.

Two locks on commercial figures: ``commercial.read`` here, and the restrictive
``commercial_visibility`` policy in the database, which holds even if the matrix
were wrong. Cost (``cost.read``) is not commercial: reconciliation, allocation
and anomalies are cost data and follow the account restriction instead.

Currency (HLD, Multi-currency): amounts are returned in their transaction
currency, unrounded. Given ``currency``, each is also converted at the serving
layer: cost at the daily average rate in force on each usage date, margin at the
month's average rate, after it was computed in the contract currency, with FX
impact as its own line. A conversion with no rate in force is absent, never a 1
or a 0. Rounding happens once, in the console.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

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
from cloudops_commercial import finops, margin, pricing
from cloudops_commercial.ratecard import (
    InvalidRateCardError,
    RateCard,
    RateCardError,
    chain,
    from_document,
    rate_card_on,
)
from cloudops_tenancy import ScopedConnection

router = APIRouter(prefix="/v1", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

Period = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Currency = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
COST_SOURCE: Final = "billing_exports"
MARGIN_SOURCE: Final = "margin_engine"
RECON_SOURCE: Final = "reconciliation"
TREND_MONTHS: Final = 6
ANOMALY_DAYS: Final = 30
RATIO: Final = "ratio"
HOURS: Final = "hours"

NotCaptured = Literal["no_sow", "no_people_cost", "no_fx_rate", "no_cost_data", "not_computed"]
Verification = Literal["verified", "unverified"]


def _now() -> datetime:
    return datetime.now(UTC)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _month(period: str) -> tuple[date, date]:
    start = date.fromisoformat(f"{period}-01")
    nxt = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def _end_of(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)


def _money(value: Any, unit: str, source: str, as_of: datetime, ref: str,
           modelled: bool = False) -> Metric[Decimal]:
    return Metric[Decimal](value=Decimal(value), unit=unit, source_system=source, as_of=as_of,
                           query_ref=ref, is_modelled=modelled)


def _opt(value: Any, unit: str | None, source: str, as_of: datetime, ref: str,
         modelled: bool = False) -> Metric[Decimal] | None:
    if value is None or unit is None:
        return None
    return _money(value, unit, source, as_of, ref, modelled)


async def _tenants(conn: ScopedConnection) -> dict[UUID, str]:
    return {UUID(str(r[0])): str(r[1]) for r in _rows(await conn.execute(
        text("SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")))}


def _staff(access: Access) -> None:
    # The FinOps module is staff work; the database refuses a customer scope the
    # new cost tables anyway (no grant to cloudops_customer, ADR 0018).
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


def _in_scope(access: Access, tenant_id: UUID) -> None:
    if tenant_id not in access.scope.tenant_ids:
        raise ApiError(404, "not_found")


# --- FinOps views ---------------------------------------------------------------------------


class ChargeLine(ApiModel):
    charge_class: str
    feeds_margin: bool  # only consumption does (HLD D8)
    currency: str
    amortized: Metric[Decimal]
    unblended: Metric[Decimal]
    # In the reporting currency at each usage date's daily average rate. Absent
    # when a rate is missing for any day: never a partial sum.
    converted: Metric[Decimal] | None


class AccountCheck(ApiModel):
    account_id: UUID
    account_name: str
    kind: Literal["provider_total", "invoice"]
    status: Verification
    reason: str | None
    checked_on: date
    ledger: Metric[Decimal]
    reference: Metric[Decimal] | None
    drift: Metric[Decimal] | None


class Unallocated(ApiModel):
    dimension: Literal["application", "cost_centre"]
    currency: str
    allocated: Metric[Decimal]
    unallocated: Metric[Decimal]
    share: Metric[Decimal] | None


class CustomerCost(ApiModel):
    tenant: TenantView
    period: str
    verification: Verification  # every account verified, or not
    charges: list[ChargeLine]
    checks: list[AccountCheck]
    unallocated: list[Unallocated]
    anomalies: Metric[int]  # spikes and drops in the last 30 days


class FinOpsOverview(ApiModel):
    period: str
    reporting_currency: str | None
    customers: list[CustomerCost]


class AllocationLine(ApiModel):
    dimension: Literal["application", "cost_centre"]
    value: str | None  # None: unallocated
    currency: str
    amortized: Metric[Decimal]


class AnomalyView(ApiModel):
    account_id: UUID
    account_name: str
    granularity: Literal["day", "month"]
    observed_on: date
    direction: Literal["spike", "drop"]
    currency: str
    observed: Metric[Decimal]
    baseline: Metric[Decimal]
    ratio: Metric[Decimal] | None


class CustomerFinOps(ApiModel):
    cost: CustomerCost
    allocation: list[AllocationLine]
    anomalies: list[AnomalyView]


_CHARGES: Final = text(
    "SELECT c.tenant_id, c.charge_class::text, c.currency, sum(c.amortized_cost),"
    "   sum(c.unblended_cost), sum(c.amortized_cost * r.rate), bool_and(r.rate IS NOT NULL),"
    "   max(c.usage_date)"
    " FROM cloudops.cost_fact c"
    " LEFT JOIN LATERAL (SELECT CASE WHEN CAST(:rep AS text) IS NULL THEN NULL"
    "   WHEN c.currency = :rep THEN 1::numeric ELSE ("
    "     SELECT f.rate FROM cloudops.fx_rate f WHERE f.from_currency = c.currency"
    "       AND f.to_currency = :rep AND f.rate_type = 'average'"
    "       AND f.effective_date <= c.usage_date"
    "     ORDER BY f.effective_date DESC LIMIT 1) END AS rate) r ON true"
    " WHERE c.invoice_period = :period"
    "   AND (CAST(:tenant AS uuid) IS NULL OR c.tenant_id = CAST(:tenant AS uuid))"
    " GROUP BY c.tenant_id, c.charge_class, c.currency"
    " ORDER BY c.tenant_id, c.charge_class, c.currency"
)
# The check that decides what is shown: the invoice once there is one, else the
# latest provider total.
_CHECKS: Final = text(
    "SELECT DISTINCT ON (r.account_id) r.tenant_id, r.account_id, a.display_name, r.kind,"
    "   r.status, r.reason, r.checked_on, r.ledger_amount, r.ledger_currency,"
    "   r.reference_amount, r.reference_currency, r.drift, r.created_at"
    " FROM cloudops.cost_reconciliation r"
    " JOIN cloudops.cloud_account a ON a.id = r.account_id"
    " WHERE r.invoice_period = :period"
    "   AND (CAST(:tenant AS uuid) IS NULL OR r.tenant_id = CAST(:tenant AS uuid))"
    " ORDER BY r.account_id, (r.kind = 'invoice') DESC, r.checked_on DESC"
)
_ACCOUNTS_WITH_COST: Final = text(
    "SELECT DISTINCT c.tenant_id, c.account_id FROM cloudops.cost_fact c"
    " WHERE c.invoice_period = :period"
    "   AND (CAST(:tenant AS uuid) IS NULL OR c.tenant_id = CAST(:tenant AS uuid))"
)
_ALLOCATION: Final = text(
    "SELECT c.tenant_id, c.resource_id IS NOT NULL, c.tags_snapshot, r.tags, c.currency,"
    "   sum(c.amortized_cost)"
    " FROM cloudops.cost_fact c LEFT JOIN cloudops.resource r ON r.id = c.resource_id"
    " WHERE c.invoice_period = :period"
    "   AND (CAST(:tenant AS uuid) IS NULL OR c.tenant_id = CAST(:tenant AS uuid))"
    " GROUP BY c.tenant_id, c.resource_id IS NOT NULL, c.tags_snapshot, r.tags, c.currency"
)
_ANOMALY_COUNT: Final = text(
    "SELECT tenant_id, count(*) FROM cloudops.cost_anomaly WHERE observed_on >= :since"
    " GROUP BY tenant_id"
)
_ANOMALIES: Final = text(
    "SELECT n.account_id, a.display_name, n.granularity, n.observed_on, n.direction,"
    "   n.currency, n.observed, n.baseline, n.ratio, n.detected_at"
    " FROM cloudops.cost_anomaly n JOIN cloudops.cloud_account a ON a.id = n.account_id"
    " WHERE n.tenant_id = :tenant AND n.observed_on >= :since"
    " ORDER BY n.observed_on DESC, n.granularity, a.display_name LIMIT 200"
)


Alloc = tuple[finops.Allocation | None, Decimal]


def _allocations(rows: list[tuple[Any, ...]]) -> dict[UUID, dict[str, list[Alloc]]]:
    """tenant -> currency -> (allocation, amount); no resource is ``None``."""
    out: dict[UUID, dict[str, list[Alloc]]] = defaultdict(lambda: defaultdict(list))
    for tenant, linked, snapshot, tags, ccy, amount in rows:
        a = finops.allocate(snapshot, tags) if linked else None
        out[UUID(str(tenant))][str(ccy)].append((a, Decimal(amount)))
    return out


async def _customer_costs(conn: ScopedConnection, period: str, rep: str | None,
                          tenant: UUID | None) -> tuple[list[CustomerCost],
                                                        dict[UUID, dict[str, list[Alloc]]]]:
    names = await _tenants(conn)
    params = {"period": period, "rep": rep, "tenant": tenant}
    charges = _rows(await conn.execute(_CHARGES, params))
    checks = _rows(await conn.execute(_CHECKS, params))
    with_cost = _rows(await conn.execute(_ACCOUNTS_WITH_COST, params))
    alloc = _allocations(_rows(await conn.execute(_ALLOCATION, params)))
    since = _now().date() - timedelta(days=ANOMALY_DAYS)
    anomaly_counts = {UUID(str(r[0])): int(r[1])
                      for r in _rows(await conn.execute(_ANOMALY_COUNT, {"since": since}))}
    now = _now()
    ref = query_ref("finops.charges", 1, period=period, rep=rep)
    by_tenant: dict[UUID, list[ChargeLine]] = defaultdict(list)
    for t, cls, ccy, amortized, unblended, converted, complete, last in charges:
        as_of = _end_of(last)
        by_tenant[UUID(str(t))].append(ChargeLine(
            charge_class=cls, feeds_margin=cls in finops.FEEDS_MARGIN, currency=ccy,
            amortized=_money(amortized, ccy, COST_SOURCE, as_of, ref),
            unblended=_money(unblended, ccy, COST_SOURCE, as_of, ref),
            converted=(_money(converted, rep, COST_SOURCE, as_of, ref)
                       if rep and complete and converted is not None else None)))
    checks_by: dict[UUID, list[AccountCheck]] = defaultdict(list)
    checked_accounts: set[UUID] = set()
    rref = query_ref("finops.reconciliation", 1, period=period)
    for (t, acct, name, kind, status, reason, on, ledger, l_ccy, refamt, r_ccy, drift,
         created) in checks:
        checked_accounts.add(UUID(str(acct)))
        checks_by[UUID(str(t))].append(AccountCheck(
            account_id=acct, account_name=name, kind=kind, status=status, reason=reason,
            checked_on=on,
            ledger=_money(ledger, l_ccy or r_ccy or "XXX", COST_SOURCE, created, rref),
            reference=_opt(refamt, r_ccy, "provider_billing", created, rref),
            drift=_opt(drift, RATIO, RECON_SOURCE, created, rref)))
    unchecked = {UUID(str(t)) for t, a in with_cost if UUID(str(a)) not in checked_accounts}
    out: list[CustomerCost] = []
    aref = query_ref("finops.unallocated", 1, period=period)
    for tid in sorted(set(by_tenant) | set(checks_by), key=lambda t: names.get(t, "")):
        if tid not in names:
            continue
        acct_checks = checks_by.get(tid, [])
        verified = (bool(acct_checks) and tid not in unchecked
                    and all(c.status == "verified" for c in acct_checks))
        unalloc: list[Unallocated] = []
        for ccy, rows in sorted(alloc.get(tid, {}).items()):
            for dim in ("application", "cost_centre"):
                tot = finops.unallocated(rows, dim)
                share = tot.unallocated_share
                unalloc.append(Unallocated(
                    dimension=dim,
                    currency=ccy, allocated=_money(tot.allocated, ccy, COST_SOURCE, now, aref),
                    unallocated=_money(tot.unallocated, ccy, COST_SOURCE, now, aref),
                    share=_opt(share, RATIO, COST_SOURCE, now, aref)))
        out.append(CustomerCost(
            tenant=TenantView(id=tid, name=names[tid]), period=period,
            verification="verified" if verified else "unverified",
            charges=by_tenant.get(tid, []), checks=acct_checks, unallocated=unalloc,
            anomalies=Metric[int](value=anomaly_counts.get(tid, 0), unit="anomalies",
                                  source_system=COST_SOURCE, as_of=now,
                                  query_ref=query_ref("finops.anomalies", 1, days=ANOMALY_DAYS),
                                  is_modelled=False)))
    return out, alloc


@router.get("/finops/overview", response_model=FinOpsOverview)
@requires("cost.read")
async def finops_overview(
    request: Request, access: AccessDep, scope: ScopeDep, period: Period | None = None,
    currency: Currency = None,
) -> FinOpsOverview:
    """Every customer in scope for one invoice period: cost by charge class, the
    reconciliation verdict, unallocated spend and recent anomalies. Staff work:
    a customer reads its own cost through the portal (step 18)."""
    _staff(access)
    period = period or _now().strftime("%Y-%m")
    async with platform(request).db.transaction(scope) as conn:
        customers, _ = await _customer_costs(conn, period, currency, None)
    audit_only(request, [c.tenant.id for c in customers])
    return FinOpsOverview(period=period, reporting_currency=currency, customers=customers)


@router.get("/finops/customers/{tenant_id}", response_model=CustomerFinOps)
@requires("cost.read")
async def finops_customer(
    request: Request, tenant_id: UUID, access: AccessDep, period: Period | None = None,
    currency: Currency = None,
) -> CustomerFinOps:
    _staff(access)
    _in_scope(access, tenant_id)
    period = period or _now().strftime("%Y-%m")
    now = _now()
    async with platform(request).db.transaction(access.scope) as conn:
        names = await _tenants(conn)
        if tenant_id not in names:
            raise ApiError(404, "not_found")
        customers, alloc = await _customer_costs(conn, period, currency, tenant_id)
        since = now.date() - timedelta(days=ANOMALY_DAYS)
        found = _rows(await conn.execute(_ANOMALIES, {"tenant": tenant_id, "since": since}))
    audit_only(request, [tenant_id])
    cost = customers[0] if customers else CustomerCost(
        tenant=TenantView(id=tenant_id, name=names[tenant_id]), period=period,
        verification="unverified", charges=[], checks=[], unallocated=[],
        anomalies=Metric[int](value=len(found), unit="anomalies", source_system=COST_SOURCE,
                              as_of=now, query_ref=query_ref("finops.anomalies", 1),
                              is_modelled=False))
    aref = query_ref("finops.allocation", 1, period=period)
    lines: list[AllocationLine] = []
    for ccy, rows in sorted(alloc.get(tenant_id, {}).items()):
        for dim in ("application", "cost_centre"):
            sums: dict[str | None, Decimal] = defaultdict(Decimal)
            for a, amount in rows:
                sums[None if a is None else getattr(a, dim)] += amount
            for value, amount in sorted(sums.items(), key=lambda i: (i[0] is None, -i[1])):
                lines.append(AllocationLine(dimension=dim,
                                            value=value, currency=ccy,
                                            amortized=_money(amount, ccy, COST_SOURCE, now,
                                                             aref)))
    nref = query_ref("finops.anomaly_list", 1, days=ANOMALY_DAYS)
    anomalies = [AnomalyView(
        account_id=acct, account_name=name, granularity=g, observed_on=on, direction=d,
        currency=ccy, observed=_money(obs, ccy, COST_SOURCE, detected, nref),
        baseline=_money(base, ccy, COST_SOURCE, detected, nref, modelled=True),
        ratio=_opt(ratio, RATIO, COST_SOURCE, detected, nref))
        for acct, name, g, on, d, ccy, obs, base, ratio, detected in found]
    return CustomerFinOps(cost=cost, allocation=lines, anomalies=anomalies)


# --- FX corrections ---------------------------------------------------------------------------


class FxCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_currency: str = Field(pattern=r"^[A-Z]{3}$")
    to_currency: str = Field(pattern=r"^[A-Z]{3}$")
    rate_type: Literal["closing", "average"]
    effective_date: date
    rate: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    reason: str = Field(min_length=3, max_length=500)
    # EUR anchors every rate. For a pair without EUR, the currency whose EUR leg
    # moves (one of the pair); the other is held. For a pair with EUR, absent or
    # the other currency.
    moves: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class FxCorrectionView(ApiModel):
    id: str  # an entry identifier, not a figure
    from_currency: str
    to_currency: str
    rate_type: str
    effective_date: date
    previous_rate: Metric[Decimal] | None
    new_rate: Metric[Decimal]
    source: str
    reason: str
    entered_at: datetime


# The function's refusals (migrations 0012, 0016), by SQLSTATE. Anything else is a 500.
_REFUSALS: Final = {
    "42501": (403, "forbidden"),
    "22023": (422, "invalid_request"),
    "23514": (422, "invalid_request"),
    "FXSAM": (409, "unchanged"),
    "FXLEG": (422, "eur_leg_required"),
    "FXHLD": (422, "no_held_rate"),
}


@asynccontextmanager
async def _guarded(request: Request, access: Access) -> AsyncIterator[ScopedConnection]:
    try:
        async with platform(request).db.transaction(access.scope) as conn:
            yield conn
    except DBAPIError as exc:
        refusal = _REFUSALS.get(str(getattr(exc.orig, "sqlstate", "")))
        if refusal is None:
            raise
        raise ApiError(*refusal) from None


_FX_ENTRIES: Final = (
    "SELECT id, from_currency, to_currency, rate_type, effective_date, previous_rate,"
    " new_rate, source, reason, entered_at FROM cloudops.fx_restatement"
)


def _fx_view(row: tuple[Any, ...]) -> FxCorrectionView:
    rid, frm, to, kind, eff, prev, new, source, reason, at = row
    unit = f"{frm}->{to}"
    ref = query_ref("fx.restatement", 1, id=rid)
    return FxCorrectionView(
        id=str(rid), from_currency=frm, to_currency=to, rate_type=kind, effective_date=eff,
        previous_rate=_opt(prev, unit, source, at, ref), new_rate=_money(new, unit, source, at,
                                                                         ref),
        source=source, reason=reason, entered_at=at)


@router.get("/finops/fx-corrections", response_model=list[FxCorrectionView])
@requires("cost.read")
async def list_fx_corrections(request: Request, access: AccessDep) -> list[FxCorrectionView]:
    """Every correction and source revision of a rate: the explicit entries."""
    if access.kind != "staff":
        raise ApiError(403, "forbidden")
    async with platform(request).db.transaction(access.scope) as conn:
        rows = _rows(await conn.execute(text(
            f"{_FX_ENTRIES} ORDER BY entered_at DESC, id DESC LIMIT 200")))
    return [_fx_view(r) for r in rows]


@router.post("/finops/fx-corrections", response_model=FxCorrectionView, status_code=201)
@requires("fx.manage")
async def correct_fx_rate(
    request: Request, body: FxCorrectionRequest, access: AccessDep
) -> FxCorrectionView:
    """Finance corrects a rate. Its inverse and every pair with the moving
    currency are re-derived from the EUR rates on the same day, each with its own
    entry; the returned entry is the first that moved. The closed months it
    touches are restated by the next month-end reprocessing, as new versions;
    nothing closed is overwritten."""
    if access.kind != "staff":
        raise ApiError(403, "forbidden")
    if body.from_currency == body.to_currency:
        raise ApiError(422, "invalid_request")
    async with _guarded(request, access) as conn:
        entry = await conn.scalar(text(
            "SELECT access.correct_fx_rate(:actor, :f, :t, :k, :d, :r, :why,"
            " CAST(:moves AS char(3)))"), {
            "actor": access.identity.oid, "f": body.from_currency, "t": body.to_currency,
            "k": body.rate_type, "d": body.effective_date, "r": body.rate,
            "why": body.reason, "moves": body.moves})
        row = _rows(await conn.execute(text(f"{_FX_ENTRIES} WHERE id = :id"),
                                       {"id": entry}))
    return _fx_view(row[0])


# --- margin -----------------------------------------------------------------------------------


class Reported(ApiModel):
    """The contract-currency margin converted for the viewer, FX impact apart."""

    currency: str
    margin: Metric[Decimal]
    margin_at_prior_rates: Metric[Decimal]
    fx_impact: Metric[Decimal]


class Effort(ApiModel):
    hours: Metric[Decimal]
    modelled_hours: Metric[Decimal] | None
    items: Metric[int]
    items_without_hours: Metric[int]
    complete: bool


class TrendPoint(ApiModel):
    period: str
    status: Literal["provisional", "closed"]
    margin_pct: Metric[Decimal] | None
    band: str | None


class MarginView(ApiModel):
    tenant: TenantView
    period: str
    not_captured: NotCaptured | None
    status: Literal["provisional", "closed"] | None
    through: date | None
    version: Metric[int] | None
    restated: bool
    rate_card_version: str | None
    contract_currency: str | None
    revenue: Metric[Decimal] | None
    modelled_cost: Metric[Decimal] | None
    actual_cost: Metric[Decimal] | None
    modelled_margin: Metric[Decimal] | None
    actual_margin: Metric[Decimal] | None
    margin_pct: Metric[Decimal] | None
    band: str | None  # None while provisional: the band arrives at close
    band_basis: Literal["actual", "modelled"] | None
    target_margin: Metric[Decimal] | None
    gap_to_target: Metric[Decimal] | None
    # Did the cost under the figure reconcile? None: no cost to verify.
    cost_verified: bool | None
    effort: Effort | None
    unpriced_cis: Metric[int] | None
    exceeds_slab: bool
    reported: Reported | None
    trend: list[TrendPoint]


class MarginOverview(ApiModel):
    period: str
    reporting_currency: str | None
    customers: list[MarginView]


class VarianceView(ApiModel):
    workgroup: str
    activity: str
    hours: Metric[Decimal]
    items: Metric[int]
    items_without_hours: Metric[int]
    share: Metric[Decimal]
    excess_hours: Metric[Decimal]
    excess_cost: Metric[Decimal]


class VersionView(ApiModel):
    version: Metric[int]
    status: Literal["provisional", "closed"]
    computed_at: datetime
    restates_version: Metric[int] | None
    margin_pct: Metric[Decimal] | None


class MarginDetail(ApiModel):
    margin: MarginView
    variance: list[VarianceView]
    versions: list[VersionView]


_SNAPSHOT_COLUMNS: Final = (
    "s.id, s.tenant_id, s.period, s.through, s.version, s.status, s.restates, s.computed_at,"
    " s.rate_card_version, s.not_captured, s.contract_currency, s.revenue, s.modelled_cost,"
    " s.actual_cost, s.modelled_margin, s.actual_margin, s.margin_at_prior_rates,"
    " s.margin_pct, s.band, s.band_basis, s.target_margin, s.gap_to_target,"
    " s.effort_hours, s.modelled_hours, s.effort_items, s.effort_items_without_hours,"
    " s.effort_complete, s.unpriced_cis, s.exceeds_slab, s.cost_verified"
)
_LATEST: Final = text(
    f"SELECT DISTINCT ON (s.tenant_id, s.period) {_SNAPSHOT_COLUMNS}"  # noqa: S608
    " FROM cloudops.margin_snapshot s WHERE s.period BETWEEN :first AND :last"
    "   AND (CAST(:tenant AS uuid) IS NULL OR s.tenant_id = CAST(:tenant AS uuid))"
    " ORDER BY s.tenant_id, s.period, s.version DESC"
)
_MONTH_AVG: Final = text(
    "SELECT avg(rate) FROM cloudops.fx_rate WHERE from_currency = :f AND to_currency = :t"
    " AND rate_type = 'average' AND effective_date BETWEEN :a AND :b"
)


async def _month_avg(conn: ScopedConnection, frm: str, to: str, first: date,
                     last: date) -> Decimal | None:
    if frm == to:
        return Decimal(1)
    value = await conn.scalar(_MONTH_AVG, {"f": frm, "t": to, "a": first, "b": last})
    return None if value is None else Decimal(value)


def _empty(tenant: TenantView, period: str, reason: NotCaptured,
           trend: list[TrendPoint]) -> MarginView:
    return MarginView(
        tenant=tenant, period=period, not_captured=reason, status=None, through=None,
        version=None, restated=False, rate_card_version=None, contract_currency=None,
        revenue=None, modelled_cost=None, actual_cost=None, modelled_margin=None,
        actual_margin=None, margin_pct=None, band=None, band_basis=None, target_margin=None,
        gap_to_target=None, cost_verified=None, effort=None, unpriced_cis=None,
        exceeds_slab=False, reported=None,
        trend=trend)


async def _margin_view(conn: ScopedConnection, tenant: TenantView, period: str,
                       row: tuple[Any, ...] | None, rep: str | None,
                       trend: list[TrendPoint]) -> MarginView:
    if row is None:
        return _empty(tenant, period, "not_computed", trend)
    (sid, _t, p, through, version, status, restates, computed, card, not_captured, ccy,
     revenue, modelled, actual, m_margin, a_margin, at_prior, pct, band, basis, target, gap,
     hours, m_hours, items, missing, complete, unpriced, exceeds, verified) = row
    ref = query_ref("commercial.margin", 1, snapshot=str(sid))
    as_of = computed
    modelled_flag = basis == "modelled"

    def money(v: Any, modelled: bool = False) -> Metric[Decimal] | None:
        return _opt(v, ccy, MARGIN_SOURCE, as_of, ref, modelled)

    def count(v: int, unit: str) -> Metric[int]:
        return Metric[int](value=int(v), unit=unit, source_system=MARGIN_SOURCE, as_of=as_of,
                           query_ref=ref, is_modelled=False)

    reported = None
    basis_margin = a_margin if basis == "actual" else m_margin
    if rep and ccy and basis_margin is not None and at_prior is not None:
        first = p
        prior = _add_months(p, -1)
        now_rate = await _month_avg(conn, ccy, rep, first, through)
        prior_rate = await _month_avg(conn, ccy, rep, prior, first - timedelta(days=1))
        if now_rate is not None and prior_rate is not None:
            r = margin.report(Decimal(basis_margin), Decimal(at_prior), now_rate, prior_rate)
            rref = query_ref("commercial.margin_reported", 1, snapshot=str(sid), rep=rep)
            reported = Reported(
                currency=rep,
                margin=_money(r.margin, rep, MARGIN_SOURCE, as_of, rref, modelled_flag),
                margin_at_prior_rates=_money(r.margin_at_prior_rates, rep, MARGIN_SOURCE, as_of,
                                             rref, modelled_flag),
                fx_impact=_money(r.fx_impact, rep, MARGIN_SOURCE, as_of, rref, modelled_flag))
    return MarginView(
        tenant=tenant, period=period, not_captured=not_captured, status=status,
        through=through, version=count(version, "version"), restated=restates is not None,
        rate_card_version=card, contract_currency=ccy, revenue=money(revenue),
        modelled_cost=money(modelled, True), actual_cost=money(actual),
        modelled_margin=money(m_margin, True), actual_margin=money(a_margin),
        margin_pct=_opt(pct, RATIO, MARGIN_SOURCE, as_of, ref, modelled_flag), band=band,
        band_basis=basis, target_margin=_opt(target, RATIO, "sow_register", as_of, ref),
        gap_to_target=_opt(gap, RATIO, MARGIN_SOURCE, as_of, ref, modelled_flag),
        cost_verified=verified,
        effort=Effort(
            hours=_money(hours, HOURS, "servicenow", as_of, ref),
            modelled_hours=_opt(m_hours, HOURS, MARGIN_SOURCE, as_of, ref, True),
            items=count(items, "work items"), items_without_hours=count(missing, "work items"),
            complete=complete),
        unpriced_cis=count(unpriced, "CIs"), exceeds_slab=exceeds, reported=reported,
        trend=trend)


def _trend(rows: list[tuple[Any, ...]]) -> dict[UUID, list[TrendPoint]]:
    out: dict[UUID, list[TrendPoint]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: r[2]):
        sid, tenant, p, computed, status, pct, band = r[0], r[1], r[2], r[7], r[5], r[17], r[18]
        out[UUID(str(tenant))].append(TrendPoint(
            period=p.strftime("%Y-%m"), status=status,
            margin_pct=_opt(pct, RATIO, MARGIN_SOURCE, computed,
                            query_ref("commercial.margin", 1, snapshot=str(sid))),
            band=band))
    return out


async def _margins(conn: ScopedConnection, period: str, rep: str | None,
                   tenant: UUID | None) -> list[MarginView]:
    start, _ = _month(period)
    rows = _rows(await conn.execute(_LATEST, {
        "first": _add_months(start, -(TREND_MONTHS - 1)), "last": start, "tenant": tenant}))
    trend = _trend(rows)
    current = {UUID(str(r[1])): r for r in rows if r[2] == start}
    names = await _tenants(conn)
    out: list[MarginView] = []
    for tid, name in names.items():
        if tenant is not None and tid != tenant:
            continue
        out.append(await _margin_view(conn, TenantView(id=tid, name=name), period,
                                      current.get(tid), rep, trend.get(tid, [])))
    return out


def _default_margin_period() -> str:
    # The latest month that has normally closed (closing runs from the 5th).
    today = _now().date()
    return _add_months(today.replace(day=1), -1 if today.day >= 5 else -2).strftime("%Y-%m")


@router.get("/commercial/margin", response_model=MarginOverview)
@requires("commercial.read")
async def margin_overview(
    request: Request, access: AccessDep, scope: ScopeDep, period: Period | None = None,
    currency: Currency = None,
) -> MarginOverview:
    """Every customer in scope for one month: modelled and actual margin in the
    contract currency, the band, the gap to target and the trend. A customer
    without a SOW is listed as not captured, never as zero."""
    period = period or _default_margin_period()
    async with platform(request).db.transaction(scope) as conn:
        customers = await _margins(conn, period, currency, None)
    return MarginOverview(period=period, reporting_currency=currency, customers=customers)


@router.get("/commercial/margin/{tenant_id}", response_model=MarginDetail)
@requires("commercial.read")
async def margin_detail(
    request: Request, tenant_id: UUID, access: AccessDep, period: Period | None = None,
    currency: Currency = None,
) -> MarginDetail:
    """One customer-month: variance by workgroup and activity type, and every
    version of the figure (restatements are versions, never overwrites)."""
    _in_scope(access, tenant_id)
    period = period or _default_margin_period()
    start, _ = _month(period)
    async with platform(request).db.transaction(access.scope) as conn:
        views = await _margins(conn, period, currency, tenant_id)
        if not views:
            raise ApiError(404, "not_found")
        versions = _rows(await conn.execute(text(
            "SELECT s.id, s.version, s.status, s.computed_at, r.version, s.margin_pct"
            " FROM cloudops.margin_snapshot s"
            " LEFT JOIN cloudops.margin_snapshot r ON r.id = s.restates"
            " WHERE s.tenant_id = :t AND s.period = :p ORDER BY s.version"),
            {"t": tenant_id, "p": start}))
        variance: list[tuple[Any, ...]] = []
        if versions:
            variance = _rows(await conn.execute(text(
                "SELECT v.workgroup, v.activity, v.hours, v.items, v.items_without_hours,"
                " v.share, v.excess_hours, v.excess_cost, v.currency"
                " FROM cloudops.margin_variance v WHERE v.snapshot_id = :s"
                " ORDER BY v.hours DESC, v.workgroup, v.activity"), {"s": versions[-1][0]}))
    audit_only(request, [tenant_id])
    as_of = versions[-1][3] if versions else _now()
    ref = query_ref("commercial.variance", 1, tenant=str(tenant_id), period=period)

    def cnt(v: int) -> Metric[int]:
        return Metric[int](value=int(v), unit="work items", source_system="servicenow",
                           as_of=as_of, query_ref=ref, is_modelled=False)

    return MarginDetail(
        margin=views[0],
        variance=[VarianceView(
            workgroup=wg, activity=act, hours=_money(h, HOURS, "servicenow", as_of, ref),
            items=cnt(i), items_without_hours=cnt(m),
            share=_money(share, RATIO, MARGIN_SOURCE, as_of, ref),
            excess_hours=_money(xh, HOURS, MARGIN_SOURCE, as_of, ref, True),
            excess_cost=_money(xc, ccy, MARGIN_SOURCE, as_of, ref, True))
            for wg, act, h, i, m, share, xh, xc, ccy in variance],
        versions=[VersionView(
            version=_version(v, at, sid), status=st, computed_at=at,
            restates_version=None if rv is None else _version(rv, at, sid),
            margin_pct=_opt(pct, RATIO, MARGIN_SOURCE, at, query_ref(
                "commercial.margin", 1, snapshot=str(sid))))
            for sid, v, st, at, rv, pct in versions])


def _version(v: int, at: datetime, sid: Any) -> Metric[int]:
    return Metric[int](value=int(v), unit="version", source_system=MARGIN_SOURCE, as_of=at,
                       query_ref=query_ref("commercial.margin", 1, snapshot=str(sid)),
                       is_modelled=False)


# --- pricing ----------------------------------------------------------------------------------


class RateCardView(ApiModel):
    version: str
    effective_from: date
    effective_to: date | None
    currency: str
    source: str
    resource_types: list[str]
    coverages: list[str]
    support_models: list[str]
    locations: list[str]


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inventory: dict[str, Annotated[int, Field(ge=0, le=1_000_000)]] = Field(max_length=64)
    monthly_consumption: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    coverage: Literal["8x5", "16x5", "24x7"] = "24x7"
    support_model: Literal["Bronze", "Silver", "Gold"] = "Gold"
    location: Literal["India", "Budapest"] = "India"
    corestack: bool = True
    on: date | None = None  # the rate card in force on this date; today by default


class TeamLine(ApiModel):
    role: str
    fte: Metric[Decimal]


class QuoteView(ApiModel):
    rate_card_version: str
    currency: str
    critical_cis: Metric[int]
    slab: str
    service_cost: Metric[Decimal]
    base_pack: Metric[Decimal]
    cloud_services_cost: Metric[Decimal]
    platform_cost: Metric[Decimal]
    total_monthly: Metric[Decimal]
    price_per_ci: Metric[Decimal]
    fte: Metric[Decimal]
    team: list[TeamLine]
    transition_weeks: Metric[int]
    next_slab: str | None
    cis_until_next_slab: Metric[int] | None
    repricing_due: bool


_RATE_CARDS: Final = text("SELECT document FROM cloudops.rate_card_documents()")


async def _rate_card_on(request: Request, access: Access, day: date) -> RateCard:
    """The version in force on ``day``, read under the caller's scope: the
    commercial policy decides whether any row exists (ADR 0033)."""
    async with platform(request).db.transaction(access.scope) as conn:
        cards = chain(r[0] for r in await conn.execute(_RATE_CARDS))
    return rate_card_on(day, cards)


@router.get("/commercial/rate-card", response_model=RateCardView)
@requires("commercial.read")
async def rate_card_view(request: Request, access: AccessDep) -> RateCardView:
    if access.kind != "staff":
        raise ApiError(403, "forbidden")
    try:
        card = await _rate_card_on(request, access, _now().date())
    except RateCardError:
        raise ApiError(404, "not_found") from None
    return RateCardView(
        version=card.version, effective_from=card.effective_from,
        effective_to=card.effective_to, currency=card.currency, source=card.source,
        resource_types=sorted(card.resource_complexity),
        coverages=sorted({c for c, _ in card.service_multiplier}),
        support_models=sorted({s for _, s in card.service_multiplier}),
        locations=sorted(card.people_cost))


# --- rate card versions (ADR 0033) -------------------------------------------------------------

# Every number is a string: a JSON number would be a float (CLAUDE.md rule 5), a
# string is never coerced from one, and the API serves no bare number.
Dec = Annotated[str, Field(pattern=r"^\d{1,12}(\.\d{1,16})?$")]
Whole = Annotated[str, Field(pattern=r"^\d{1,10}$")]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Coverage = Literal["8x5", "16x5", "24x7"]
Support = Literal["Bronze", "Silver", "Gold"]


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComplexityRow(_Row):
    complexity: str = Field(pattern=r"^([0-9]{1,3}|Case)$")
    minutes: Dec


class MultiplierRow(_Row):
    coverage: Coverage
    support: Support
    multiplier: Dec


class CatalogueRow(_Row):
    resource_type: Name
    complexity: str = Field(pattern=r"^([0-9]{1,3}|Case)$")


class TypeMapRow(_Row):
    type_canonical: Name
    resource_type: Name


class BaseBandRow(_Row):
    lower: Dec
    upper: Dec | None
    rate: Dec


class CoverageUpliftRow(_Row):
    coverage: Coverage
    uplift: Dec


class SlabRow(_Row):
    label: Name
    lower: Whole
    upper: Whole
    transition_weeks: Whole


class ToolRow(_Row):
    tool: Name
    cost_per_ci: Dec
    default: bool


class PeopleCostRow(_Row):
    location: Literal["India", "Budapest"]
    monthly_cost: Dec


class RoleRow(_Row):
    role: Name
    share: Dec


class HealthBandRow(_Row):
    below: Annotated[str, Field(pattern=r"^-?0?\.\d{1,6}$|^0$")]
    label: Annotated[str, Field(min_length=1, max_length=40)]


class RateCardDocument(_Row):
    """A whole rate card version, as the database stores it and the engine reads it
    (``cloudops_commercial.ratecard.from_document``)."""

    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
    effective_from: date
    source: Name
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    blended_day_rate: Dec
    minutes_per_day: Dec
    working_days_per_month: Dec
    hours_per_day: Dec
    base_pack_floor: Dec
    corestack_consumption_rate: Dec
    repricing_headroom_minimum: Whole
    repricing_headroom_share: Dec
    healthy_label: Annotated[str, Field(min_length=1, max_length=40)]
    complexity_minutes: list[ComplexityRow] = Field(min_length=1, max_length=50)
    service_multipliers: list[MultiplierRow] = Field(min_length=9, max_length=9)
    catalogue: list[CatalogueRow] = Field(min_length=1, max_length=500)
    type_map: list[TypeMapRow] = Field(max_length=500)
    base_pack_bands: list[BaseBandRow] = Field(min_length=1, max_length=50)
    base_pack_coverage_uplift: list[CoverageUpliftRow] = Field(min_length=3, max_length=3)
    ci_slabs: list[SlabRow] = Field(min_length=1, max_length=50)
    tools: list[ToolRow] = Field(min_length=1, max_length=50)
    people_cost: list[PeopleCostRow] = Field(min_length=1, max_length=2)
    roles: list[RoleRow] = Field(min_length=1, max_length=50)
    health_bands: list[HealthBandRow] = Field(min_length=1, max_length=10)


class RateCardVersionView(ApiModel):
    id: str  # a version identifier, not a figure
    version: str
    effective_from: date
    effective_to: date | None  # exclusive: where the next version begins
    in_force: bool
    published_at: datetime
    published_by: str
    reason: str
    document: RateCardDocument


class PublishRateCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: RateCardDocument
    reason: str = Field(min_length=3, max_length=500)


_DOCUMENTS: Final = text(
    "SELECT id, document, published_by, published_at, reason"
    " FROM cloudops.rate_card_documents()")

# The publish function's refusals (migration 0031), by SQLSTATE.
_RATE_CARD_REFUSALS: Final = {
    **_REFUSALS,
    "RCDAT": (422, "effective_date_not_future"),
    "RCVER": (409, "version_exists"),
    "23505": (409, "version_exists"),
}


def _versions(rows: list[tuple[Any, ...]]) -> list[RateCardVersionView]:
    cards = {c.version: c for c in chain(r[1] for r in rows)}
    today = _now().date()
    out = []
    for rid, doc, by, at, reason in rows:
        card = cards[doc["version"]]
        out.append(RateCardVersionView(
            id=str(rid), version=card.version, effective_from=card.effective_from,
            effective_to=card.effective_to, in_force=card.in_force_on(today),
            published_at=at, published_by=str(by), reason=str(reason),
            document=RateCardDocument.model_validate(doc)))
    return sorted(out, key=lambda v: v.effective_from, reverse=True)


@router.get("/commercial/rate-cards", response_model=list[RateCardVersionView])
@requires("commercial.read")
async def list_rate_cards(request: Request, access: AccessDep) -> list[RateCardVersionView]:
    """Every published version, newest first, each whole: history is never edited."""
    if access.kind != "staff":
        raise ApiError(403, "forbidden")
    async with platform(request).db.transaction(access.scope) as conn:
        rows = _rows(await conn.execute(_DOCUMENTS))
    return _versions(rows)


@router.post("/commercial/rate-cards", response_model=RateCardVersionView, status_code=201)
@requires("ratecard.publish")
async def publish_rate_card(
    request: Request, body: PublishRateCard, access: AccessDep
) -> RateCardVersionView:
    """Publish a new version, effective from a future date. It is a new row; the
    version before it ends where this one begins and is otherwise untouched."""
    if access.kind != "staff":
        raise ApiError(403, "forbidden")
    doc = body.document.model_dump(mode="json")
    try:
        from_document(doc)  # the engine's own checks: can this card price every case?
    except InvalidRateCardError:
        raise ApiError(422, "invalid_rate_card") from None
    try:
        async with platform(request).db.transaction(access.scope) as conn:
            new_id = await conn.scalar(text(
                "SELECT access.publish_rate_card(:actor, CAST(:doc AS jsonb), :why)"), {
                "actor": access.identity.oid, "doc": json.dumps(doc), "why": body.reason})
            rows = _rows(await conn.execute(_DOCUMENTS))
    except DBAPIError as exc:
        refusal = _RATE_CARD_REFUSALS.get(str(getattr(exc.orig, "sqlstate", "")))
        if refusal is None:
            raise
        raise ApiError(*refusal) from None
    return next(v for v in _versions(rows) if v.id == str(new_id))


@router.post("/commercial/quotes", response_model=QuoteView)
@requires("commercial.read")
async def quote(request: Request, body: QuoteRequest, access: AccessDep) -> QuoteView:
    """The pricing engine on the rate card in force. Computes; stores nothing."""
    if access.kind != "staff":
        raise ApiError(403, "forbidden")
    try:
        card = await _rate_card_on(request, access, body.on or _now().date())
    except RateCardError:
        raise ApiError(422, "no_rate_card") from None
    if body.currency != card.currency:
        # The workbook prices in its own currency; converting a forward quote is
        # a commercial decision, not this endpoint's.
        raise ApiError(422, "currency_not_supported")
    unknown = set(body.inventory) - set(card.resource_complexity)
    if unknown or not any(body.inventory.values()):
        raise ApiError(422, "invalid_request")
    q = pricing.quote(pricing.Engagement(
        customer="", inventory=dict(body.inventory),
        monthly_consumption_eur=body.monthly_consumption, coverage=body.coverage,
        support_model=body.support_model, location=body.location, tools=card.default_tools,
        corestack=body.corestack), card)
    head = pricing.slab_headroom(q.critical_cis, card)
    now = _now()
    ref = query_ref("pricing.quote", 1, card=card.version, body=body.model_dump(mode="json"))
    ccy = card.currency

    def m(v: Decimal, unit: str = ccy) -> Metric[Decimal]:
        return _money(v, unit, f"rate_card:{card.version}", now, ref, modelled=True)

    def n(v: int, unit: str) -> Metric[int]:
        return Metric[int](value=v, unit=unit, source_system=f"rate_card:{card.version}",
                           as_of=now, query_ref=ref, is_modelled=True)

    return QuoteView(
        rate_card_version=card.version, currency=ccy, critical_cis=n(q.critical_cis, "CIs"),
        slab=q.slab, service_cost=m(q.service_cost_eur), base_pack=m(q.base_pack_eur),
        cloud_services_cost=m(q.cloud_services_cost_eur),
        platform_cost=m(q.platform_cost_eur), total_monthly=m(q.total_monthly_eur),
        price_per_ci=m(q.price_per_ci_eur), fte=m(q.fte_count, "FTE"),
        team=[TeamLine(role=r, fte=m(v, "FTE")) for r, v in q.team.items()],
        transition_weeks=n(q.transition_weeks, "weeks"), next_slab=head.next_slab,
        cis_until_next_slab=(None if head.cis_until_next_slab is None
                             else n(head.cis_until_next_slab, "CIs")),
        repricing_due=head.repricing_due)

