"""FinOps, margin and pricing endpoints (step 16) against the seeded database:
three tenants in EUR, GBP and USD, one margin snapshot each."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from cloudops_tenancy import Role, TenantScope

from .conftest import Api, Principals, Seeded
from .support import scope

pytestmark = pytest.mark.integration

REF_INVENTORY = {
    "Azure Virtual Machines / Amazon EC2": 15,
    "App Service / AWS Elastic Beanstalk": 260,
    "Azure Kubernetes Service (AKS) / Amazon EKS": 46,
}


async def _post(api: Api, path: str, who: Any, body: dict[str, Any]) -> Any:
    return await api.client.post(path, json=body,
                                 headers={"Authorization": f"Bearer {api.token(who)}"})


# --- the case the brief names --------------------------------------------------------------


@pytest.mark.isolation
async def test_an_engineer_sees_a_customers_tickets_but_not_its_margin(
    api: Api, principals: Principals, seeded: Seeded, app_db: Any
) -> None:
    """The engineer is assigned to tenant a. Tickets: yes. Margin: refused by the
    matrix, and absent in the database under the engineer's own scope, so a
    future endpoint that forgot the matrix still could not leak it."""
    tickets = await api.get("/v1/work-items", principals.engineer_a)
    assert tickets.status_code == 200
    assert any(i["number"] == f"INC-{seeded.a.name}" for i in tickets.json()["items"])
    for path in ("/v1/commercial/margin", f"/v1/commercial/margin/{seeded.a.id}",
                 "/v1/commercial/sows", "/v1/commercial/rate-card"):
        r = await api.get(path, principals.engineer_a)
        assert r.status_code == 403, path
    async with app_db.transaction(scope(seeded.a, role=Role.ENGINEER)) as conn:
        work = await conn.scalar(_count("cloudops.work_item"))
        margin = await conn.scalar(_count("cloudops.margin_snapshot"))
        variance = await conn.scalar(_count("cloudops.margin_variance"))
    assert work == 1
    assert (margin, variance) == (0, 0)
    async with app_db.transaction(scope(seeded.a, role=Role.CFO)) as conn:
        assert await conn.scalar(_count("cloudops.margin_snapshot")) == 1


@pytest.mark.isolation
@pytest.mark.parametrize("role", [Role.ENGINEER, Role.SECOPS, Role.INGEST])
async def test_commercial_tables_are_closed_to_non_commercial_roles(
    app_db: Any, seeded: Seeded, role: Role
) -> None:
    async with app_db.transaction(scope(seeded.a, role=role)) as conn:
        for table in ("cloudops.margin_snapshot", "cloudops.margin_variance"):
            assert await conn.scalar(_count(table)) == 0, table


@pytest.mark.isolation
@pytest.mark.parametrize("table", ["cloudops.margin_snapshot", "cloudops.margin_variance",
                                   "cloudops.billing_total", "cloudops.cost_reconciliation",
                                   "cloudops.cost_anomaly", "cloudops.anomaly_threshold",
                                   "cloudops.fx_restatement"])
async def test_a_customer_scope_is_refused_every_step16_table(
    app_db: Any, seeded: Seeded, table: str
) -> None:
    """The first lock (ADR 0018): a customer scope runs as cloudops_customer, which
    holds no grant on any step 16 table, so it is refused outright; for margin the
    restrictive policy is the second lock."""
    with pytest.raises(Exception, match="permission denied"):
        async with app_db.transaction(scope(seeded.a, role=Role.CUSTOMER)) as conn:
            await conn.scalar(_count(table))


@pytest.mark.isolation
async def test_margin_is_scoped_to_the_callers_tenants(app_db: Any, seeded: Seeded) -> None:
    async with app_db.transaction(scope(seeded.a, role=Role.FINOPS)) as conn:
        tenants = {r[0] for r in await conn.execute(_tenants_of("cloudops.margin_snapshot"))}
    assert tenants == {seeded.a.id}


@pytest.mark.isolation
async def test_a_customer_gets_no_margin_and_no_rate_card(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    for path in ("/v1/commercial/margin", "/v1/commercial/rate-card"):
        assert (await api.get(path, principals.customer_a)).status_code == 403
    q = await _post(api, "/v1/commercial/quotes", principals.customer_a,
                    {"inventory": REF_INVENTORY, "monthly_consumption": "1", "currency": "EUR"})
    assert q.status_code == 403


@pytest.mark.isolation
async def test_a_subscription_limited_scope_sees_no_margin(app_db: Any, seeded: Seeded) -> None:
    """Margin is per customer, never per subscription (D16)."""
    limited = TenantScope([seeded.c.id], Role.FINOPS, "principal:isolation-suite",
                          [seeded.c.account_id])
    async with app_db.transaction(limited) as conn:
        assert await conn.scalar(_count("cloudops.margin_snapshot")) == 0
        assert await conn.scalar(_count("cloudops.cost_reconciliation")) == 1


@pytest.mark.isolation
async def test_the_engine_cannot_write_margin_for_another_tenant(
    seeded: Seeded, ingest_db: Any
) -> None:
    from sqlalchemy import text

    with pytest.raises(Exception, match="row-level security"):
        async with ingest_db.transaction(scope(seeded.a, role=Role.FINOPS)) as conn:
            await conn.execute(text(
                "INSERT INTO cloudops.margin_snapshot (tenant_id, period, through, status,"
                " rate_card_version, not_captured) VALUES (:t, '2026-01-01', '2026-01-31',"
                " 'closed', 'x', 'no_sow')"), {"t": seeded.b.id})


# --- margin ------------------------------------------------------------------------------------


async def test_margin_overview_in_three_contract_currencies(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    from datetime import UTC, datetime

    period = datetime.now(UTC).strftime("%Y-%m")
    r = await api.get("/v1/commercial/margin", principals.finops_all, params={"period": period})
    assert r.status_code == 200
    by = {c["tenant"]["id"]: c for c in r.json()["customers"]}
    for t in seeded.tenants:
        m = by[str(t.id)]
        assert m["contract_currency"] == t.currency
        assert m["actual_margin"]["value"] == "10250.500000"
        assert m["actual_margin"]["unit"] == t.currency
        assert m["band"] == "watch"
        assert m["gap_to_target"]["unit"] == "ratio"
        assert m["not_captured"] is None


async def test_margin_reported_with_fx_impact_apart(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    """GBP and EUR contracts reported in USD: converted after computing, impact
    is margin now minus margin at last month's rates."""
    from datetime import UTC, datetime

    period = datetime.now(UTC).strftime("%Y-%m")
    r = await api.get("/v1/commercial/margin", principals.finops_all,
                      params={"period": period, "currency": "USD"})
    assert r.status_code == 200
    for m in r.json()["customers"]:
        rep = m["reported"]
        if rep is None:
            continue  # a pair with no rate in force: absent, never a guess
        assert rep["currency"] == "USD"
        impact = Decimal(rep["fx_impact"]["value"])
        assert impact == Decimal(rep["margin"]["value"]) - Decimal(
            rep["margin_at_prior_rates"]["value"])


