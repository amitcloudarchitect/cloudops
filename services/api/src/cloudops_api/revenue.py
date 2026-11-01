"""Revenue & margin per customer over a range of months (ADR 0038): the monthly
report workbook's sheet, computed by ``cloudops_commercial.revenue``.

Same rules as ``commercial``: ``commercial.read``, staff only, queries under the
caller's scope (the console's customer selector narrows it, ``ScopeDep``), so
row-level security decides which customers exist. Customer roles never reach it:
the matrix refuses them, ``commercial_visibility`` returns them no SOW or margin
row, and ``cloudops_customer`` holds no grant on either table (ADR 0002 D10).

Amounts are monthly averages over the range (consumption, manage revenue, cost
and margin) or contract values (professional services, ACVs), unrounded, each
in the row's contract currency and, given ``currency``, also converted at the
rate in force on its transaction date. A derived ACV is ``is_modelled``. A figure
that cannot be computed is absent with its gap named, never zero.

``/v1/commercial/revenue.csv`` is the same view as printed: the reporting
currency where one is asked, else each contract's own, rounded once (money to
two places, percentages to one) because a CSV is presentation.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
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
from cloudops_commercial import revenue
from cloudops_tenancy import ScopedConnection, TenantScope

router = APIRouter(prefix="/v1", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

MONTH: Final = r"^\d{4}-(0[1-9]|1[0-2])$"
Month = Annotated[str | None, Query(pattern=MONTH)]
Currency = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
MAX_MONTHS: Final = 24
RATIO: Final = "ratio"
# Rates published up to this long before a transaction date still count as in force:
# weekends, holidays, and a SOW that took effect before the range.
RATE_LOOKBACK: Final = timedelta(days=31)


def _now() -> datetime:
    return datetime.now(UTC)


def _first(period: str) -> date:
    return date.fromisoformat(f"{period}-01")


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def _months(first: date, last: date) -> int:
    return (last.year - first.year) * 12 + last.month - first.month + 1


def _default_period() -> str:
    # The latest month that has normally closed (margin closes from the 5th).
    today = _now().date()
    return _add_months(today.replace(day=1), -1 if today.day >= 5 else -2).strftime("%Y-%m")


# --- response -----------------------------------------------------------------------------------


class RevenueFigures(ApiModel):
    """One customer's figures in one currency. Monthly averages over the range:
    consumption, revenue, cost and margin. Contract values: the rest. ``None``:
    not captured, with the gap in ``gaps``."""

    currency: str | None
    cloud_consumption: Metric[Decimal] | None
    manage_revenue: Metric[Decimal] | None
    manage_cost: Metric[Decimal] | None  # actual; is_modelled where effort was incomplete
    gross_margin: Metric[Decimal] | None
    gross_margin_pct: Metric[Decimal] | None  # a ratio, in the contract currency
    professional_services: Metric[Decimal] | None
    hosting_acv: Metric[Decimal] | None  # is_modelled: derived, not entered on the SOW
    manage_acv: Metric[Decimal] | None


class FigureGap(ApiModel):
    figure: str  # a RevenueFigures field, "reported.<field>", or "row"
    reason: str  # cloudops_commercial.revenue.Gap


class RevenueBlock(ApiModel):
    contract: RevenueFigures
    reported: RevenueFigures | None  # in reporting_currency, when one was asked
    gaps: list[FigureGap]


class RevenueChange(ApiModel):
    """Current minus compare; the margin percentage's change is in ratio points."""

    contract: RevenueFigures | None  # None: the contract currency changed between ranges
    reported: RevenueFigures | None


class RevenueRow(ApiModel):
    tenant: TenantView
    csp: bool | None  # None: not recorded on the SOW
    sow_reference: str | None
    contract_currency: str | None
    current: RevenueBlock
    compare: RevenueBlock | None
    change: RevenueChange | None


class MissingCount(ApiModel):
    figure: str
    customers: Metric[int]


class RevenueTotal(ApiModel):
    currency: str
    customers: Metric[int]
    figures: RevenueFigures
    missing: list[MissingCount]  # customers left out of each figure's sum


class RevenueTotals(ApiModel):
    contract: list[RevenueTotal]  # one per contract currency
    reported: RevenueTotal | None


