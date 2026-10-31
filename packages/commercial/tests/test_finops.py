"""Reconciliation, anomalies and allocation, each in at least two currencies."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from cloudops_commercial import finops
from cloudops_commercial.finops import Allocation, Threshold

DAY = Threshold(Decimal("0.30"), Decimal(100))
MONTH = Threshold(Decimal("0.20"), Decimal(1000))


@pytest.mark.parametrize("ccy", ["EUR", "GBP", "USD"])
def test_drift_at_the_boundary(ccy: str) -> None:
    ok = finops.reconcile(Decimal("100500"), ccy, Decimal("100000"), ccy)
    assert ok.status == "verified"
    assert ok.drift == Decimal("0.005")
    bad = finops.reconcile(Decimal("100500.01"), ccy, Decimal("100000"), ccy)
    assert bad.status == "unverified"
    assert bad.reason == "drift"
    under = finops.reconcile(Decimal("99400"), ccy, Decimal("100000"), ccy)
    assert under.status == "unverified"  # drift in either direction


def test_different_currencies_never_verify() -> None:
    r = finops.reconcile(Decimal(100), "EUR", Decimal(100), "GBP")
    assert r.status == "unverified"
    assert r.reason == "currency_mismatch"
    assert r.drift is None


def test_zero_reference() -> None:
    assert finops.reconcile(Decimal(0), "USD", Decimal(0), "USD").status == "verified"
    r = finops.reconcile(Decimal(5), "EUR", Decimal(0), "EUR")
    assert r.status == "unverified"
    assert r.reason == "reference_zero"


def test_negative_totals_compare_by_magnitude() -> None:
    r = finops.reconcile(Decimal("-1000"), "GBP", Decimal("-1004"), "GBP")
    assert r.status == "verified"


def _series(values: list[str], start: date = date(2026, 10, 1)) -> dict[date, Decimal]:
    return {start + timedelta(days=i): Decimal(v) for i, v in enumerate(values)}


@pytest.mark.parametrize("ccy", ["EUR", "USD"])
def test_day_over_day_spike_and_drop(ccy: str) -> None:
    s = _series(["1000"] * 7 + ["1400"])
    a = finops.day_over_day(s, date(2026, 10, 8), DAY)
    assert a is not None
    assert a.direction == "spike"
    assert a.baseline == Decimal(1000)
    assert a.ratio == Decimal("0.4")
    s = _series(["1000"] * 7 + ["500"])
    d = finops.day_over_day(s, date(2026, 10, 8), DAY)
    assert d is not None
    assert d.direction == "drop"


def test_day_over_day_needs_both_thresholds() -> None:
    small = _series(["100"] * 7 + ["190"])  # +90%, but only 90 units
    assert finops.day_over_day(small, date(2026, 10, 8), DAY) is None
    big = _series(["10000"] * 7 + ["12000"])  # 2000 units, but only +20%
    assert finops.day_over_day(big, date(2026, 10, 8), DAY) is None


def test_day_over_day_needs_a_baseline() -> None:
    assert finops.day_over_day(_series(["1", "1000"]), date(2026, 10, 2), DAY) is None
    assert finops.day_over_day(_series(["1"] * 8), date(2026, 11, 1), DAY) is None


def test_zero_baseline_spike() -> None:
    a = finops.day_over_day(_series(["0", "0", "0", "500"]), date(2026, 10, 4), DAY)
    assert a is not None
    assert a.ratio is None
    assert a.direction == "spike"


@pytest.mark.parametrize("ccy", ["GBP", "EUR"])
def test_month_over_month_run_rate(ccy: str) -> None:
    a = finops.month_over_month(Decimal(12000), date(2026, 10, 10), 10, 31, Decimal(30000),
                                MONTH)
    assert a is not None
    assert a.observed == Decimal(37200)
    assert a.direction == "spike"
    assert finops.month_over_month(Decimal(10000), date(2026, 10, 10), 10, 31,
                                   Decimal(30000), MONTH) is None
    assert finops.month_over_month(Decimal(1), date(2026, 10, 1), 1, 31, None, MONTH) is None


def test_allocation_normalises_keys_not_values() -> None:
    for key in ("costcentre", "CostCenter", "cost-centre", "Cost Centre"):
        a = finops.allocate({key: " CC-1042 ", "Application": "Billing"})
        assert a == Allocation("Billing", "CC-1042")
    assert finops.allocate({"owner": "x"}) == Allocation(None, None)
    assert finops.allocate(None, {}) == Allocation(None, None)


def test_snapshot_tags_win_then_resource_tags_fill_in() -> None:
    a = finops.allocate({"app": "Shop"}, {"application": "Old", "CostCenter": "CC-9"})
    assert a == Allocation("Shop", "CC-9")


def test_unallocated_is_a_figure() -> None:
    rows = [(Allocation("A", "CC-1"), Decimal("70")), (Allocation("A", None), Decimal("20")),
            (None, Decimal("10"))]
    cc = finops.unallocated(rows, "cost_centre")
    assert (cc.allocated, cc.unallocated) == (Decimal(70), Decimal(30))
    assert cc.unallocated_share == Decimal("0.3")
    app = finops.unallocated(rows, "application")
    assert app.unallocated == Decimal(10)
    assert finops.unallocated([], "application").unallocated_share is None


def test_only_consumption_feeds_margin() -> None:
    assert frozenset({"consumption"}) == finops.FEEDS_MARGIN
    assert set(finops.CHARGE_CLASSES) >= {"consumption", "licence", "marketplace", "support",
                                          "reservation", "savings_plan", "tax", "credit"}
