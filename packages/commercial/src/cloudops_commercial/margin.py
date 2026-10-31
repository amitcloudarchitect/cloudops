"""Margin and contribution (HLD M12) in the contract currency.

Revenue is the SOW's monthly manage revenue, in the SOW's currency. Cost comes
from the pricing port (``pricing.modelled_cost`` and ``pricing.actual_cost``), in
the rate card's currency (EUR):

- **modelled** = implied FTE x loaded people cost + tooling, where the FTE comes
  from quoting the customer's priced CIs and the month's consumption;
- **actual** = observed effort hours x loaded day rate + tooling, the hours
  being ServiceNow's ``actual_hours`` (step 16 decision: nothing estimated from
  durations; work items without hours make the actual figure *partial*). A
  month with no work items, or no hours at all, is *incomplete* too: no
  effort recorded is a gap (an instance not connected, a mapping hole), never
  zero cost, so the band falls back to the modelled basis.

**Margin is computed in the contract currency first, then converted** (HLD,
Multi-currency). Each cost is converted from the rate card's currency into the
contract currency at the month's average rate (flows use average), *then*
subtracted from revenue. The same month is also computed at the previous
month's average rate, so the serving layer can report FX impact as its own line
(step 16 decision): this month at this month's rates minus this month at last
month's rates, covering cost-to-contract here and contract-to-reporting there.

Gaps are visible (HLD principle 7): no SOW is *not captured*, never zero; a
delivery location the rate card has no people cost for, a month with no billing
data, or a missing rate leaves the figure not captured too.
Nothing here rounds: figures are stored at numeric(18,6) and rounded once, at
presentation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal, cast

from cloudops_commercial import pricing
from cloudops_commercial.ratecard import Coverage, Location, RateCard, SupportModel

ZERO: Final = Decimal(0)

NotCaptured = Literal["no_sow", "no_people_cost", "no_fx_rate", "no_cost_data"]


@dataclass(frozen=True, slots=True)
class MonthRate:
    """The average rate for a month (mean of the daily average rates in it), from
    the rate card's currency into the contract currency."""

    rate: Decimal
    first_day: date  # the earliest daily rate the mean used
    last_day: date
    days: int


@dataclass(frozen=True, slots=True)
class EffortBucket:
    """ServiceNow effort closed in the month, for one workgroup and activity type."""

    workgroup: str
    activity: str  # work_item.kind
    hours: Decimal
    items: int
    items_without_hours: int


@dataclass(frozen=True, slots=True)
class MarginInputs:
    period: date  # first day of the month
    contract_currency: str
    revenue: Decimal | None  # monthly manage revenue; None: no SOW
    target_margin: Decimal | None  # a fraction, from the SOW
    location: str  # the tenant's delivery location
    coverage: Coverage
    support: SupportModel
    priced_inventory: Mapping[str, int]  # catalogue row -> critical CI count
    unpriced_cis: int  # critical CIs whose type has no catalogue row
    consumption_eur: Decimal  # the month's consumption-class cost, in EUR
    has_cost_data: bool  # any consumption line in the month at all
    effort: tuple[EffortBucket, ...]
    rate: MonthRate | None  # rate card currency -> contract currency, this month
    prior_rate: MonthRate | None  # the same, last month


@dataclass(frozen=True, slots=True)
class VarianceLine:
    workgroup: str
    activity: str
    hours: Decimal
    items: int
    items_without_hours: int
    share: Decimal  # of the month's observed hours
    excess_hours: Decimal  # this line's attributed part of hours above the slab
    excess_cost: Decimal  # in the contract currency


@dataclass(frozen=True, slots=True)
class MarginFigures:
    period: date
    rate_card_version: str
    contract_currency: str
    not_captured: NotCaptured | None
    revenue: Decimal | None
    modelled_cost: Decimal | None
    actual_cost: Decimal | None
    tooling_cost: Decimal | None
    modelled_margin: Decimal | None
    actual_margin: Decimal | None
    margin_pct: Decimal | None  # on the band basis below
    band_basis: Literal["actual", "modelled"] | None
    band: str | None
    target_margin: Decimal | None
    gap_to_target: Decimal | None  # margin_pct - target (negative: below target)
    margin_at_prior_rates: Decimal | None  # band-basis margin at last month's rate
    fte: Decimal | None
    modelled_hours: Decimal | None  # the priced slab's effort, in hours
    effort_hours: Decimal
    effort_items: int
    effort_items_without_hours: int
    effort_complete: bool
    unpriced_cis: int
    exceeds_slab: bool
    rate: MonthRate | None
    prior_rate: MonthRate | None
    variance: tuple[VarianceLine, ...]


def month_rate(daily: Iterable[tuple[date, Decimal]]) -> MonthRate | None:
    """The mean of a month's daily average rates. None if there are none: a
    missing rate is a gap, never a 1 or a 0."""
    points = sorted(daily)
    if not points:
        return None
    total = sum((r for _, r in points), ZERO)
    return MonthRate(total / len(points), points[0][0], points[-1][0], len(points))


