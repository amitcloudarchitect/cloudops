"""The home sections' rules, without a database (ADR 0038): the date range, the
size band boundaries, the modelled health score, the top-10 order, and money in
two currencies, converted per line and never summed across currencies unconverted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from cloudops_api.home_sections import (
    HEALTH_POLICY,
    Health,
    HealthInputs,
    SizePolicy,
    _Hosting,
    _Managed,
    _revenue_section,
    health_score,
    managed_margin,
    rank_customers,
    resolve_range,
    size_band,
    spend_month,
)
from cloudops_api.pipeline import ApiError

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
POLICY = SizePolicy(1, "EUR", Decimal("10000"), Decimal("50000"), 200, 1000, NOW)

# --- the range -------------------------------------------------------------------------


def test_the_default_range_is_the_month_to_date() -> None:
    assert resolve_range(None, None, date(2026, 11, 17)) == (date(2026, 11, 1),
                                                             date(2026, 11, 17))
    assert resolve_range(None, date(2026, 10, 31), date(2026, 11, 17)) == (
        date(2026, 10, 1), date(2026, 10, 31))


@pytest.mark.parametrize(("start", "end"), [
    (date(2026, 10, 2), date(2026, 10, 1)),  # ends before it starts
    (date(2025, 1, 1), date(2026, 1, 2)),  # 367 days: more than a year
])
def test_a_range_backwards_or_over_a_year_is_refused(start: date, end: date) -> None:
    with pytest.raises(ApiError) as refused:
        resolve_range(start, end, date(2026, 11, 1))
    assert (refused.value.status, refused.value.code) == (422, "invalid_range")


def test_a_single_day_and_a_whole_year_are_ranges() -> None:
    assert resolve_range(date(2026, 3, 3), date(2026, 3, 3), date(2026, 11, 1))
    assert resolve_range(date(2025, 1, 1), date(2025, 12, 31), date(2026, 11, 1))


# --- size bands ------------------------------------------------------------------------


@pytest.mark.parametrize(("spend", "expected"), [
    (Decimal("9999.999999"), "small"),
    (Decimal("10000"), "medium"),  # Medium from 10k, inclusive
    (Decimal("50000"), "medium"),  # Large only above 50k
    (Decimal("50000.000001"), "large"),
])
def test_spend_band_boundaries(spend: Decimal, expected: str) -> None:
    assert size_band(spend, 0, POLICY)[0] == expected


@pytest.mark.parametrize(("resources", "expected"), [
    (0, "small"), (199, "small"), (200, "medium"), (1000, "medium"), (1001, "large"),
])
def test_resource_band_boundaries(resources: int, expected: str) -> None:
    assert size_band(Decimal(0), resources, POLICY)[1] == expected


def test_the_band_is_the_higher_of_the_two() -> None:
    assert size_band(Decimal("60000"), 10, POLICY) == ("large", "small", "large")
    assert size_band(Decimal("100"), 1500, POLICY) == ("small", "large", "large")
    assert size_band(Decimal("20000"), 300, POLICY) == ("medium", "medium", "medium")
    # Spend not known: the resources decide, and the spend band says it is unknown.
    assert size_band(None, 250, POLICY) == (None, "medium", "medium")


def test_edited_thresholds_move_the_boundaries() -> None:
    tighter = SizePolicy(2, "EUR", Decimal("5000"), Decimal("20000"), 50, 100, NOW)
    assert size_band(Decimal("20000.01"), 0, tighter)[0] == "large"
    assert size_band(Decimal("5000"), 0, tighter)[0] == "medium"
    assert size_band(None, 101, tighter)[2] == "large"


@pytest.mark.parametrize(("end", "month"), [
    (date(2026, 10, 31), date(2026, 10, 1)),  # the end closes its month
    (date(2026, 11, 17), date(2026, 10, 1)),  # mid-month: the month before
    (date(2026, 3, 1), date(2026, 2, 1)),
    (date(2024, 2, 29), date(2024, 2, 1)),  # a leap day closes February
])
def test_the_spend_month_is_the_last_whole_month(end: date, month: date) -> None:
    assert spend_month(end) == month


# --- the health score ------------------------------------------------------------------


def test_a_quiet_customer_scores_100() -> None:
    h = health_score(HealthInputs(0, 0, 10, 0, 0, 0))
    assert (h.score, h.band) == (Decimal(100), "healthy")


def test_each_part_is_capped() -> None:
    h = health_score(HealthInputs(p1=5, p2=5, critical=4, unavailable=4,
                                  major_vulnerabilities=50, security_events=9))
    assert h.penalties == {"p1_p2": Decimal(40), "availability": Decimal(20),
                           "vulnerabilities": Decimal(20), "security_events": Decimal(20)}
    assert (h.score, h.band) == (Decimal(0), "critical")


def test_penalties_add_up() -> None:
    # One P1 (20), one P2 (10), a quarter of critical resources down (5), three
    # major vulnerabilities (6), one high security event (5): 100 - 46 = 54.
    h = health_score(HealthInputs(1, 1, 4, 1, 3, 1))
    assert h.score == Decimal(54)
    assert h.band == "at-risk"


@pytest.mark.parametrize(("inputs", "score", "band"), [
    (HealthInputs(0, 2, 0, 0, 0, 0), 80, "healthy"),  # healthy from 80
    (HealthInputs(0, 3, 0, 0, 0, 0), 70, "watch"),
    (HealthInputs(0, 4, 0, 0, 0, 0), 60, "watch"),  # watch from 60
    (HealthInputs(0, 4, 0, 0, 1, 0), 58, "at-risk"),
    (HealthInputs(0, 4, 0, 0, 10, 0), 40, "at-risk"),  # at risk from 40
    (HealthInputs(0, 4, 0, 0, 10, 1), 35, "critical"),
])
def test_health_bands(inputs: HealthInputs, score: int, band: str) -> None:
    h = health_score(inputs)
    assert (h.score, h.band) == (Decimal(score), band)


def test_an_unobserved_part_costs_nothing_and_says_so() -> None:
    h = health_score(HealthInputs(0, 0, critical=0, unavailable=0,
                                  major_vulnerabilities=None, security_events=None))
    assert h.penalties["availability"] is None  # no critical resources in inventory
    assert h.penalties["vulnerabilities"] is None  # not permitted to the caller
    assert h.score == Decimal(100)
    assert HEALTH_POLICY.tickets_cap + HEALTH_POLICY.availability_cap \
        + HEALTH_POLICY.vulnerabilities_cap + HEALTH_POLICY.events_cap == 100


# --- the top 10 ------------------------------------------------------------------------


def _row(name: str, score: int, revenue: str | None
         ) -> tuple[UUID, str, Health, HealthInputs, Decimal | None]:
    h = Health(Decimal(score), "healthy", {})
    return (uuid4(), name, h, HealthInputs(0, 0, 0, 0, 0, 0),
            None if revenue is None else Decimal(revenue))


def test_revenue_order_is_largest_first_unknown_last() -> None:
    rows = [_row("Alpha", 90, "100"), _row("Bravo", 50, None), _row("Charlie", 70, "900"),
            _row("Delta", 95, "100")]
    assert [r[1] for r in rank_customers(rows, "revenue")] == ["Charlie", "Alpha", "Delta",
                                                               "Bravo"]


def test_health_order_is_least_healthy_first_then_revenue() -> None:
    rows = [_row("Alpha", 90, "100"), _row("Bravo", 50, None), _row("Charlie", 50, "900"),
            _row("Delta", 95, "100")]
    assert [r[1] for r in rank_customers(rows, "health")] == ["Charlie", "Bravo", "Alpha",
                                                              "Delta"]


# --- money in two currencies -----------------------------------------------------------

T1, T2 = uuid4(), uuid4()


def _hosting() -> list[_Hosting]:
    # T1 in EUR (rate 1), T2 in USD at 0.9 on its usage dates; T2's price has a rate.
    return [
        _Hosting(T1, "azure", "EUR", Decimal("1000.000000"), date(2026, 10, 31),
                 Decimal("1000.000000"), Decimal("1080.000000"), Decimal("1080.000000")),
        _Hosting(T2, "aws", "USD", Decimal("2000.000000"), date(2026, 10, 30),
                 Decimal("1800.000000"), Decimal("2200.000000"), Decimal("1980.000000")),
    ]


def _managed(usd_rate: Decimal | None = Decimal("0.9")) -> list[_Managed]:
    return [
        _Managed(T1, date(2026, 10, 1), True, "EUR", Decimal("5000"), Decimal("3500"), False,
                 NOW, Decimal(1)),
        _Managed(T2, date(2026, 10, 1), False, "USD", Decimal("4000"), Decimal("3000"), True,
                 NOW, usd_rate),
    ]


def test_margin_is_computed_in_the_contract_currency_then_converted() -> None:
    assert managed_margin(Decimal("4000"), Decimal("3000"), Decimal("0.9")) == (
        Decimal("1000"), Decimal("900.0"))
    assert managed_margin(Decimal("4000"), Decimal("3000"), None) == (Decimal("1000"), None)


def test_lines_stay_in_their_currency_and_totals_are_converted() -> None:
    body = _revenue_section(date(2026, 10, 1), date(2026, 10, 31), NOW, "EUR", True,
                            _hosting(), _managed())
    assert body.hosting is not None
    assert body.managed is not None
    hosting = {h.currency: h for h in body.hosting}
    assert set(hosting) == {"EUR", "USD"}
    assert hosting["USD"].cost.value == Decimal("2000.000000")
    assert hosting["USD"].cost.unit == "USD"
    assert hosting["USD"].cost_converted is not None
    assert hosting["USD"].cost_converted.unit == "EUR"
    managed = {m.currency: m for m in body.managed}
    assert managed["USD"].margin.value == Decimal(1000)
    assert managed["USD"].margin.is_modelled is True  # the engine modelled that cost
    assert managed["EUR"].margin.is_modelled is False
    assert managed["USD"].provisional.value == 1
    t = body.totals
    assert t is not None
    assert t.currency == "EUR"
    assert t.hosting_cost is not None
    assert t.hosting_cost.value == Decimal("2800.000000")
    assert t.hosting_price is not None
    assert t.hosting_price.value == Decimal("3060.000000")
    assert t.managed_revenue is not None
    assert t.managed_revenue.value == Decimal("8600.0")
    assert t.managed_margin is not None
    assert t.managed_margin.value == Decimal("2400.0")
    assert t.revenue is not None
    assert t.revenue.value == Decimal("11660.0")
    # (3060 - 2800) + 2400 = 2660, over 11660.
    assert t.margin is not None
    assert t.margin.value == Decimal("2660.0")
    assert t.margin_share is not None
    assert t.margin_share.value == Decimal("2660.0") / Decimal("11660.0")
    assert t.margin.is_modelled is True
    # Not rounded on the way out: rounding happens once, at presentation.
    assert str(t.hosting_cost.value) == "2800.000000"


def test_a_missing_rate_loses_the_total_never_half_of_it() -> None:
    body = _revenue_section(date(2026, 10, 1), date(2026, 10, 31), NOW, "EUR", True,
                            _hosting(), _managed(usd_rate=None))
    t = body.totals
    assert t is not None
    assert t.managed_revenue is None
    assert t.revenue is None
    assert t.margin is None
    assert t.hosting_cost is not None  # the hosting lines all had rates
    assert body.managed is not None
    usd = next(m for m in body.managed if m.currency == "USD")
    assert usd.revenue.value == Decimal(4000)  # still shown in its own currency
    assert usd.revenue_converted is None


def test_without_a_reporting_currency_there_is_no_total() -> None:
    body = _revenue_section(date(2026, 10, 1), date(2026, 10, 31), NOW, None, True,
                            _hosting(), _managed())
    assert body.totals is None


def test_without_commercial_read_only_cost_is_shown() -> None:
    body = _revenue_section(date(2026, 10, 1), date(2026, 10, 31), NOW, "EUR", False,
                            _hosting(), [])
    assert body.managed is None
    assert body.projects is None
    assert body.hosting is not None
    assert all(h.price is None and h.price_converted is None for h in body.hosting)
    assert body.totals is not None
    assert body.totals.hosting_price is None
    assert body.totals.revenue is None
    assert body.totals.hosting_cost is not None


def test_project_prices_say_there_is_no_source_yet() -> None:
    body = _revenue_section(date(2026, 10, 1), date(2026, 10, 31), NOW, "EUR", True,
                            _hosting(), _managed())
    assert body.projects is not None
    assert (body.projects.available, body.projects.reason) == (False, "no_data_source")
    assert body.projects.new_project_price is None
