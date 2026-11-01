"""Revenue & margin per customer over a range of months (ADR 0038). Pure; the API
reads the stored figures and the rates and calls ``customer``.

The columns are the monthly report workbook's: CSP, average monthly cloud
consumption, monthly manage revenue, manage cost (actual), gross margin on
manage revenue, professional services (one-time), hosting ACV and manage ACV.

**Where each figure comes from.**

- *Cloud consumption*: the ``consumption`` charge class of the cost facts (the
  only class that feeds margin, HLD D8), amortized, summed over the range and
  divided by the months in it: a monthly average.
- *Manage revenue, manage cost, gross margin*: the margin engine's latest
  snapshot of each month (``margin_snapshot``), averaged over the months that
  have one. Cost is the actual cost; a month whose band fell back to modelled
  cost (effort incomplete) contributes its modelled cost, and the figure is then
  marked modelled. Margin is revenue minus that cost, per month, in the contract
  currency, and the percentage is the range's margin over its revenue.
- *Professional services, hosting ACV, manage ACV*: the SOW version in force at
  the end of the range (else the latest one that overlaps it). An ACV entered on
  the SOW is used as entered. One not entered is **derived, and marked
  modelled**:

    manage ACV  = 12 x the SOW's monthly manage revenue
    hosting ACV = 12 x average monthly consumption x (1 + the SOW's consumption
                  uplift), for a CSP customer only; a customer whose cloud
                  Getronics does not resell has no hosting ACV.

  Until ServiceNow project tasks are ingested, professional services is the
  SOW's figure.

**Currency** (CLAUDE.md rule 5). A row's currency is its contract currency. Each
figure is computed in it, at the rate in force on its transaction date:

- consumption at the daily average rate in force on each usage date;
- revenue, cost and margin at each month's average rate (flows, as margin does),
  after margin was computed in the contract currency;
- an SOW figure at the rate in force on the day the version took effect.

The same rules convert into a reporting currency. A conversion with no rate is
absent, with its gap named, never a 1 or a 0. Nothing here rounds.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final, Literal

ZERO: Final = Decimal(0)
ONE: Final = Decimal(1)
MONTHS_A_YEAR: Final = Decimal(12)  # not money: months in a year

Source = Literal["billing_exports", "margin_engine", "sow_register", "revenue_model"]
Gap = Literal[
    "no_sow",                 # no SOW overlaps the range
    "no_margin",              # no month in the range has a computed margin
    "partial_range",          # some months in the range have no computed margin
    "no_cost_data",           # no consumption in the range
    "no_fx_rate",             # a figure has no rate in force to convert it
    "mixed_currency",         # consumption in currencies with no contract currency to join them
    "csp_not_recorded",       # the SOW does not say whether Getronics resells the cloud
    "not_csp",                # not a CSP customer: no hosting ACV
    "no_uplift",              # CSP, but no consumption uplift to price hosting with
]
FIGURES: Final = ("cloud_consumption", "manage_revenue", "manage_cost", "gross_margin",
                  "gross_margin_pct", "professional_services", "hosting_acv", "manage_acv")


# --- rates -------------------------------------------------------------------------------------


class RateBook:
    """Daily average rates, looked up as the rate in force on a day: the latest on
    or before it. Same currency is 1. Built from ``(from, to, effective_date,
    rate)`` rows the caller loaded for the dates it needs."""

    def __init__(self, rows: Iterable[tuple[str, str, date, Decimal]]) -> None:
        by: dict[tuple[str, str], list[tuple[date, Decimal]]] = defaultdict(list)
        for frm, to, on, rate in rows:
            by[(frm, to)].append((on, Decimal(rate)))
        self._days: dict[tuple[str, str], list[date]] = {}
        self._rates: dict[tuple[str, str], list[Decimal]] = {}
        for pair, items in by.items():
            items.sort()
            self._days[pair] = [d for d, _ in items]
            self._rates[pair] = [r for _, r in items]

    def on(self, frm: str, to: str, day: date) -> Decimal | None:
        """The rate in force on ``day``."""
        if frm == to:
            return ONE
        days = self._days.get((frm, to))
        if not days:
            return None
        i = bisect.bisect_right(days, day)
        return None if i == 0 else self._rates[(frm, to)][i - 1]

    def month_average(self, frm: str, to: str, first: date, through: date) -> Decimal | None:
        """The mean of the daily rates published from ``first`` to ``through``, as
        the margin engine converts a month (``margin.MonthRate``)."""
        if frm == to:
            return ONE
        days = self._days.get((frm, to))
        if not days:
            return None
        lo, hi = bisect.bisect_left(days, first), bisect.bisect_right(days, through)
        rates = self._rates[(frm, to)][lo:hi]
        return sum(rates, ZERO) / len(rates) if rates else None


# --- inputs ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Consumption:
    """A day's consumption cost in one transaction currency."""

    usage_date: date
    currency: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class MarginMonth:
    """The latest margin snapshot of one month, as the engine stored it."""

    period: date  # first day
    through: date
    currency: str
    revenue: Decimal
    cost: Decimal  # the band's basis: actual, else modelled
    modelled: bool  # the cost is modelled (effort incomplete)
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class Sow:
    """The SOW version the row reads its commercial terms from."""

    reference: str
    currency: str
    effective_from: date
    csp: bool | None
    monthly_manage_revenue: Decimal
    hosting_acv: Decimal | None
    manage_acv: Decimal | None
    professional_services: Decimal
    consumption_uplift: Decimal | None
    entered_at: datetime