class RevenueTotalsChange(ApiModel):
    contract: list[RevenueFigures]  # per contract currency present in both ranges
    reported: RevenueFigures | None


class RevenueRange(ApiModel):
    first: str  # YYYY-MM
    last: str
    months: Metric[int]


class RevenueOverview(ApiModel):
    range: RevenueRange
    compare_range: RevenueRange | None
    reporting_currency: str | None
    rows: list[RevenueRow]
    totals: RevenueTotals
    compare_totals: RevenueTotals | None
    totals_change: RevenueTotalsChange | None


# --- reading -----------------------------------------------------------------------------------


_TENANTS: Final = text("SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")
# The version in force at the end of the range, else the latest that overlaps it.
_SOWS: Final = text(
    "SELECT DISTINCT ON (tenant_id) tenant_id, reference, currency, effective_from, csp,"
    "   monthly_manage_revenue, hosting_acv, manage_acv, professional_services,"
    "   consumption_uplift, created_at"
    " FROM cloudops.sow WHERE effective_from <= :last"
    "   AND (effective_to IS NULL OR effective_to >= :first)"
    " ORDER BY tenant_id, effective_from DESC, created_at DESC"
)
# Each month's latest version; a month the engine could not capture is left out.
_MARGIN: Final = text(
    "SELECT tenant_id, period, through, contract_currency, revenue, actual_cost,"
    "   modelled_cost, band_basis, computed_at"
    " FROM (SELECT DISTINCT ON (tenant_id, period) * FROM cloudops.margin_snapshot"
    "       WHERE period BETWEEN :first AND :last_month"
    "       ORDER BY tenant_id, period, version DESC) s"
    " WHERE s.not_captured IS NULL AND s.revenue IS NOT NULL"
    " ORDER BY tenant_id, period"
)
_CONSUMPTION: Final = text(
    "SELECT tenant_id, usage_date, currency, sum(amortized_cost) FROM cloudops.cost_fact"
    " WHERE charge_class = 'consumption' AND usage_date BETWEEN :first AND :last"
    " GROUP BY tenant_id, usage_date, currency"
)
_RATES: Final = text(
    "SELECT from_currency, to_currency, effective_date, rate FROM cloudops.fx_rate"
    " WHERE rate_type = 'average' AND from_currency = ANY(:c) AND to_currency = ANY(:c)"
    "   AND effective_date BETWEEN :lo AND :hi"
)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _ccy(v: Any) -> str:
    return str(v).strip()


class _Inputs:
    """Everything one range needs, per tenant."""

    def __init__(self) -> None:
        self.sows: dict[UUID, revenue.Sow] = {}
        self.months: dict[UUID, list[revenue.MarginMonth]] = defaultdict(list)
        self.consumption: dict[UUID, list[revenue.Consumption]] = defaultdict(list)


async def _read(conn: ScopedConnection, scope: TenantScope, first: date,
                last: date) -> _Inputs:
    out = _Inputs()
    bounds = {"first": first, "last": last, "last_month": last.replace(day=1)}
    for (t, ref, ccy, eff, csp, monthly, hosting, manage, ps, uplift,
         at) in _rows(await conn.execute(_SOWS, bounds)):
        out.sows[UUID(str(t))] = revenue.Sow(
            reference=str(ref), currency=_ccy(ccy), effective_from=eff, csp=csp,
            monthly_manage_revenue=Decimal(monthly),
            hosting_acv=None if hosting is None else Decimal(hosting),
            manage_acv=None if manage is None else Decimal(manage),
            professional_services=Decimal(ps),
            consumption_uplift=None if uplift is None else Decimal(uplift), entered_at=at)
    for (t, period, through, ccy, rev, actual, modelled, basis,
         at) in _rows(await conn.execute(_MARGIN, bounds)):
        cost = actual if basis == "actual" else modelled
        if cost is None or ccy is None:
            continue
        out.months[UUID(str(t))].append(revenue.MarginMonth(
            period=period, through=through, currency=_ccy(ccy), revenue=Decimal(rev),
            cost=Decimal(cost), modelled=basis != "actual", computed_at=at))
    # Commercial figures are per customer, never per subscription (ADR 0002 D16): a
    # subscription-limited scope reads no margin, so it reads no consumption either.
    if scope.account_ids is None:
        for t, day, ccy, amount in _rows(await conn.execute(_CONSUMPTION, bounds)):
            out.consumption[UUID(str(t))].append(
                revenue.Consumption(day, _ccy(ccy), Decimal(amount)))
    return out