def _location(value: str, card: RateCard) -> Location | None:
    return cast(Location, value) if value in card.people_cost else None


def compute(inputs: MarginInputs, card: RateCard) -> MarginFigures:
    hours = sum((b.hours for b in inputs.effort), ZERO)
    items = sum(b.items for b in inputs.effort)
    missing = sum(b.items_without_hours for b in inputs.effort)
    location = _location(inputs.location, card)
    # Zero items or zero hours is a gap, not free delivery (HLD principle 7).
    complete = items > 0 and hours > 0 and missing == 0

    def empty(reason: NotCaptured) -> MarginFigures:
        return MarginFigures(
            inputs.period, card.version, inputs.contract_currency, reason, inputs.revenue,
            None, None, None, None, None, None, None, None, inputs.target_margin, None, None,
            None, None, hours, items, missing, complete, inputs.unpriced_cis, False,
            inputs.rate, inputs.prior_rate, ())

    if inputs.revenue is None:
        return empty("no_sow")
    if location is None:
        return empty("no_people_cost")
    if not inputs.has_cost_data:
        # The base pack and governance price on consumption: without a line of it
        # the modelled cost would be wrong, not small.
        return empty("no_cost_data")
    if inputs.rate is None or inputs.prior_rate is None:
        return empty("no_fx_rate")

    q = pricing.quote(pricing.Engagement(
        customer="", inventory=dict(inputs.priced_inventory),
        monthly_consumption_eur=inputs.consumption_eur, coverage=inputs.coverage,
        support_model=inputs.support, location=location, tools=card.default_tools,
        corestack=True), card)
    modelled_eur = pricing.modelled_cost(q.fte_count, q.platform_cost_eur, location, card)
    actual_eur = pricing.actual_cost(hours, q.platform_cost_eur, location, card)

    # Convert each cost into the contract currency, then subtract.
    def conv(amount: Decimal, rate: MonthRate) -> Decimal:
        return amount * rate.rate

    revenue = inputs.revenue
    modelled = conv(modelled_eur, inputs.rate)
    actual = conv(actual_eur, inputs.rate)
    basis: Literal["actual", "modelled"] = "actual" if complete else "modelled"
    basis_eur = actual_eur if complete else modelled_eur
    basis_margin = revenue - conv(basis_eur, inputs.rate)
    pct = basis_margin / revenue if revenue else None
    modelled_hours = q.fte_count * card.working_days_per_month * card.hours_per_day
    excess = hours - modelled_hours
    day_rate_c = conv(pricing.day_rate(location, card), inputs.rate)

    # Hours above the slab are attributed to each workgroup and activity type in
    # proportion to its share of the month's observed hours.
    lines: list[VarianceLine] = []
    for b in sorted(inputs.effort, key=lambda b: (-b.hours, b.workgroup, b.activity)):
        share = b.hours / hours if hours else ZERO
        ex = excess * share if excess > 0 else ZERO
        lines.append(VarianceLine(b.workgroup, b.activity, b.hours, b.items,
                                  b.items_without_hours, share, ex,
                                  ex / card.hours_per_day * day_rate_c))
    variance = tuple(lines)

    return MarginFigures(
        period=inputs.period,
        rate_card_version=card.version,
        contract_currency=inputs.contract_currency,
        not_captured=None,
        revenue=revenue,
        modelled_cost=modelled,
        actual_cost=actual,
        tooling_cost=conv(q.platform_cost_eur, inputs.rate),
        modelled_margin=revenue - modelled,
        actual_margin=revenue - actual,
        margin_pct=pct,
        band_basis=basis,
        band=None if pct is None else card.health(pct),
        target_margin=inputs.target_margin,
        gap_to_target=(None if pct is None or inputs.target_margin is None
                       else pct - inputs.target_margin),
        margin_at_prior_rates=revenue - conv(basis_eur, inputs.prior_rate),
        fte=q.fte_count,
        modelled_hours=modelled_hours,
        effort_hours=hours,
        effort_items=items,
        effort_items_without_hours=missing,
        effort_complete=complete,
        unpriced_cis=inputs.unpriced_cis,
        exceeds_slab=excess > 0,
        rate=inputs.rate,
        prior_rate=inputs.prior_rate,
        variance=variance,
    )


@dataclass(frozen=True, slots=True)
class Reported:
    """A contract-currency margin converted for a viewer, with FX impact apart."""

    margin: Decimal
    margin_at_prior_rates: Decimal
    fx_impact: Decimal  # margin - margin_at_prior_rates


def report(margin_c: Decimal, margin_c_at_prior: Decimal,
           to_reporting: Decimal, to_reporting_prior: Decimal) -> Reported:
    """Convert after computing. ``to_reporting`` is this month's average rate from
    the contract currency into the reporting currency; ``to_reporting_prior``
    last month's. FX impact is everything the two rate sets explain; the rest of
    the month-on-month change is operational."""
    now = margin_c * to_reporting
    before = margin_c_at_prior * to_reporting_prior
    return Reported(now, before, now - before)