# --- outputs -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Value:
    amount: Decimal
    modelled: bool
    source: Source
    as_of: datetime


@dataclass(frozen=True, slots=True)
class Figures:
    """One row's figures in one currency. ``None``: not captured (see the gaps)."""

    currency: str | None
    cloud_consumption: Value | None = None
    manage_revenue: Value | None = None
    manage_cost: Value | None = None
    gross_margin: Value | None = None
    gross_margin_pct: Value | None = None  # a ratio, the same in every currency
    professional_services: Value | None = None
    hosting_acv: Value | None = None
    manage_acv: Value | None = None

    def get(self, name: str) -> Value | None:
        value: Value | None = getattr(self, name)
        return value


@dataclass(frozen=True, slots=True)
class Row:
    contract: Figures
    reported: Figures | None  # None: no reporting currency asked
    gaps: dict[str, Gap]  # figure (or "row") -> why it is absent or partial


def _scaled(v: Value | None, factor: Decimal | None) -> Value | None:
    if v is None or factor is None:
        return None
    return replace(v, amount=v.amount * factor)


def _currency(sow: Sow | None, months: Sequence[MarginMonth],
              consumption: Sequence[Consumption]) -> str | None:
    if sow is not None:
        return sow.currency
    if months:
        return months[-1].currency
    currencies = {c.currency for c in consumption}
    return currencies.pop() if len(currencies) == 1 else None


def _consumption(days: Sequence[Consumption], to: str, rates: RateBook,
                 months_in_range: int) -> tuple[Value | None, Gap | None]:
    if not days:
        return None, "no_cost_data"
    total = ZERO
    for d in days:
        rate = rates.on(d.currency, to, d.usage_date)
        if rate is None:
            return None, "no_fx_rate"
        total += d.amount * rate
    last = max(d.usage_date for d in days)
    as_of = datetime.combine(last + timedelta(days=1), time.min, tzinfo=UTC)
    return Value(total / months_in_range, False, "billing_exports", as_of), None


