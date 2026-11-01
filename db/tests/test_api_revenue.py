"""Revenue & margin (migration 0036, ADR 0038) against the database: two customers
contracted in GBP and EUR, consumption billed in USD, rates that move between the
months, staff only.

Every figure here is made up for the test. The months (March and April 2025) are
ones no other fixture uses, so the range holds only these two customers' data."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from cloudops_tenancy import Database, Role

from .conftest import Api, Principals, Seeded
from .support import scope

pytestmark = pytest.mark.integration

RANGE = {"from": "2025-03", "to": "2025-04"}
# Average rates. March GBP->EUR averages 1.20, April (1.10 + 1.20) / 2 = 1.15. The SOW
# takes effect on 2025-01-01, when the rate in force is 2024-12-31's 1.18. USD moves
# on 1 April. The May rate is after the range: history never uses it.
RATES = [
    ("GBP", "EUR", "2024-12-31", "1.18"),
    ("GBP", "EUR", "2025-03-03", "1.20"), ("GBP", "EUR", "2025-03-31", "1.20"),
    ("GBP", "EUR", "2025-04-01", "1.10"), ("GBP", "EUR", "2025-04-30", "1.20"),
    ("USD", "GBP", "2025-03-03", "0.80"), ("USD", "GBP", "2025-04-01", "0.75"),
    ("USD", "EUR", "2025-03-03", "0.95"), ("USD", "EUR", "2025-04-01", "0.90"),
    ("USD", "GBP", "2025-05-02", "0.10"),
]


@dataclass(frozen=True)
class Customers:
    gbp: UUID  # CSP, consumption in USD, both ACVs derived
    eur: UUID  # not CSP, no consumption, manage ACV entered
    gbp_account: UUID


def _sow(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "reference": "SOW-REV-TEST", "effective_from": "2025-01-01",
        "term_start": "2025-01-01", "currency": "GBP",
        "service_scope": "Managed cloud operations (test)", "recurring_fee": "120000",
        "billing_frequency": "annual", "consumption_uplift": "0.1",
        "professional_services": "2500", "csp": True,
        "document_uri": "SharePoint: Contracts/Test/SOW-REV-TEST.pdf"}
    return {**body, **kw}


def _snapshot(cx: psycopg.Connection[Any], tenant: UUID, period: str, through: str,
              ccy: str, revenue: str, cost: str) -> None:
    margin = Decimal(revenue) - Decimal(cost)
    cx.execute(
        "INSERT INTO cloudops.margin_snapshot (tenant_id, period, through, status,"
        " rate_card_version, contract_currency, revenue, modelled_cost, actual_cost,"
        " modelled_margin, actual_margin, margin_pct, band_basis, effort_complete)"
        " VALUES (%s, %s, %s, 'closed', '2026-v2.14', %s, %s, %s, %s, %s, %s, %s,"
        " 'actual', true)",
        (tenant, period, through, ccy, revenue, cost, cost, margin, margin,
         margin / Decimal(revenue)))


@pytest.fixture(scope="module")
def customers(seeded: Seeded) -> Iterator[Customers]:
    made = Customers(uuid4(), uuid4(), uuid4())
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for t, name in ((made.gbp, "Revenue test GBP"), (made.eur, "=Revenue test EUR")):
            cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code)"
                       " VALUES (%s, %s, %s)", (t, f"{name} {t}", f"RV{str(t)[:6].upper()}"))
        cx.execute("INSERT INTO cloudops.cloud_account (id, tenant_id, provider, native_id,"
                   " display_name) VALUES (%s, %s, 'aws', %s, 'Revenue test')",
                   (made.gbp_account, made.gbp, f"rev-{made.gbp_account}"))
        for day, amount in (("2025-03-10", "1000"), ("2025-04-10", "3000")):
            cx.execute("INSERT INTO cloudops.cost_fact (tenant_id, account_id, usage_date,"
                       " invoice_period, service_native, unblended_cost, amortized_cost,"
                       " currency) VALUES (%s, %s, %s, %s, 'EC2', %s, %s, 'USD')",
                       (made.gbp, made.gbp_account, day, day[:7], amount, amount))
        # Not consumption: never in the consumption column (HLD D8).
        cx.execute("INSERT INTO cloudops.cost_fact (tenant_id, account_id, usage_date,"
                   " invoice_period, service_native, charge_class, unblended_cost,"
                   " amortized_cost, currency) VALUES (%s, %s, '2025-03-11', '2025-03',"
                   " 'Support', 'support', 999, 999, 'USD')", (made.gbp, made.gbp_account))
        _snapshot(cx, made.gbp, "2025-03-01", "2025-03-31", "GBP", "10000", "6000")
        _snapshot(cx, made.gbp, "2025-04-01", "2025-04-30", "GBP", "10000", "7000")
        _snapshot(cx, made.eur, "2025-03-01", "2025-03-31", "EUR", "20000", "15000")
        _snapshot(cx, made.eur, "2025-04-01", "2025-04-30", "EUR", "20000", "16000")
        for f, t, day, rate in RATES:
            cx.execute("INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate,"
                       " rate_type, effective_date, source) VALUES (%s, %s, %s, 'average',"
                       " %s, 'test') ON CONFLICT DO NOTHING", (f, t, rate, day))
    yield made
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("SET session_replication_role = replica")  # the SOW history trigger
        for f, t, day, _ in RATES:
            cx.execute("DELETE FROM cloudops.fx_rate WHERE from_currency = %s AND to_currency"
                       " = %s AND rate_type = 'average' AND effective_date = %s AND source"
                       " = 'test'", (f, t, day))
        for t in (made.gbp, made.eur):
            cx.execute("DELETE FROM cloudops.cost_fact WHERE tenant_id = %s", (t,))
            cx.execute("DELETE FROM cloudops.margin_snapshot WHERE tenant_id = %s", (t,))
            cx.execute("DELETE FROM cloudops.tenant WHERE id = %s", (t,))


@pytest.fixture(scope="module")
async def recorded(api: Api, principals: Principals, customers: Customers) -> Customers:
    """The SOWs go through Settings' own path, so ``csp`` is recorded by 0036's
    ``access.record_sow``."""
    for tenant, body in ((customers.gbp, _sow()),
                         (customers.eur, _sow(currency="EUR", recurring_fee="240000",
                                              manage_acv="250000", csp=False))):
        r = await api.post(f"/v1/commercial/customers/{tenant}/sows", principals.admin, body)
        assert r.status_code == 201, r.text
        assert r.json()["versions"][0]["csp"] is body["csp"]
    return customers


def _v(metric: dict[str, Any] | None) -> Decimal:
    assert metric is not None
    return Decimal(str(metric["value"]))


def _row(body: dict[str, Any], tenant: UUID) -> dict[str, Any]:
    [row] = [r for r in body["rows"] if r["tenant"]["id"] == str(tenant)]
    return row


async def _get(api: Api, who: UUID, path: str = "/v1/commercial/revenue",
               **params: str) -> Any:
    return await api.get(path, who, params={**RANGE, **params})


# --- isolation and refusal -------------------------------------------------------------------


@pytest.mark.isolation
@pytest.mark.parametrize("path", ["/v1/commercial/revenue", "/v1/commercial/revenue.csv"])
async def test_customers_and_engineers_are_refused(
    api: Api, principals: Principals, recorded: Customers, path: str
) -> None:
    for who in (principals.customer_a, principals.tenant_admin_a, principals.engineer_a):
        r = await _get(api, who, path)
        assert r.status_code == 403, (who, r.text)
        assert "Revenue test" not in r.text


@pytest.mark.isolation
async def test_a_customer_scope_cannot_read_the_csp_flag(app_db: Database, seeded: Seeded) -> None:
    from sqlalchemy import text

    with pytest.raises(Exception, match="permission denied for table sow"):
        async with app_db.transaction(scope(seeded.a, role=Role.CUSTOMER)) as conn:
            await conn.execute(text("SELECT csp FROM cloudops.sow"))


@pytest.mark.isolation
async def test_the_selector_narrows_to_one_customer_and_never_widens(
    api: Api, principals: Principals, recorded: Customers
) -> None:
    r = await _get(api, principals.finops_all, tenant_id=str(recorded.gbp))
    assert r.status_code == 200, r.text
    assert [row["tenant"]["id"] for row in r.json()["rows"]] == [str(recorded.gbp)]
    unknown = await _get(api, principals.finops_all, tenant_id=str(uuid4()))
    assert unknown.status_code == 404


@pytest.mark.isolation
async def test_an_engineer_scope_reads_no_margin_or_sow_for_the_view(
    app_db: Database, recorded: Customers
) -> None:
    """The second lock: were the matrix wrong, the database still returns nothing."""
    from sqlalchemy import text

    from cloudops_tenancy import TenantScope

    engineer = TenantScope([recorded.gbp], Role.ENGINEER, "principal:isolation-suite")
    async with app_db.transaction(engineer) as conn:
        for table in ("cloudops.sow", "cloudops.margin_snapshot"):
            assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0  # noqa: S608


# --- figures ----------------------------------------------------------------------------------


async def test_two_currency_margin_computed_in_contract_currency_then_converted(
    api: Api, principals: Principals, recorded: Customers
) -> None:
    r = await _get(api, principals.finops_all, currency="EUR")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["range"] == {**body["range"], "first": "2025-03", "last": "2025-04"}
    g = _row(body, recorded.gbp)
    assert g["csp"] is True
    assert g["contract_currency"] == "GBP"
    c = g["current"]["contract"]
    assert c["manage_revenue"]["unit"] == "GBP"
    assert _v(c["manage_revenue"]) == Decimal(10000)
    assert _v(c["manage_cost"]) == Decimal(6500)
    assert _v(c["gross_margin_pct"]) == Decimal("0.35")
    rep = g["current"]["reported"]
    assert rep["manage_revenue"]["unit"] == "EUR"
    # Each month at its own average rate, margin converted after it was computed.
    assert _v(rep["manage_revenue"]) == (Decimal(10000) * Decimal("1.20")
                                         + Decimal(10000) * Decimal("1.15")) / 2
    assert _v(rep["gross_margin"]) == (Decimal(4000) * Decimal("1.20")
                                       + Decimal(3000) * Decimal("1.15")) / 2
    assert _v(rep["gross_margin_pct"]) == Decimal("0.35")


async def test_consumption_converts_on_its_usage_date_and_history_ignores_later_rates(
    api: Api, principals: Principals, recorded: Customers
) -> None:
    g = _row((await _get(api, principals.finops_all, currency="EUR")).json(), recorded.gbp)
    c, rep = g["current"]["contract"], g["current"]["reported"]
    # 10 March at 0.80, 10 April at 0.75; the 0.10 published in May is never used.
    assert _v(c["cloud_consumption"]) == (Decimal(1000) * Decimal("0.80")
                                          + Decimal(3000) * Decimal("0.75")) / 2
    assert _v(rep["cloud_consumption"]) == (Decimal(1000) * Decimal("0.95")
                                            + Decimal(3000) * Decimal("0.90")) / 2


async def test_acvs_entered_or_derived_and_sow_figures_at_the_rate_when_it_took_effect(
    api: Api, principals: Principals, recorded: Customers
) -> None:
    body = (await _get(api, principals.finops_all, currency="EUR")).json()
    g, e = _row(body, recorded.gbp), _row(body, recorded.eur)
    gc = g["current"]["contract"]
    assert gc["manage_acv"]["is_modelled"] is True
    assert _v(gc["manage_acv"]) == Decimal(120000)  # 12 x 10000
    assert gc["hosting_acv"]["is_modelled"] is True
    assert _v(gc["hosting_acv"]) == 12 * _v(gc["cloud_consumption"]) * Decimal("1.1")
    assert _v(g["current"]["reported"]["manage_acv"]) == Decimal(120000) * Decimal("1.18")
    assert _v(g["current"]["reported"]["professional_services"]) == (
        Decimal(2500) * Decimal("1.18"))
    ec = e["current"]["contract"]
    assert e["csp"] is False
    assert ec["manage_acv"]["is_modelled"] is False
    assert _v(ec["manage_acv"]) == Decimal(250000)
    assert ec["hosting_acv"] is None
    assert {"figure": "hosting_acv", "reason": "not_csp"} in e["current"]["gaps"]


async def test_totals_per_contract_currency_and_in_the_reporting_currency(
    api: Api, principals: Principals, recorded: Customers
) -> None:
    body = (await _get(api, principals.finops_all, currency="EUR")).json()
    contract = {t["currency"]: t for t in body["totals"]["contract"]}
    assert _v(contract["GBP"]["figures"]["manage_revenue"]) == Decimal(10000)
    assert _v(contract["EUR"]["figures"]["manage_revenue"]) == Decimal(20000)
    total = body["totals"]["reported"]["figures"]
    revenue = Decimal(11750) + Decimal(20000)
    margin = Decimal(4125) + Decimal(4500)
    assert _v(total["manage_revenue"]) == revenue
    assert _v(total["gross_margin"]) == margin
    assert _v(total["gross_margin_pct"]) == margin / revenue
    # The sum of what the rows show: no hidden figure.
    shown = sum((_v(r["current"]["reported"]["manage_revenue"]) for r in body["rows"]
                 if r["current"]["reported"]["manage_revenue"]), Decimal(0))
    assert shown == revenue


async def test_month_against_month(
    api: Api, principals: Principals, recorded: Customers
) -> None:
    r = await api.get("/v1/commercial/revenue", principals.finops_all, params={
        "from": "2025-04", "to": "2025-04", "compare_from": "2025-03", "compare_to": "2025-03"})
    assert r.status_code == 200, r.text
    g = _row(r.json(), recorded.gbp)
    assert _v(g["compare"]["contract"]["manage_cost"]) == Decimal(6000)
    assert _v(g["change"]["contract"]["manage_cost"]) == Decimal(1000)
    assert _v(g["change"]["contract"]["gross_margin_pct"]) == Decimal("-0.1")
    assert _v(g["change"]["contract"]["cloud_consumption"]) == (
        Decimal(3000) * Decimal("0.75") - Decimal(1000) * Decimal("0.80"))


@pytest.mark.parametrize("params", [
    {"from": "2025-05", "to": "2025-04"}, {"from": "2023-01", "to": "2025-04"},
    {"from": "2025-04", "to": "2025-04", "compare_from": "2025-03"},
    {"from": "2025-13"}])
async def test_a_bad_range_is_refused(api: Api, principals: Principals,
                                      params: dict[str, str]) -> None:
    r = await api.get("/v1/commercial/revenue", principals.finops_all, params=params)
    assert r.status_code == 422


# --- CSV --------------------------------------------------------------------------------------


async def test_csv_is_the_sheet_rounded_once_with_totals(
    api: Api, principals: Principals, recorded: Customers
) -> None:
    r = await _get(api, principals.finops_all, "/v1/commercial/revenue.csv", currency="EUR",
                   tenant_id=str(recorded.gbp))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "revenue-margin-2025-03-2025-04.csv" in r.headers["content-disposition"]
    lines = list(csv.reader(io.StringIO(r.text)))
    assert lines[0][:4] == ["Sl No", "Customer name", "CSP", "Currency"]
    [row, total] = lines[1:]
    assert row[:4] == ["1", f"Revenue test GBP {recorded.gbp}", "Yes", "EUR"]
    assert row[4] == "1825.00"   # consumption
    assert row[5] == "11750.00"  # manage revenue
    assert row[7] == "35.0"      # margin %
    assert row[10] == "141600.00"  # manage ACV
    assert "manage_acv modelled" in row[11]
    assert total[1] == "Total (EUR)"
    assert total[5] == "11750.00"


async def test_csv_neutralises_a_formula_in_a_customer_name(
    api: Api, principals: Principals, recorded: Customers
) -> None:
    r = await _get(api, principals.finops_all, "/v1/commercial/revenue.csv",
                   tenant_id=str(recorded.eur))
    [_, row, _total] = list(csv.reader(io.StringIO(r.text)))
    assert row[1].startswith("'=Revenue test EUR")
    assert row[3] == "EUR"
    assert row[7] == "22.5"