async def _rates(conn: ScopedConnection, inputs: list[_Inputs], first: date, last: date,
                 reporting: str | None) -> revenue.RateBook:
    currencies = {reporting} if reporting else set()
    earliest = first
    for i in inputs:
        for s in i.sows.values():
            currencies.add(s.currency)
            earliest = min(earliest, s.effective_from)
        currencies |= {m.currency for ms in i.months.values() for m in ms}
        currencies |= {c.currency for cs in i.consumption.values() for c in cs}
    if len(currencies) < 2:
        return revenue.RateBook([])
    rows = await conn.execute(_RATES, {"c": sorted(currencies), "lo": earliest - RATE_LOOKBACK,
                                       "hi": last})
    return revenue.RateBook((_ccy(f), _ccy(t), d, Decimal(r)) for f, t, d, r in _rows(rows))


# --- assembly ----------------------------------------------------------------------------------


def _metric(v: revenue.Value | None, unit: str | None, ref: str) -> Metric[Decimal] | None:
    if v is None or unit is None:
        return None
    return Metric[Decimal](value=v.amount, unit=unit, source_system=v.source, as_of=v.as_of,
                           query_ref=ref, is_modelled=v.modelled)


def _figures(f: revenue.Figures, ref: str) -> RevenueFigures:
    def m(name: str, unit: str | None = f.currency) -> Metric[Decimal] | None:
        return _metric(f.get(name), unit, ref)

    return RevenueFigures(
        currency=f.currency, cloud_consumption=m("cloud_consumption"),
        manage_revenue=m("manage_revenue"), manage_cost=m("manage_cost"),
        gross_margin=m("gross_margin"), gross_margin_pct=m("gross_margin_pct", RATIO),
        professional_services=m("professional_services"), hosting_acv=m("hosting_acv"),
        manage_acv=m("manage_acv"))


def _count(n: int, unit: str, ref: str, as_of: datetime) -> Metric[int]:
    return Metric[int](value=n, unit=unit, source_system="revenue_model", as_of=as_of,
                       query_ref=ref, is_modelled=False)


def _total(t: revenue.Totals, ref: str, as_of: datetime) -> RevenueTotal:
    return RevenueTotal(
        currency=t.currency, customers=_count(t.customers, "customers", ref, as_of),
        figures=_figures(t.figures, ref),
        missing=[MissingCount(figure=k, customers=_count(v, "customers", ref, as_of))
                 for k, v in t.missing.items() if v])


class _Computed:
    def __init__(self, first: date, last: date, rows: dict[UUID, revenue.Row],
                 sows: dict[UUID, revenue.Sow], ref: str) -> None:
        self.first, self.last, self.rows, self.sows, self.ref = first, last, rows, sows, ref


async def _compute(conn: ScopedConnection, scope: TenantScope, ranges: list[tuple[date, date]],
                   reporting: str | None, names: dict[UUID, str]) -> list[_Computed]:
    inputs = [await _read(conn, scope, a, b) for a, b in ranges]
    rates = await _rates(conn, inputs, min(a for a, _ in ranges), max(b for _, b in ranges),
                         reporting)
    out: list[_Computed] = []
    for (first, last), i in zip(ranges, inputs, strict=True):
        n = _months(first, last)
        rows = {t: revenue.customer(n, i.months.get(t, []), i.consumption.get(t, []),
                                    i.sows.get(t), rates, reporting) for t in names}
        out.append(_Computed(first, last, rows, i.sows, query_ref(
            "commercial.revenue", 1, first=first.isoformat(), last=last.isoformat(),
            rep=reporting)))
    return out


def _block(row: revenue.Row, ref: str) -> RevenueBlock:
    return RevenueBlock(
        contract=_figures(row.contract, ref),
        reported=None if row.reported is None else _figures(row.reported, ref),
        gaps=[FigureGap(figure=k, reason=v) for k, v in sorted(row.gaps.items())])


