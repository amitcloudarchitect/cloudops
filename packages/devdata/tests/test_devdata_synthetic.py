"""The synthetic dataset: the shape step 02 asks for, and the mess it promises.

A dataset that is quietly tidy would let identity resolution and currency code
pass here and fail in production, so the mess is asserted, not assumed.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest

from cloudops_devdata.synthetic import (
    COST_DAYS,
    CURRENCIES,
    CiOutcome,
    CostRow,
    Dataset,
    Resolution,
    VulnerabilityRow,
    WorkItemRow,
    generate,
)

ANCHOR = date(2026, 10, 27)


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return generate(ANCHOR, seed=7)


# Shape -------------------------------------------------------------------------------


def test_volumes_match_the_brief(ds: Dataset) -> None:
    assert len(ds.tenants) == 5
    assert len(ds.resources) == 200
    assert len(ds.work_items) == 500
    assert len(ds.vulnerabilities) > 50


def test_cost_covers_exactly_ninety_days_ending_at_the_anchor(ds: Dataset) -> None:
    days = {c.usage_date for c in ds.costs}
    assert min(days) == ANCHOR - timedelta(days=COST_DAYS - 1)
    assert max(days) == ANCHOR
    assert len(days) == COST_DAYS


def test_cost_is_in_three_currencies_and_some_tenants_bill_in_two(ds: Dataset) -> None:
    assert {c.currency for c in ds.costs} == set(CURRENCIES)
    per_tenant = {t.id: {c.currency for c in ds.costs if c.tenant_id == t.id} for t in ds.tenants}
    assert any(len(ccys) > 1 for ccys in per_tenant.values())


def test_a_contract_changed_currency_between_sows(ds: Dataset) -> None:
    by_tenant: dict[object, set[str]] = {}
    for s in ds.sows:
        by_tenant.setdefault(s.tenant_id, set()).add(s.currency)
    assert any(len(c) > 1 for c in by_tenant.values())


def test_money_is_decimal_at_six_places_never_float(ds: Dataset) -> None:
    for c in ds.costs:
        for amount in (c.unblended_cost, c.amortized_cost):
            assert type(amount) is Decimal
            assert amount.as_tuple().exponent == -6
    for s in ds.sows:
        assert type(s.monthly_manage_revenue) is Decimal


def test_cost_includes_credits_reservations_and_unmapped_services(ds: Dataset) -> None:
    classes = Counter(c.charge_class for c in ds.costs)
    assert {"consumption", "licence", "support", "tax", "credit", "reservation"} <= set(classes)
    assert any(c.unblended_cost < 0 for c in ds.costs if c.charge_class == "credit")
    assert any(c.service_canonical is None for c in ds.costs)
    assert any(c.unblended_cost != c.amortized_cost for c in ds.costs)


def test_fx_has_both_directions_and_both_rate_types_for_every_cost_day(ds: Dataset) -> None:
    average = {(f.from_currency, f.to_currency, f.effective_date)
               for f in ds.fx_rates if f.rate_type == "average"}
    for day in {c.usage_date for c in ds.costs}:
        for a in CURRENCIES:
            for b in CURRENCIES:
                if a != b:
                    assert (a, b, day) in average
    assert any(f.rate_type == "closing" for f in ds.fx_rates)
    assert all(type(f.rate) is Decimal and f.rate > 0 for f in ds.fx_rates)


def test_generation_is_deterministic(ds: Dataset) -> None:
    again = generate(ANCHOR, seed=7)
    assert again.resources == ds.resources
    assert again.work_items == ds.work_items
    assert again.costs == ds.costs
    assert generate(ANCHOR, seed=8).work_items != ds.work_items


# Database constraints the rows must satisfy --------------------------------------------


def test_natural_keys_are_unique(ds: Dataset) -> None:
    def unique(keys: list[object]) -> bool:
        return len(keys) == len(set(keys))

    assert unique([(r.account_id, r.native_id) for r in ds.resources])
    assert unique([w.number for w in ds.work_items])
    assert unique([(c.account_id, c.usage_date, c.resource_id, c.service_native, c.charge_class)
                   for c in ds.costs])
    assert unique([(v.tenant_id, v.qid, v.asset_name, v.first_detected)
                   for v in ds.vulnerabilities])
    assert unique([(a.source_system, a.alias) for a in ds.tenant_aliases])
    assert unique([(q.source_system, q.alias) for q in ds.alias_quarantine])
    assert unique([(q.tenant_id, q.source_system, q.external_key)
                   for q in ds.resource_quarantine])
    assert unique([(f.from_currency, f.to_currency, f.rate_type, f.effective_date)
                   for f in ds.fx_rates])


def test_rows_only_reference_their_own_tenant(ds: Dataset) -> None:
    resource_tenant = {r.id: r.tenant_id for r in ds.resources}
    account_tenant = {a.id: a.tenant_id for a in ds.accounts}
    for r in ds.resources:
        assert account_tenant[r.account_id] == r.tenant_id
    rows: list[WorkItemRow | VulnerabilityRow | CostRow] = [
        *ds.work_items, *ds.vulnerabilities, *ds.costs
    ]
    for row in rows:
        if row.resource_id is not None:
            assert resource_tenant[row.resource_id] == row.tenant_id


def test_timestamps_are_timezone_aware_and_not_in_the_future(ds: Dataset) -> None:
    horizon = ds.anchor + timedelta(days=1)
    for w in ds.work_items:
        assert w.opened_at.tzinfo is not None
        assert w.opened_at.date() < horizon
        assert w.closed_at is None or w.closed_at >= w.opened_at


# The mess -------------------------------------------------------------------------------


def test_every_tenant_has_aliases_in_several_sources_and_every_kind_of_variant(
    ds: Dataset,
) -> None:
    for t in ds.tenants:
        sources = {a.source_system for a in ds.tenant_aliases if a.tenant_id == t.id}
        assert len(sources) >= 3
        kinds = {a.resolution for a in ds.alias_truth if a.tenant_code == t.short_code}
        assert kinds == set(Resolution)


def test_ticket_companies_are_only_resolvable_names_and_include_variants(ds: Dataset) -> None:
    truth = {a.text: a for a in ds.alias_truth}
    used = Counter(truth[w.company_raw].resolution for w in ds.work_items)
    assert set(used) == {Resolution.EXACT, Resolution.NORMALISED}
    code = {t.id: t.short_code for t in ds.tenants}
    assert all(truth[w.company_raw].tenant_code == code[w.tenant_id] for w in ds.work_items)


def test_unresolvable_names_are_quarantined_with_counts_never_registered(ds: Dataset) -> None:
    registered = {a.alias for a in ds.tenant_aliases}
    quarantined = {q.alias for q in ds.alias_quarantine}
    expected = {a.text for a in ds.alias_truth if a.resolution is Resolution.QUARANTINE}
    assert quarantined == expected
    assert not quarantined & registered
    assert all(q.occurrence_count >= 1 for q in ds.alias_quarantine)
    assert any(q.suggested_tenant_id is None for q in ds.alias_quarantine)
    assert any(q.suggested_tenant_id is not None for q in ds.alias_quarantine)


def test_short_names_collide_across_and_within_tenants(ds: Dataset) -> None:
    owners: dict[str, set[object]] = {}
    per_tenant = Counter((r.tenant_id, r.name_norm) for r in ds.resources)
    for r in ds.resources:
        owners.setdefault(r.name_norm, set()).add(r.tenant_id)
    assert any(len(t) > 1 for t in owners.values()), "no cross-tenant name collision"
    assert any(n > 1 for n in per_tenant.values()), "no same-tenant name collision"


def test_private_ips_are_reused_across_tenants(ds: Dataset) -> None:
    owners: dict[str, set[object]] = {}
    for r in ds.resources:
        for ip in r.ip_addresses:
            owners.setdefault(ip, set()).add(r.tenant_id)
    assert any(len(t) > 1 for t in owners.values())


def test_ticket_cis_cover_every_outcome_and_ambiguous_ones_stay_unlinked(ds: Dataset) -> None:
    assert {c.outcome for c in ds.ci_truth} == set(CiOutcome)
    linked = {(w.tenant_id, w.configuration_item): w.resource_id for w in ds.work_items}
    for c in ds.ci_truth:
        if c.outcome is not CiOutcome.LINKED:
            tid = next(t.id for t in ds.tenants if t.short_code == c.tenant_code)
            assert linked[(tid, c.value)] is None


def test_unknown_cis_and_unseen_hosts_are_in_resource_quarantine(ds: Dataset) -> None:
    sources = Counter(q.source_system for q in ds.resource_quarantine)
    assert sources["servicenow"] > 0
    assert sources["qualys"] > 0
    assert any(v.resource_id is None for v in ds.vulnerabilities)


def test_tags_are_inconsistent(ds: Dataset) -> None:
    env_keys = {k for r in ds.resources for k in r.tags
                if k.strip().lower() in ("env", "environment")}
    assert len(env_keys) >= 3
    assert any(not r.tags for r in ds.resources)


def test_workaround_spelling_varies(ds: Dataset) -> None:
    codes = {w.closure_code for w in ds.work_items if w.closure_code}
    assert "Resolved (Workaround)" in codes
    assert "Solved (Work Around)" in codes  # escapes the %workaround% flag


def test_planted_injection_and_cross_tenant_bait_exist(ds: Dataset) -> None:
    text = [f"{w.short_description} {w.close_notes or ''}" for w in ds.work_items]
    assert any("ignore all previous instructions" in t.lower() for t in text)
    names = [t.canonical_name for t in ds.tenants]
    assert any(n in t for t in text for n in names)
    assert any("Ignore previous instructions" in v for r in ds.resources for v in r.tags.values())


# --- step 16 ------------------------------------------------------------------------------


def test_one_customer_has_no_sow_and_the_rest_have_targets(ds: Dataset) -> None:
    from cloudops_devdata.synthetic import NO_SOW, tenant_id

    without = {t.id for t in ds.tenants} - {s.tenant_id for s in ds.sows}
    assert without == {tenant_id(code) for code in NO_SOW}
    assert all(s.target_margin is not None for s in ds.sows)
    assert all(s.consumption_uplift is not None for s in ds.sows)


def test_billing_totals_agree_except_the_drift_account_in_two_currencies(ds: Dataset) -> None:
    from cloudops_devdata.synthetic import DRIFT

    ledger: Counter[tuple[object, str]] = Counter()
    for c in ds.costs:
        ledger[(c.account_id, c.invoice_period)] += c.unblended_cost  # type: ignore[assignment]
    current = f"{ds.anchor:%Y-%m}"
    drifting = [b for b in ds.billing_totals
                if Decimal(b.amount) != ledger[(b.account_id, b.invoice_period)]]
    assert len(drifting) == 1
    d = drifting[0]
    assert (d.kind, d.invoice_period) == ("provider_total", current)
    assert d.amount == (ledger[(d.account_id, current)] * DRIFT).quantize(Decimal("0.000001"))
    assert {b.currency for b in ds.billing_totals} >= {"EUR", "GBP", "USD"}
    assert any(b.kind == "invoice" for b in ds.billing_totals)
    assert not any(b.kind == "invoice" and b.invoice_period == current
                   for b in ds.billing_totals)


def test_the_internal_tenant_is_a_tenant_like_any_other() -> None:
    """Step 19 (ADR 0022): the platform's own cost, for the FinOps code to process."""
    from decimal import Decimal

    from cloudops_devdata.seed import INTERNAL_CODE, demo_internal

    ds = generate(date(2026, 10, 30), 7)
    before = {t.id for t in ds.tenants}
    internal = demo_internal(ds)
    assert internal not in before
    assert [t.short_code for t in ds.tenants if t.id == internal] == [INTERNAL_CODE]
    rows = ds.for_tenant(internal)
    assert len(rows["cloudops.cloud_account"]) == 1
    costs = rows["cloudops.cost_fact"]
    assert costs
    assert {c.currency for c in costs} == {"EUR"}
    assert all(c.tags_snapshot == {"application": "cloudops", "costcentre": "CC-PLATFORM"}
               for c in costs)
    assert all(isinstance(c.amortized_cost, Decimal) for c in costs)
    # cost_fact's unique key, NULLs equal: no two rows may collide.
    keys = [(c.usage_date, c.resource_id, c.service_native, c.charge_class) for c in costs]
    assert len(keys) == len(set(keys))
    for b in rows["cloudops.billing_total"]:
        if b.kind == "provider_total":
            assert b.amount == sum((c.unblended_cost for c in costs
                                    if c.invoice_period == b.invoice_period
                                    and c.usage_date <= b.as_of), Decimal(0))
    # Nothing of any customer's moved.
    assert all(ds.for_tenant(t)["cloudops.cost_fact"] for t in before)
