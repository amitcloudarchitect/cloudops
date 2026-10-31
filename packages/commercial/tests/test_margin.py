"""Margin in the contract currency (HLD M12, Multi-currency). Every case runs in at
least two currencies: the defects live in conversion, rate selection and rounding
(LLD, Test data)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cloudops_commercial import margin, pricing
from cloudops_commercial.margin import EffortBucket, MarginInputs, MonthRate
from cloudops_commercial.ratecard import V2_14, RateCardError, rate_card, rate_card_on

INV = {"Azure Virtual Machines / Amazon EC2": 15, "App Service / AWS Elastic Beanstalk": 260,
       "Azure Kubernetes Service (AKS) / Amazon EKS": 46}
OCT = date(2026, 10, 1)


def rate(r: str) -> MonthRate:
    return MonthRate(Decimal(r), OCT, date(2026, 10, 30), 22)


def inputs(**kw: object) -> MarginInputs:
    base: dict[str, object] = dict(
        period=OCT, contract_currency="EUR", revenue=Decimal("17095.82"),
        target_margin=Decimal("0.25"), location="India", coverage="24x7", support="Gold",
        priced_inventory=INV, unpriced_cis=4, consumption_eur=Decimal("43856"),
        has_cost_data=True,
        effort=(EffortBucket("Cloud Ops", "incident", Decimal(700), 40, 0),
                EffortBucket("Network", "change_task", Decimal(400), 10, 0)),
        rate=rate("1"), prior_rate=rate("1"))
    base.update(kw)
    return MarginInputs(**base)  # type: ignore[arg-type]


def test_eur_contract_matches_the_pricing_port() -> None:
    f = margin.compute(inputs(), V2_14)
    e = pricing.Engagement("x", INV, Decimal("43856"), sow_monthly_revenue_eur=Decimal(
        "17095.82"))
    m = pricing.margin(e, pricing.quote(e, V2_14), V2_14, actual_effort_hours=Decimal(1100))
    assert f.not_captured is None
    assert f.modelled_cost == m.modelled_cost_eur
    assert f.actual_cost == m.actual_cost_eur
    assert f.margin_pct == m.gross_margin_pct
    assert f.band == m.health
    assert f.band_basis == "actual"
    assert f.effort_complete
    assert f.unpriced_cis == 4


def test_gbp_contract_converts_cost_before_subtracting() -> None:
    """GBP revenue, EUR cost: the cost is converted at the month's rate, then
    subtracted. Never revenue converted to EUR and the margin converted back."""
    eur = margin.compute(inputs(), V2_14)
    gbp = margin.compute(inputs(contract_currency="GBP", revenue=Decimal("15000"),
                                rate=rate("0.86"), prior_rate=rate("0.85")), V2_14)
    assert eur.actual_cost is not None
    assert gbp.actual_cost is not None
    assert gbp.actual_cost == eur.actual_cost * Decimal("0.86")
    assert gbp.actual_margin == Decimal("15000") - gbp.actual_cost
    assert gbp.margin_pct == gbp.actual_margin / Decimal("15000")
    assert gbp.gap_to_target == gbp.margin_pct - Decimal("0.25")
    # The same month at last month's rate: what the currency alone moved.
    assert gbp.margin_at_prior_rates == Decimal("15000") - eur.actual_cost * Decimal("0.85")


def test_fx_impact_is_its_own_line() -> None:
    """GBP contract reported in USD: impact = now at now's rates - now at last
    month's, and an unchanged rate set has no impact at all."""
    gbp = margin.compute(inputs(contract_currency="GBP", revenue=Decimal("15000"),
                                rate=rate("0.86"), prior_rate=rate("0.85")), V2_14)
    assert gbp.actual_margin is not None
    assert gbp.margin_at_prior_rates is not None
    r = margin.report(gbp.actual_margin, gbp.margin_at_prior_rates, Decimal("1.27"),
                      Decimal("1.25"))
    assert r.margin == gbp.actual_margin * Decimal("1.27")
    assert r.fx_impact == r.margin - gbp.margin_at_prior_rates * Decimal("1.25")
    flat = margin.compute(inputs(contract_currency="USD", revenue=Decimal("18000"),
                                 rate=rate("1.08"), prior_rate=rate("1.08")), V2_14)
    assert flat.actual_margin is not None
    assert flat.margin_at_prior_rates is not None
    assert margin.report(flat.actual_margin, flat.margin_at_prior_rates, Decimal(1),
                         Decimal(1)).fx_impact == 0


def test_no_sow_is_not_captured_never_zero() -> None:
    for ccy in ("EUR", "GBP"):
        f = margin.compute(inputs(revenue=None, contract_currency=ccy), V2_14)
        assert f.not_captured == "no_sow"
        assert f.margin_pct is None
        assert f.actual_margin is None
        assert f.band is None


def test_mixed_delivery_has_no_people_cost_rather_than_an_invented_blend() -> None:
    for ccy, r in (("USD", "1.08"), ("EUR", "1")):
        f = margin.compute(inputs(location="Mixed", contract_currency=ccy,
                                  rate=rate(r), prior_rate=rate(r)), V2_14)
        assert f.not_captured == "no_people_cost"
        assert f.modelled_cost is None


def test_missing_rate_is_not_captured() -> None:
    f = margin.compute(inputs(contract_currency="GBP", rate=None), V2_14)
    assert f.not_captured == "no_fx_rate"
    f = margin.compute(inputs(contract_currency="USD", prior_rate=None), V2_14)
    assert f.not_captured == "no_fx_rate"