def _totals(c: _Computed, reporting: str | None, as_of: datetime) -> RevenueTotals:
    contract = revenue.totals(r.contract for r in c.rows.values())
    reported = None
    if reporting is not None:
        found = revenue.totals(r.reported for r in c.rows.values() if r.reported)
        reported = found[0] if found else revenue.Totals(
            reporting, 0, revenue.Figures(currency=reporting))
    return RevenueTotals(contract=[_total(t, c.ref, as_of) for t in contract],
                         reported=None if reported is None else _total(reported, c.ref, as_of))


def _range(c: _Computed, as_of: datetime) -> RevenueRange:
    return RevenueRange(first=c.first.strftime("%Y-%m"), last=c.last.strftime("%Y-%m"),
                        months=_count(_months(c.first, c.last), "months", c.ref, as_of))


def _ranges(first: str | None, last: str | None, compare_first: str | None,
            compare_last: str | None) -> list[tuple[date, date]]:
    first = first or last or _default_period()
    last = last or first
    out = [(first, last)]
    if (compare_first is None) != (compare_last is None):
        raise ApiError(422, "invalid_request")
    if compare_first and compare_last:
        out.append((compare_first, compare_last))
    ranges: list[tuple[date, date]] = []
    for a, b in out:
        start, end = _first(a), _first(b)
        if end < start or _months(start, end) > MAX_MONTHS:
            raise ApiError(422, "invalid_range")
        ranges.append((start, _add_months(end, 1) - timedelta(days=1)))
    return ranges


async def _overview(request: Request, access: Access, scope: TenantScope, first: str | None,
                    last: str | None, compare_first: str | None, compare_last: str | None,
                    currency: str | None) -> RevenueOverview:
    if access.kind != "staff":
        raise ApiError(403, "forbidden")
    ranges = _ranges(first, last, compare_first, compare_last)
    async with platform(request).db.transaction(scope) as conn:
        names = {UUID(str(r[0])): str(r[1]) for r in _rows(await conn.execute(_TENANTS))}
        computed = await _compute(conn, scope, ranges, currency, names)
    audit_only(request, list(names))
    now = _now()
    cur = computed[0]
    cmp = computed[1] if len(computed) > 1 else None
    rows: list[RevenueRow] = []
    for tid, name in names.items():
        row, sow = cur.rows[tid], cur.sows.get(tid)
        old = cmp.rows[tid] if cmp else None
        change = None
        if old is not None and cmp is not None:
            delta = revenue.change(row.contract, old.contract)
            rep = revenue.change(row.reported, old.reported)
            change = RevenueChange(
                contract=None if delta is None else _figures(delta, cur.ref),
                reported=None if rep is None else _figures(rep, cur.ref))
        rows.append(RevenueRow(
            tenant=TenantView(id=tid, name=name), csp=None if sow is None else sow.csp,
            sow_reference=None if sow is None else sow.reference,
            contract_currency=row.contract.currency, current=_block(row, cur.ref),
            compare=None if old is None or cmp is None else _block(old, cmp.ref),
            change=change))
    totals = _totals(cur, currency, now)
    compare_totals = None if cmp is None else _totals(cmp, currency, now)
    totals_change = None
    if cmp is not None and compare_totals is not None:
        before = {t.currency: t.figures for t in revenue.totals(
            r.contract for r in cmp.rows.values())}
        contract = [d for t in revenue.totals(r.contract for r in cur.rows.values())
                    if (d := revenue.change(t.figures, before.get(t.currency)))]
        reported = None
        if currency is not None:
            a = revenue.totals(r.reported for r in cur.rows.values() if r.reported)
            b = revenue.totals(r.reported for r in cmp.rows.values() if r.reported)
            d = revenue.change(a[0].figures if a else None, b[0].figures if b else None)
            reported = None if d is None else _figures(d, cur.ref)
        totals_change = RevenueTotalsChange(
            contract=[_figures(d, cur.ref) for d in contract], reported=reported)
    return RevenueOverview(
        range=_range(cur, now), compare_range=None if cmp is None else _range(cmp, now),
        reporting_currency=currency, rows=rows, totals=totals, compare_totals=compare_totals,
        totals_change=totals_change)