def _margin(months: Sequence[MarginMonth], to: str, rates: RateBook,
            ) -> tuple[tuple[Value, Value, Value, Value] | None, Gap | None]:
    """Average monthly revenue, cost and margin, and the margin percentage, each
    month converted at its own average rate after its margin was computed."""
    revenue = cost = margin = contract_revenue = contract_margin = ZERO
    for m in months:
        rate = rates.month_average(m.currency, to, m.period, m.through)
        if rate is None:
            return None, "no_fx_rate"
        month_margin = m.revenue - m.cost  # in the contract currency, first
        revenue += m.revenue * rate
        cost += m.cost * rate
        margin += month_margin * rate
        contract_revenue += m.revenue
        contract_margin += month_margin
    n = len(months)
    modelled = any(m.modelled for m in months)
    as_of = max(m.computed_at for m in months)
    pct = contract_margin / contract_revenue if contract_revenue else None

    def v(amount: Decimal, is_modelled: bool = modelled) -> Value:
        return Value(amount, is_modelled, "margin_engine", as_of)

    if pct is None:
        return None, "no_margin"
    return (v(revenue / n, False), v(cost / n), v(margin / n), v(pct)), None


def _figures(currency: str, months: Sequence[MarginMonth], consumption: Sequence[Consumption],
             sow: Sow | None, rates: RateBook, months_in_range: int,
             gaps: dict[str, Gap]) -> Figures:
    used, gap = _consumption(consumption, currency, rates, months_in_range)
    if gap:
        gaps.setdefault("cloud_consumption", gap)
    f = Figures(currency=currency, cloud_consumption=used)
    if months:
        got, gap = _margin(months, currency, rates)
        if got is None:
            for name in ("manage_revenue", "manage_cost", "gross_margin", "gross_margin_pct"):
                gaps.setdefault(name, gap or "no_margin")
        else:
            rev, cost, margin, pct = got
            f = replace(f, manage_revenue=rev, manage_cost=cost, gross_margin=margin,
                        gross_margin_pct=pct)
            if len(months) < months_in_range:
                for name in ("manage_revenue", "manage_cost", "gross_margin"):
                    gaps.setdefault(name, "partial_range")
    else:
        for name in ("manage_revenue", "manage_cost", "gross_margin", "gross_margin_pct"):
            gaps.setdefault(name, "no_margin" if sow is not None else "no_sow")
    if sow is None:
        for name in ("professional_services", "hosting_acv", "manage_acv"):
            gaps.setdefault(name, "no_sow")
        return f
    rate = rates.on(sow.currency, currency, sow.effective_from)
    if rate is None:
        for name in ("professional_services", "hosting_acv", "manage_acv"):
            gaps.setdefault(name, "no_fx_rate")
    entered = Value(ZERO, False, "sow_register", sow.entered_at)
    ps = replace(entered, amount=sow.professional_services * rate) if rate else None
    if sow.manage_acv is not None:
        manage = _scaled(replace(entered, amount=sow.manage_acv), rate)
    else:
        manage = _scaled(Value(MONTHS_A_YEAR * sow.monthly_manage_revenue, True,
                               "revenue_model", sow.entered_at), rate)
    hosting: Value | None = None
    if sow.hosting_acv is not None:
        hosting = _scaled(replace(entered, amount=sow.hosting_acv), rate)
    elif sow.csp is None:
        gaps["hosting_acv"] = "csp_not_recorded"
    elif not sow.csp:
        gaps["hosting_acv"] = "not_csp"
    elif sow.consumption_uplift is None:
        gaps["hosting_acv"] = "no_uplift"
    elif used is not None:
        hosting = Value(MONTHS_A_YEAR * used.amount * (ONE + sow.consumption_uplift), True,
                        "revenue_model", max(used.as_of, sow.entered_at))
    else:
        gaps.setdefault("hosting_acv", gaps.get("cloud_consumption", "no_cost_data"))
    return replace(f, professional_services=ps, hosting_acv=hosting, manage_acv=manage)