def test_partial_effort_bands_on_modelled_cost_and_says_so() -> None:
    f = margin.compute(inputs(contract_currency="GBP", revenue=Decimal("15000"),
                              rate=rate("0.86"), prior_rate=rate("0.86"),
                              effort=(EffortBucket("Cloud Ops", "incident", Decimal(10), 40,
                                                   12),)), V2_14)
    assert not f.effort_complete
    assert f.effort_items_without_hours == 12
    assert f.band_basis == "modelled"
    assert f.margin_pct == f.modelled_margin / Decimal("15000")  # type: ignore[operator]


def test_variance_attributes_hours_above_the_slab() -> None:
    small = {"Azure Virtual Machines / Amazon EC2": 5}
    f = margin.compute(inputs(priced_inventory=small, consumption_eur=Decimal("900"),
                              contract_currency="GBP", revenue=Decimal("9000"),
                              rate=rate("0.86"), prior_rate=rate("0.86")), V2_14)
    assert f.modelled_hours is not None
    assert f.exceeds_slab
    excess = Decimal(1100) - f.modelled_hours
    lines = {(v.workgroup, v.activity): v for v in f.variance}
    ops = lines[("Cloud Ops", "incident")]
    assert ops.share == Decimal(700) / Decimal(1100)
    assert ops.excess_hours == excess * ops.share
    assert sum(v.excess_hours for v in f.variance) == pytest.approx(excess)
    day_rate_gbp = pricing.day_rate("India", V2_14) * Decimal("0.86")
    assert ops.excess_cost == ops.excess_hours / 8 * day_rate_gbp
    assert [v.workgroup for v in f.variance] == ["Cloud Ops", "Network"]  # largest first


def test_within_slab_attributes_nothing() -> None:
    f = margin.compute(inputs(effort=(EffortBucket("Cloud Ops", "incident", Decimal(5), 2,
                                                   0),)), V2_14)
    assert not f.exceeds_slab
    assert all(v.excess_hours == 0 for v in f.variance)


@pytest.mark.parametrize(("pct", "band"), [("-0.5", "critical"), ("0.0999", "critical"),
                                           ("0.10", "at risk"), ("0.1999", "at risk"),
                                           ("0.20", "watch"), ("0.30", "healthy")])
def test_bands(pct: str, band: str) -> None:
    assert V2_14.health(Decimal(pct)) == band


def test_month_rate_is_the_mean_of_daily_rates() -> None:
    r = margin.month_rate([(date(2026, 10, 2), Decimal("0.86")),
                           (date(2026, 10, 1), Decimal("0.84"))])
    assert r is not None
    assert r.rate == Decimal("0.85")
    assert (r.first_day, r.last_day, r.days) == (date(2026, 10, 1), date(2026, 10, 2), 2)
    assert margin.month_rate([]) is None


def test_rate_card_versions_are_effective_dated() -> None:
    assert rate_card_on(date(2026, 10, 31)) is V2_14
    assert rate_card("2026-v2.14") is V2_14
    with pytest.raises(RateCardError):
        rate_card_on(date(2025, 12, 31))
    with pytest.raises(RateCardError):
        rate_card("1999")


def test_a_month_without_billing_data_is_not_captured() -> None:
    for ccy, r in (("EUR", "1"), ("GBP", "0.86")):
        f = margin.compute(inputs(has_cost_data=False, consumption_eur=Decimal(0),
                                  contract_currency=ccy, rate=rate(r), prior_rate=rate(r)),
                           V2_14)
        assert f.not_captured == "no_cost_data"
        assert f.actual_margin is None


@pytest.mark.parametrize("effort", [
    (),  # no ServiceNow items at all: not connected, a mapping gap, a quiet month
    (EffortBucket("Cloud Ops", "incident", Decimal(0), 5, 0),),  # items, zero hours
], ids=["no-items", "zero-hours"])
def test_no_recorded_effort_is_a_gap_not_free_delivery(
        effort: tuple[EffortBucket, ...]) -> None:
    """HLD principle 7: zero effort must not read as zero cost. The same customer
    with its effort recorded is critical; without it the band falls back to the
    modelled basis rather than an actual basis on 0 hours."""
    recorded = margin.compute(inputs(), V2_14)
    gap = margin.compute(inputs(effort=effort), V2_14)
    assert recorded.band_basis == "actual"
    assert recorded.band == "critical"
    assert gap.effort_complete is False
    assert gap.band_basis == "modelled"
    assert gap.margin_pct == (gap.revenue - gap.modelled_cost) / gap.revenue  # type: ignore[operator]
    assert gap.not_captured is None  # actual figures still stored, just not banded on


def test_not_captured_figures_record_missing_effort_as_incomplete() -> None:
    assert margin.compute(inputs(revenue=None, effort=()), V2_14).effort_complete is False


def test_a_provisional_month_has_figures_but_no_band_or_gap() -> None:
    """Month to date: full-month revenue against part of the month's cost would
    band too well. The figures are shown; the band waits for the close."""
    closed = margin.compute(inputs(), V2_14)
    provisional = margin.compute(inputs(provisional=True), V2_14)
    assert provisional.margin_pct == closed.margin_pct
    assert provisional.actual_margin == closed.actual_margin
    assert provisional.band is None
    assert provisional.gap_to_target is None
    assert closed.band is not None
    assert closed.gap_to_target is not None
