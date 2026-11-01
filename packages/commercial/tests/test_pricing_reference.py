"""The pricing release gate (LLD, CI/CD: "pricing regression against the reference
case"; HLD: "the pricing engine reproducing its reference case exactly").

The workbook's reference case — 321 critical CIs, 24x7, Gold, India delivery,
EUR 43,856/mo consumption — must come out of the ``Decimal`` port equal to the
reference engine's figures at six decimal places. Then the port and the
reference are run side by side over every coverage, support model, location and
a spread of inventories and consumptions, so a slip in a branch the reference
case does not reach still fails.

CI runs this file as the ``pricing-regression`` job (``.github/workflows/ci.yml``).
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cloudops_commercial import pricing
from cloudops_commercial.ratecard import from_document

# Version 1 as migration 0031 seeds it: the workbook's constants (ADR 0033).
V2_14 = from_document(json.loads(
    (Path(__file__).resolve().parents[3] / "db/migrations/data/rate_card_2026-v2.14.json")
    .read_text(encoding="utf-8")))

SIX: Decimal = Decimal("0.000001")
REFERENCE = Path(__file__).resolve().parents[3] / "reference" / "pricing_engine.py"

REF_INVENTORY = {
    "Azure Virtual Machines / Amazon EC2": 15,
    "App Service / AWS Elastic Beanstalk": 260,
    "Azure Kubernetes Service (AKS) / Amazon EKS": 46,
}


def six(value: Decimal | float) -> Decimal:
    return Decimal(str(value)).quantize(SIX, rounding=ROUND_HALF_EVEN)


@pytest.fixture(scope="module")
def ref() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reference_pricing_engine", REFERENCE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def _engagement(**kw: Any) -> pricing.Engagement:
    return pricing.Engagement(customer="Reference (workbook)", inventory=REF_INVENTORY,
                              monthly_consumption_eur=Decimal("43856"), **kw)


# --- the reference case, to six places (the workbook's own figures) ---------------------


def test_unit_prices() -> None:
    assert six(pricing.unit_price("Azure Virtual Machines / Amazon EC2", "24x7", "Gold",
                                  V2_14)) == six(18.502633102565962)
    assert six(pricing.unit_price("Azure Kubernetes Service (AKS) / Amazon EKS", "24x7",
                                  "Gold", V2_14)) == six(77.09430459402485)


def test_reference_case_quote() -> None:
    q = pricing.quote(_engagement(), V2_14)
    assert six(q.service_cost_eur) == six(8634.562114530783)
    assert six(q.base_pack_eur) == Decimal("2863.952000")
    assert six(q.cloud_services_cost_eur) == six(11498.514114530783)
    assert six(q.platform_cost_eur) == Decimal("5597.304000")
    assert six(q.total_monthly_eur) == six(17095.818114530783)
    assert six(q.price_per_ci_eur) == six(53.258000356793715)
    assert six(q.fte_count) == six(6.4348664156948585)
    assert q.slab == "301 - 500 CIs"
    assert q.critical_cis == 321
    assert q.transition_weeks == 12
    assert q.transition_effort_days == 60
    assert q.rate_card_version == "2026-v2.14"


def test_reference_case_team() -> None:
    q = pricing.quote(_engagement(), V2_14)
    assert q.team == {
        "Full Stack Cloud Engineer": Decimal("2.896"),
        "Cloud Specialist": Decimal("2.252"),
        "Cloud Architect": Decimal("0.643"),
        "CloudOps Manager": Decimal("0.322"),
        "Cloud Delivery Manager": Decimal("0.322"),
    }


def test_reference_case_margin_against_a_sow(ref: ModuleType) -> None:
    e = _engagement(sow_monthly_revenue_eur=Decimal("17095.82"))
    q = pricing.quote(e, V2_14)
    r_e = ref.Engagement(customer="r", inventory=REF_INVENTORY, monthly_consumption_eur=43_856.0,
                         sow_monthly_revenue_eur=17_095.82)
    r_q = ref.quote(r_e)
    for hours in (None, Decimal(1100)):
        m = pricing.margin(e, q, V2_14, actual_effort_hours=hours)
        r_m = ref.margin(r_e, r_q, actual_effort_hours=None if hours is None else float(hours))
        assert six(m.modelled_cost_eur) == six(r_m.modelled_cost_eur)
        assert six(m.gross_margin_pct) == six(r_m.gross_margin_pct)
        assert m.health == r_m.health
        if hours is not None:
            assert m.actual_cost_eur is not None
            assert m.variance_vs_model_eur is not None
            assert six(m.actual_cost_eur) == six(r_m.actual_cost_eur)
            assert six(m.variance_vs_model_eur) == six(r_m.variance_vs_model_eur)


def test_margin_without_a_sow_is_not_captured() -> None:
    e = _engagement()
    with pytest.raises(pricing.SowNotCapturedError):
        pricing.margin(e, pricing.quote(e, V2_14), V2_14)


def test_slab_headroom_matches_the_reference(ref: ModuleType) -> None:
    for cis in (0, 40, 41, 50, 51, 145, 300, 321, 495, 1000, 1001, 5000):
        h = pricing.slab_headroom(cis, V2_14)
        r = ref.slab_headroom(cis)
        assert (h.current_slab, h.next_slab, h.cis_until_next_slab, h.repricing_due) == (
            r["current_slab"], r["next_slab"], r["cis_until_next_slab"], r["repricing_due"])


# --- the port against the reference, across every branch ---------------------------------

INVENTORIES = [
    REF_INVENTORY,
    {"AD Domain Services / AWS Managed Microsoft AD": 2, "Fileshare / Amazon FSx": 7,
     "Azure CDN / Amazon CloudFront": 30},
    {"Web Application Firewall / AWS WAF": 1},
    {"MS SQL DB / Azure SQL / Amazon RDS": 600, "Azure Functions / AWS Lambda": 450},
    {"Azure Virtual Machines / Amazon EC2": 0},
]
CONSUMPTIONS = ["0", "999.99", "1000", "4321.5", "25000", "60000.25", "99999", "250000"]


@pytest.mark.parametrize(("coverage", "support", "location"), list(itertools.product(
    ("8x5", "16x5", "24x7"), ("Bronze", "Silver", "Gold"), ("India", "Budapest"))))
def test_port_matches_reference_everywhere(
    ref: ModuleType, coverage: Any, support: Any, location: Any
) -> None:

    for inventory, consumption, corestack in itertools.product(
            INVENTORIES, CONSUMPTIONS, (True, False)):
        e = pricing.Engagement("c", inventory, Decimal(consumption), coverage, support,
                               location, corestack=corestack)
        r = ref.Engagement("c", inventory, float(consumption), coverage, support, location,
                           corestack=corestack)
        q, r_q = pricing.quote(e, V2_14), ref.quote(r)
        for name in ("service_cost_eur", "base_pack_eur", "cloud_services_cost_eur",
                     "platform_cost_eur", "total_monthly_eur", "price_per_ci_eur",
                     "fte_count"):
            assert six(getattr(q, name)) == six(getattr(r_q, name)), (name, e)
        assert (q.slab, q.transition_weeks) == (r_q.slab, r_q.transition_weeks)
        assert {k: six(v) for k, v in q.team.items()} == {
            k: six(v) for k, v in r_q.team.items()}


def test_the_rate_card_holds_the_reference_constants(ref: ModuleType) -> None:
    """Digit for digit: a constant edited in one place and not the other fails."""
    assert V2_14.blended_day_rate == Decimal(repr(ref.BLENDED_DAY_RATE_EUR))
    assert {k: Decimal(v) for k, v in ref.COMPLEXITY_MINUTES.items()} == dict(
        V2_14.complexity_minutes)
    assert {k: Decimal(repr(v)) for k, v in ref.SERVICE_MULTIPLIER.items()} == dict(
        V2_14.service_multiplier)
    assert dict(V2_14.resource_complexity) == ref.RESOURCE_COMPLEXITY
    assert {k: Decimal(repr(v)) for k, v in ref.PEOPLE_COST_EUR.items()} == dict(
        V2_14.people_cost)
    assert {k: Decimal(repr(v)) for k, v in ref.TOOL_COST_PER_CI_EUR.items()} == dict(
        V2_14.tool_cost_per_ci)
    assert Decimal(repr(ref.CORESTACK_CONSUMPTION_RATE)) == V2_14.corestack_consumption_rate
    assert Decimal(repr(ref.WORKING_DAYS_PER_MONTH)) == V2_14.working_days_per_month
    assert Decimal(repr(ref.BASE_PACK_FLOOR_EUR)) == V2_14.base_pack_floor
    assert [(lo, hi, rate) for lo, hi, rate in ref.BASE_PACK_BANDS[:-1]] == [
        (int(b.lower), int(b.upper), float(b.rate))
        for b in V2_14.base_pack_bands[:-1] if b.upper is not None]
    assert [(s.label, s.lower, s.upper, s.transition_weeks) for s in V2_14.ci_slabs] == list(
        ref.CI_SLABS)