def customer(months_in_range: int, months: Sequence[MarginMonth],
             consumption: Sequence[Consumption], sow: Sow | None, rates: RateBook,
             reporting: str | None) -> Row:
    """One customer's row: its figures in its contract currency and, given one, in
    the reporting currency. ``months`` are the range's computed months, one each;
    a month the engine marked not captured is left out by the caller."""
    if months_in_range < 1:
        raise ValueError("a range has at least one month")
    gaps: dict[str, Gap] = {}
    currency = _currency(sow, months, consumption)
    contract_months = [m for m in months if m.currency == currency]
    if currency is None:
        gaps["row"] = "mixed_currency" if consumption else "no_sow"
        contract = Figures(currency=None)
    else:
        contract = _figures(currency, contract_months, consumption, sow, rates,
                            months_in_range, gaps)
    reported = None
    if reporting is not None:
        reported_gaps: dict[str, Gap] = {}
        reported = _figures(reporting, contract_months, consumption, sow, rates,
                            months_in_range, reported_gaps)
        if currency is None:  # consumption and nothing else can still be converted
            reported = Figures(currency=reporting, cloud_consumption=reported.cloud_consumption)
        # Only what the conversion lost: a figure absent in the contract currency
        # already names its gap.
        for name, gap in reported_gaps.items():
            if name not in gaps:
                gaps[f"reported.{name}"] = gap
    return Row(contract=contract, reported=reported, gaps=gaps)


# --- totals and change -------------------------------------------------------------------------


MONEY: Final = ("cloud_consumption", "manage_revenue", "manage_cost", "gross_margin",
                "professional_services", "hosting_acv", "manage_acv")


@dataclass(frozen=True, slots=True)
class Totals:
    currency: str
    customers: int  # rows summed into this currency
    figures: Figures
    # Per figure, how many of those rows had no value: the total leaves them out.
    missing: Mapping[str, int] = field(default_factory=dict)


def totals(rows: Iterable[Figures]) -> list[Totals]:
    """Sums per currency. Rows are monthly averages and contract values, so the sum
    of customers' averages is the portfolio's average. The margin percentage is
    the summed margin over the summed revenue of the rows that have both."""
    groups: dict[str, list[Figures]] = defaultdict(list)
    for f in rows:
        if f.currency is not None:
            groups[f.currency].append(f)
    out: list[Totals] = []
    for ccy in sorted(groups):
        group = groups[ccy]
        sums: dict[str, Value] = {}
        missing: dict[str, int] = {}
        for name in MONEY:
            values = [v for f in group if (v := f.get(name)) is not None]
            missing[name] = len(group) - len(values)
            if values:
                sums[name] = Value(sum((v.amount for v in values), ZERO),
                                   any(v.modelled for v in values), values[0].source,
                                   max(v.as_of for v in values))
        both = [f for f in group if f.manage_revenue is not None and f.gross_margin is not None]
        missing["gross_margin_pct"] = len(group) - len(both)
        pct = None
        revenue = sum((f.manage_revenue.amount for f in both if f.manage_revenue), ZERO)
        if both and revenue:
            margin = sum((f.gross_margin.amount for f in both if f.gross_margin), ZERO)
            pct = Value(margin / revenue,
                        any(f.gross_margin.modelled for f in both if f.gross_margin),
                        "margin_engine", max(f.gross_margin.as_of for f in both
                                             if f.gross_margin))
        out.append(Totals(currency=ccy, customers=len(group), figures=Figures(
            currency=ccy, gross_margin_pct=pct, **sums), missing=missing))
    return out


def change(current: Figures | None, compare: Figures | None) -> Figures | None:
    """Current minus compare, figure by figure, in one currency. The percentage's
    change is in ratio points. Absent where either side is."""
    if current is None or compare is None or current.currency != compare.currency:
        return None
    out: dict[str, Value] = {}
    for name in (*MONEY, "gross_margin_pct"):
        a, b = current.get(name), compare.get(name)
        if a is not None and b is not None:
            out[name] = Value(a.amount - b.amount, a.modelled or b.modelled, a.source,
                              max(a.as_of, b.as_of))
    return Figures(currency=current.currency, **out)