@router.get("/commercial/revenue", response_model=RevenueOverview)
@requires("commercial.read")
async def revenue_overview(
    request: Request, access: AccessDep, scope: ScopeDep,
    first: Annotated[str | None, Query(alias="from", pattern=MONTH)] = None,
    last: Annotated[str | None, Query(alias="to", pattern=MONTH)] = None,
    compare_first: Annotated[str | None, Query(alias="compare_from", pattern=MONTH)] = None,
    compare_last: Annotated[str | None, Query(alias="compare_to", pattern=MONTH)] = None,
    currency: Currency = None,
) -> RevenueOverview:
    """Every customer in scope over a range of months (default: the latest closed
    month), optionally against a second range: CSP, average monthly consumption,
    manage revenue and cost, gross margin, professional services and ACVs, with
    totals per contract currency and, given ``currency``, in it."""
    return await _overview(request, access, scope, first, last, compare_first, compare_last,
                           currency)


# --- CSV ---------------------------------------------------------------------------------------


CSV_HEADER: Final = (
    "Sl No", "Customer name", "CSP", "Currency", "Average monthly cloud consumption",
    "Monthly manage revenue", "Manage cost (actual)", "Gross margin on manage revenue (%)",
    "Professional services (one-time)", "Hosting ACV", "Manage ACV", "Notes",
)
CENTS: Final = Decimal("0.01")
TENTH: Final = Decimal("0.1")
HUNDRED: Final = Decimal(100)


def _cell(text_: str) -> str:
    # A leading formula character would execute in a spreadsheet: neutralise it.
    return f"'{text_}" if text_[:1] in ("=", "+", "-", "@", "\t", "\r") else text_


def _amount(m: Metric[Decimal] | None) -> str:
    if m is None:
        return ""
    return str(m.value.quantize(CENTS, rounding=ROUND_HALF_UP))


def _pct(m: Metric[Decimal] | None) -> str:
    if m is None:
        return ""
    return str((m.value * HUNDRED).quantize(TENTH, rounding=ROUND_HALF_UP))


def _notes(f: RevenueFigures, gaps: list[FigureGap]) -> str:
    modelled = [k for k in ("manage_cost", "hosting_acv", "manage_acv")
                if (m := getattr(f, k)) is not None and m.is_modelled]
    parts = [f"{k} modelled" for k in modelled]
    parts += [f"{g.figure}: {g.reason}" for g in gaps]
    return "; ".join(parts)


def _csv_line(n: str, name: str, csp: bool | None, f: RevenueFigures,
              notes: str) -> list[str]:
    return [n, _cell(name), "" if csp is None else ("Yes" if csp else "No"), f.currency or "",
            _amount(f.cloud_consumption), _amount(f.manage_revenue), _amount(f.manage_cost),
            _pct(f.gross_margin_pct), _amount(f.professional_services), _amount(f.hosting_acv),
            _amount(f.manage_acv), notes]


def render_csv(view: RevenueOverview) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    reported = view.reporting_currency is not None
    for i, row in enumerate(sorted(view.rows, key=lambda r: r.tenant.name.lower()), start=1):
        f = row.current.reported if reported and row.current.reported else row.current.contract
        w.writerow(_csv_line(str(i), row.tenant.name, row.csp, f,
                             _notes(f, row.current.gaps)))
    totals = [view.totals.reported] if view.totals.reported else view.totals.contract
    for t in totals:
        missing = "; ".join(f"{m.figure}: {m.customers.value} not included" for m in t.missing)
        w.writerow(_csv_line("", f"Total ({t.currency})", None, t.figures, missing))
    return out.getvalue()


@router.get("/commercial/revenue.csv", response_class=Response, response_model=None,
            responses={200: {"content": {"text/csv": {}}}})
@requires("commercial.read")
async def revenue_csv(
    request: Request, access: AccessDep, scope: ScopeDep,
    first: Annotated[str | None, Query(alias="from", pattern=MONTH)] = None,
    last: Annotated[str | None, Query(alias="to", pattern=MONTH)] = None,
    currency: Currency = None,
) -> Response:
    """The range's rows and totals as the workbook's sheet, rounded for print."""
    view = await _overview(request, access, scope, first, last, None, None, currency)
    name = f"revenue-margin-{view.range.first}-{view.range.last}.csv"
    return Response(render_csv(view), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
