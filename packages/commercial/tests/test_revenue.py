"""Revenue & margin per customer (ADR 0038): two currencies, rates in force on the
transaction date, derived ACVs marked modelled, totals and change."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from cloudops_commercial import revenue
from cloudops_commercial.revenue import (
    Consumption,
    Delivery,
    Figures,
    MarginMonth,
    RateBook,
    Sow,
    Value,
)

AT = datetime(2026, 10, 5, tzinfo=UTC)
RATE_ROWS = [
    ("GBP", "EUR", date(2026, 1, 2), Decimal("1.10")),
    ("GBP", "EUR", date(2026, 8, 3), Decimal("1.20")),
    ("GBP", "EUR", date(2026, 8, 31), Decimal("1.20")),
    ("GBP", "EUR", date(2026, 9, 1), Decimal("1.15")),
    ("GBP", "EUR", date(2026, 9, 15), Decimal("1.35")),
    ("USD", "GBP", date(2026, 8, 1), Decimal("0.80")),
    ("USD", "GBP", date(2026, 9, 10), Decimal("0.75")),
    ("USD", "EUR", date(2026, 8, 1), Decimal("0.90")),
    ("USD", "EUR", date(2026, 9, 10), Decimal("0.85")),
]
RATES = RateBook(RATE_ROWS)


def amt(v: Value | None) -> Decimal:
    assert v is not None
    return v.amount


def got(v: Value | None) -> Value:
    assert v is not None
    return v


def rep(f: Figures | None) -> Figures:
    assert f is not None
    return f


def _sow(**kw: object) -> Sow:
    base: dict[str, object] = {
        "reference": "SOW-1", "currency": "GBP", "effective_from": date(2026, 1, 15),
        "csp": True, "monthly_manage_revenue": Decimal("10000"), "hosting_acv": None,
        "manage_acv": None, "professional_services": Decimal("5000"),
        "consumption_uplift": Decimal("0.08"), "entered_at": AT}
    base.update(kw)
    return Sow(**base)  # type: ignore[arg-type]


def _month(period: date, through: date, rev: str, cost: str, modelled: bool = False,
           ccy: str = "GBP") -> MarginMonth:
    return MarginMonth(period, through, ccy, Decimal(rev), Decimal(cost), modelled, AT)


AUG = _month(date(2026, 8, 1), date(2026, 8, 31), "10000", "7000")
SEP = _month(date(2026, 9, 1), date(2026, 9, 30), "10000", "8000")
USAGE = [Consumption(date(2026, 8, 20), "USD", Decimal("1000")),
         Consumption(date(2026, 9, 20), "USD", Decimal("2000"))]


def test_margin_is_computed_in_the_contract_currency_then_converted_month_by_month() -> None:
    row = revenue.customer(2, [AUG, SEP], USAGE, _sow(), RATES, "EUR")
    c = row.contract
    assert c.currency == "GBP"
    assert amt(c.manage_revenue) == Decimal("10000")
    assert amt(c.manage_cost) == Decimal("7500")
    assert amt(c.gross_margin) == Decimal("2500")
    assert amt(c.gross_margin_pct) == Decimal("0.25")
    r = rep(row.reported)
    assert r.currency == "EUR"
    # Each month at its own average rate: August 1.20, September (1.15 + 1.35) / 2.
    aug_rate, sep_rate = Decimal("1.20"), Decimal("1.25")
    assert amt(r.manage_revenue) == (Decimal("10000") * aug_rate
                                     + Decimal("10000") * sep_rate) / 2
    assert amt(r.gross_margin) == (Decimal("3000") * aug_rate + Decimal("2000") * sep_rate) / 2
    # The percentage is the contract currency's, whatever the viewer's currency.
    assert r.gross_margin_pct == c.gross_margin_pct


def test_consumption_converts_at_the_rate_in_force_on_each_usage_date() -> None:
    row = revenue.customer(2, [AUG, SEP], USAGE, _sow(), RATES, "EUR")
    # 2026-08-20 uses the rate published 08-01; 2026-09-20 the one published 09-10.
    assert amt(row.contract.cloud_consumption) == (Decimal("1000") * Decimal("0.80")
                                                   + Decimal("2000") * Decimal("0.75")) / 2
    assert amt(rep(row.reported).cloud_consumption) == (
        Decimal("1000") * Decimal("0.90") + Decimal("2000") * Decimal("0.85")) / 2


def test_todays_rate_is_never_used_on_history() -> None:
    later = RateBook([*RATE_ROWS, ("USD", "GBP", date(2026, 10, 30), Decimal("0.50"))])
    a = revenue.customer(2, [AUG, SEP], USAGE, _sow(), RATES, None)
    b = revenue.customer(2, [AUG, SEP], USAGE, _sow(), later, None)
    assert a.contract.cloud_consumption == b.contract.cloud_consumption


def test_sow_figures_convert_at_the_rate_in_force_when_the_version_took_effect() -> None:
    row = revenue.customer(2, [AUG, SEP], USAGE, _sow(manage_acv=Decimal("130000")), RATES,
                           "EUR")
    r = rep(row.reported)
    assert amt(r.manage_acv) == Decimal("130000") * Decimal("1.10")  # in force 2026-01-15
    assert amt(r.professional_services) == Decimal("5000") * Decimal("1.10")
    assert not got(r.manage_acv).modelled


def test_acvs_not_entered_are_derived_and_marked_modelled() -> None:
    c = revenue.customer(2, [AUG, SEP], USAGE, _sow(), RATES, None).contract
    assert got(c.manage_acv).modelled
    assert amt(c.manage_acv) == Decimal("120000")
    assert got(c.hosting_acv).modelled
    assert amt(c.hosting_acv) == 12 * amt(c.cloud_consumption) * Decimal("1.08")


@pytest.mark.parametrize(("csp", "uplift", "gap"), [
    (False, Decimal("0.08"), "not_csp"), (None, Decimal("0.08"), "csp_not_recorded"),
    (True, None, "no_uplift")])
def test_hosting_acv_is_not_guessed(csp: bool | None, uplift: Decimal | None, gap: str) -> None:
    row = revenue.customer(2, [AUG, SEP], USAGE, _sow(csp=csp, consumption_uplift=uplift),
                           RATES, None)
    assert row.contract.hosting_acv is None
    assert row.gaps["hosting_acv"] == gap


def test_modelled_cost_months_mark_the_cost_modelled() -> None:
    sep = _month(date(2026, 9, 1), date(2026, 9, 30), "10000", "9000", modelled=True)
    c = revenue.customer(2, [AUG, sep], USAGE, _sow(), RATES, None).contract
    assert got(c.manage_cost).modelled
    assert not got(c.manage_revenue).modelled


def test_a_missing_rate_is_absent_never_one() -> None:
    row = revenue.customer(2, [AUG, SEP], USAGE, _sow(), RATES, "JPY")
    assert rep(row.reported).manage_revenue is None
    assert row.gaps["reported.manage_revenue"] == "no_fx_rate"
    assert row.contract.manage_revenue is not None


def test_no_sow_and_partial_range_are_named() -> None:
    row = revenue.customer(3, [AUG], USAGE, None, RATES, None)
    assert row.contract.currency == "GBP"
    assert row.gaps["manage_revenue"] == "partial_range"
    assert row.gaps["manage_acv"] == "no_sow"
    assert revenue.customer(1, [], [], None, RATES, None).gaps == {"row": "no_sow"}


def test_totals_sum_per_currency_and_weigh_margin_by_revenue() -> None:
    a = revenue.customer(2, [AUG, SEP], USAGE, _sow(), RATES, "EUR")
    eur = _month(date(2026, 9, 1), date(2026, 9, 30), "20000", "10000", ccy="EUR")
    b = revenue.customer(1, [eur], [], _sow(currency="EUR", csp=False), RATES, "EUR")
    assert [t.currency for t in revenue.totals([a.contract, b.contract])] == ["EUR", "GBP"]
    [total] = revenue.totals([rep(a.reported), rep(b.reported)])
    assert total.customers == 2
    f = total.figures
    assert amt(f.manage_revenue) == (amt(rep(a.reported).manage_revenue)
                                     + amt(rep(b.reported).manage_revenue))
    assert amt(f.gross_margin_pct) == amt(f.gross_margin) / amt(f.manage_revenue)
    assert total.missing["hosting_acv"] == 1  # b is not CSP
    assert got(f.hosting_acv).modelled


def test_change_is_current_minus_compare_in_one_currency() -> None:
    aug = revenue.customer(1, [AUG], USAGE[:1], _sow(), RATES, None).contract
    sep = revenue.customer(1, [SEP], USAGE[1:], _sow(), RATES, None).contract
    delta = rep(revenue.change(sep, aug))
    assert amt(delta.manage_cost) == Decimal("1000")
    assert amt(delta.gross_margin_pct) == Decimal("-0.1")
    eur = revenue.customer(1, [AUG], [], _sow(currency="EUR"), RATES, None).contract
    assert revenue.change(sep, eur) is None


# --- total revenue and delivered project price ------------------------------------------------


def test_total_revenue_is_hosting_plus_manage_plus_services_monthly() -> None:
    row = revenue.customer(2, [AUG, SEP], USAGE, _sow(), RATES, None)
    c = row.contract
    hosting = amt(c.cloud_consumption) * Decimal("1.08")
    services = Decimal("5000") / 12  # open-ended SOW: a year
    assert amt(c.total_revenue) == Decimal("10000") + hosting + services
    assert got(c.total_revenue).modelled
    assert row.gaps["professional_services"] == "ps_from_sow"


def test_total_revenue_without_csp_has_no_hosting_and_spreads_services_over_the_term() -> None:
    sow = _sow(csp=False, term_start=date(2026, 1, 1), term_end=date(2027, 12, 31))
    c = revenue.customer(2, [AUG, SEP], USAGE, sow, RATES, None).contract
    assert amt(c.total_revenue) == Decimal("10000") + Decimal("5000") / 24


def test_total_revenue_with_csp_unknown_is_partial_and_says_why() -> None:
    row = revenue.customer(2, [AUG, SEP], USAGE, _sow(csp=None), RATES, None)
    assert amt(row.contract.total_revenue) == Decimal("10000") + Decimal("5000") / 12
    assert got(row.contract.total_revenue).modelled
    assert row.gaps["total_revenue"] == "csp_not_recorded"


def test_delivered_project_tasks_replace_the_sow_figure_at_the_delivery_days_rate() -> None:
    delivered = [Delivery(date(2026, 8, 20), "USD", Decimal("4000"), AT, False),
                 Delivery(date(2026, 9, 20), "USD", Decimal("2000"), AT, True)]
    row = revenue.customer(2, [AUG, SEP], USAGE, _sow(csp=False), RATES, "EUR", delivered)
    c = row.contract
    ps = Decimal("4000") * Decimal("0.80") + Decimal("2000") * Decimal("0.75")
    assert amt(c.professional_services) == ps
    assert got(c.professional_services).source == "servicenow_project_tasks"
    assert got(c.professional_services).modelled  # one delivery is provisional
    assert "professional_services" not in row.gaps
    assert amt(c.total_revenue) == Decimal("10000") + ps / 2
    assert amt(rep(row.reported).professional_services) == (
        Decimal("4000") * Decimal("0.90") + Decimal("2000") * Decimal("0.85"))


def test_total_revenue_needs_manage_revenue() -> None:
    row = revenue.customer(1, [], USAGE, _sow(), RATES, None)
    assert row.contract.total_revenue is None
    assert row.gaps["total_revenue"] == "no_margin"