async def test_a_month_with_no_snapshot_is_not_computed_never_zero(
    api: Api, principals: Principals
) -> None:
    r = await api.get("/v1/commercial/margin", principals.finops_all,
                      params={"period": "2020-01"})
    assert r.status_code == 200
    for m in r.json()["customers"]:
        assert m["not_captured"] == "not_computed"
        assert m["actual_margin"] is None


async def test_margin_detail_has_variance_and_versions(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    from datetime import UTC, datetime

    period = datetime.now(UTC).strftime("%Y-%m")
    r = await api.get(f"/v1/commercial/margin/{seeded.b.id}", principals.finops_all,
                      params={"period": period})
    assert r.status_code == 200
    body = r.json()
    assert body["variance"][0]["workgroup"] == "Cloud Ops"
    assert body["variance"][0]["excess_cost"]["unit"] == seeded.b.currency
    assert [v["version"]["value"] for v in body["versions"]] == [1]


async def test_margin_detail_outside_scope_is_404(api: Api, principals: Principals) -> None:
    r = await api.get(f"/v1/commercial/margin/{uuid4()}", principals.finops_all)
    assert r.status_code == 404


# --- FinOps ------------------------------------------------------------------------------------


async def test_finops_overview_per_customer_and_currency(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    r = await api.get("/v1/finops/overview", principals.finops_all, params={"currency": "EUR"})
    assert r.status_code == 200
    by = {c["tenant"]["id"]: c for c in r.json()["customers"]}
    for t in seeded.tenants:
        c = by[str(t.id)]
        # c's second subscription has cost and no check yet: the customer's figure
        # is not verified until every account is.
        assert c["verification"] == ("unverified" if t is seeded.c else "verified")
        line = next(x for x in c["charges"] if x["charge_class"] == "consumption")
        assert line["feeds_margin"] is True
        assert line["currency"] == t.currency
        assert line["amortized"]["unit"] == t.currency
        if line["converted"] is not None:
            assert line["converted"]["unit"] == "EUR"
        if t.currency == "EUR":
            assert line["converted"]["value"] == line["amortized"]["value"]
        assert c["anomalies"]["value"] >= 1
        dims = {u["dimension"] for u in c["unallocated"]}
        assert dims == {"application", "cost_centre"}


async def test_drift_shows_as_unverified_not_as_a_quiet_number(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.cost_reconciliation SET status = 'unverified',"
                   " reason = 'drift', drift = 0.0123, reference_amount = 118.66"
                   " WHERE tenant_id = %s", (seeded.b.id,))
        try:
            r = await api.get(f"/v1/finops/customers/{seeded.b.id}", principals.finops_all)
        finally:
            cx.execute("UPDATE cloudops.cost_reconciliation SET status = 'verified',"
                       " reason = NULL, drift = 0, reference_amount = 120.125"
                       " WHERE tenant_id = %s", (seeded.b.id,))
    assert r.status_code == 200
    cost = r.json()["cost"]
    assert cost["verification"] == "unverified"
    assert cost["checks"][0]["reason"] == "drift"
    assert cost["checks"][0]["drift"]["value"] == "0.01230000"


async def test_finops_customer_has_allocation_and_anomalies(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    r = await api.get(f"/v1/finops/customers/{seeded.a.id}", principals.finops_all)
    assert r.status_code == 200
    body = r.json()
    assert {a["dimension"] for a in body["allocation"]} == {"application", "cost_centre"}
    assert body["anomalies"][0]["direction"] == "spike"
    assert body["anomalies"][0]["currency"] == seeded.a.currency


async def test_finops_customer_outside_scope_is_404(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    r = await api.get(f"/v1/finops/customers/{uuid4()}", principals.finops_all)
    assert r.status_code == 404
    # A customer reads its own cost through the portal, not the FinOps module.
    for path in ("/v1/finops/overview", f"/v1/finops/customers/{seeded.a.id}",
                 f"/v1/finops/customers/{seeded.b.id}"):
        assert (await api.get(path, principals.customer_a)).status_code == 403


# --- FX corrections ----------------------------------------------------------------------------


async def test_finance_corrects_a_rate_with_an_explicit_entry(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    body = {"from_currency": "GBP", "to_currency": "USD", "rate_type": "average",
            "effective_date": "2026-01-02", "rate": "1.27123456",
            "reason": "Bank statement disagrees with the feed"}
    r = await _post(api, "/v1/finops/fx-corrections", principals.finops_all, body)
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["new_rate"]["value"] == "1.27123456"
    assert entry["new_rate"]["unit"] == "GBP->USD"
    with psycopg.connect(seeded.admin_dsn) as cx:
        rate = cx.execute("SELECT rate, source FROM cloudops.fx_rate WHERE from_currency = 'GBP'"
                          " AND to_currency = 'USD' AND rate_type = 'average'"
                          " AND effective_date = '2026-01-02'").fetchone()
    assert rate == (Decimal("1.27123456"), "finance")
    again = await _post(api, "/v1/finops/fx-corrections", principals.finops_all, body)
    assert again.status_code == 409
    listed = await api.get("/v1/finops/fx-corrections", principals.finops_all)
    assert any(e["id"] == entry["id"] for e in listed.json())


async def test_only_fx_managers_correct_rates(api: Api, principals: Principals) -> None:
    body = {"from_currency": "EUR", "to_currency": "GBP", "rate_type": "average",
            "effective_date": "2026-01-02", "rate": "0.5", "reason": "no"}
    for who in (principals.engineer_a, principals.customer_a):
        r = await _post(api, "/v1/finops/fx-corrections", who, body)
        assert r.status_code == 403


# --- pricing ------------------------------------------------------------------------------------


async def test_quote_reproduces_the_reference_case(api: Api, principals: Principals) -> None:
    r = await _post(api, "/v1/commercial/quotes", principals.finops_all, {
        "inventory": REF_INVENTORY, "monthly_consumption": "43856", "currency": "EUR",
        "coverage": "24x7", "support_model": "Gold", "location": "India",
        "on": "2026-10-31"})
    assert r.status_code == 200, r.text
    q = r.json()
    assert Decimal(q["total_monthly"]["value"]).quantize(Decimal("0.000001")) == Decimal(
        "17095.818115")
    assert q["slab"] == "301 - 500 CIs"
    assert q["rate_card_version"] == "2026-v2.14"
    assert q["total_monthly"]["is_modelled"] is True


async def test_quote_refuses_what_it_cannot_price(api: Api, principals: Principals) -> None:
    gbp = await _post(api, "/v1/commercial/quotes", principals.finops_all, {
        "inventory": REF_INVENTORY, "monthly_consumption": "1000", "currency": "GBP"})
    assert gbp.status_code == 422
    assert gbp.json()["error"]["code"] == "currency_not_supported"
    unknown = await _post(api, "/v1/commercial/quotes", principals.finops_all, {
        "inventory": {"Mainframe": 1}, "monthly_consumption": "1000", "currency": "EUR"})
    assert unknown.status_code == 422
    early = await _post(api, "/v1/commercial/quotes", principals.finops_all, {
        "inventory": REF_INVENTORY, "monthly_consumption": "1", "currency": "EUR",
        "on": "2020-01-01"})
    assert early.json()["error"]["code"] == "no_rate_card"


async def test_rate_card_lists_the_catalogue(api: Api, principals: Principals) -> None:
    r = await api.get("/v1/commercial/rate-card", principals.finops_all)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "2026-v2.14"
    assert "Azure Virtual Machines / Amazon EC2" in body["resource_types"]


def _count(table: str) -> Any:
    from sqlalchemy import text

    return text(f"SELECT count(*) FROM {table}")


def _tenants_of(table: str) -> Any:
    from sqlalchemy import text

    return text(f"SELECT DISTINCT tenant_id FROM {table}")
