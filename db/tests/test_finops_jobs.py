"""The FinOps worker's jobs against a real database (step 16): reconciliation,
anomalies, margin, month-end close and restatement.

A database of this module's own, seeded with figures chosen so every expected
value can be computed by hand (or by ``cloudops_commercial``, which the unit
tests pin to the reference engine). Four customers, four currency situations:

- ``eur``: EUR contract, EUR billing, India delivery.
- ``gbp``: GBP contract, USD billing, India delivery: cost is converted twice
  (USD to EUR for the rate card, EUR to GBP for the contract).
- ``nosow``: no SOW: margin is not captured, never zero.
- ``mixed``: USD contract, Mixed delivery: the rate card has no people cost.

The run date is 2026-10-20: October is month to date, September closes,
June to August are backfilled closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_commercial import margin
from cloudops_commercial.margin import EffortBucket, MarginInputs
from cloudops_commercial.ratecard import V2_14
from cloudops_connectors.finops import jobs
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import _database

pytestmark = pytest.mark.integration

ON = date(2026, 10, 20)
FIRST = date(2026, 6, 1)
LAST = ON - timedelta(days=1)

# Daily average rates, constant within a month so the month average is exact.
EUR_GBP = {6: "0.84", 7: "0.84", 8: "0.85", 9: "0.86", 10: "0.87"}
USD_EUR = "0.92"
EUR_USD = "1.08"


@dataclass(frozen=True)
class Customer:
    id: UUID
    account: UUID
    currency: str  # billing


@dataclass(frozen=True)
class Db:
    admin: str
    ingest: str
    app: str
    eur: Customer
    gbp: Customer
    nosow: Customer
    mixed: Customer


def _days(a: date, b: date) -> Iterator[date]:
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _seed(cx: psycopg.Connection[Any], db: Db) -> None:
    for name, c, loc in (("eur", db.eur, "India"), ("gbp", db.gbp, "India"),
                         ("nosow", db.nosow, "India"), ("mixed", db.mixed, "Mixed")):
        cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code,"
                   " delivery_location, coverage, support) VALUES (%s, %s, %s, %s, '24x7',"
                   " 'Gold')", (c.id, f"FinOps {name}", f"FO{name.upper()}", loc))
        cx.execute("INSERT INTO cloudops.cloud_account (id, tenant_id, provider, native_id,"
                   " display_name) VALUES (%s, %s, 'azure', %s, %s)",
                   (c.account, c.id, f"sub-{name}", f"{name} prod"))
    for c, ccy, revenue, target in ((db.eur, "EUR", "20000", "0.25"),
                                    (db.gbp, "GBP", "18000", "0.30"),
                                    (db.mixed, "USD", "15000", None)):
        cx.execute("INSERT INTO cloudops.sow (tenant_id, reference, effective_from, currency,"
                   " monthly_manage_revenue, target_margin) VALUES (%s, %s, '2026-01-01', %s,"
                   " %s, %s)", (c.id, f"SOW-{ccy}", ccy, revenue, target))
    # Inventory: ten VMs and two storage accounts (no catalogue row) for eur; five
    # SQL databases for gbp.
    for c, kind, n in ((db.eur, "compute.vm", 10), (db.eur, "storage.account", 2),
                       (db.gbp, "database.sql", 5), (db.mixed, "compute.vm", 3)):
        for i in range(n):
            cx.execute("INSERT INTO cloudops.resource (tenant_id, account_id, native_id, name,"
                       " type_native, type_canonical, first_seen) VALUES (%s, %s, %s, %s, 'x',"
                       " %s, '2026-01-01')", (c.id, c.account, f"/{kind}/{c.id}/{i}",
                                              f"{kind}-{i}", kind))
    for d in _days(FIRST - timedelta(days=40), ON):
        for frm, to, r in (("EUR", "GBP", EUR_GBP.get(d.month, "0.84")),
                           ("USD", "EUR", USD_EUR), ("EUR", "USD", EUR_USD)):
            cx.execute("INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate,"
                       " rate_type, effective_date, source) VALUES (%s, %s, %s, 'average', %s,"
                       " 'test')", (frm, to, r, d))
    cost = ("INSERT INTO cloudops.cost_fact (tenant_id, account_id, usage_date, invoice_period,"
            " service_native, charge_class, unblended_cost, amortized_cost, currency)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)")
    for d in _days(FIRST, LAST):
        period = d.strftime("%Y-%m")
        eur_day = "3000" if d == date(2026, 10, 18) else "1000"
        cx.execute(cost, (db.eur.id, db.eur.account, d, period, "VM", "consumption", eur_day,
                          eur_day, "EUR"))
        # Licence spend never feeds margin (HLD D8).
        cx.execute(cost, (db.eur.id, db.eur.account, d, period, "Lic", "licence", "50", "50",
                          "EUR"))
        usd_day = "800" if d == date(2026, 10, 17) else "500"
        cx.execute(cost, (db.gbp.id, db.gbp.account, d, period, "SQL", "consumption", usd_day,
                          usd_day, "USD"))
        cx.execute(cost, (db.nosow.id, db.nosow.account, d, period, "VM", "consumption", "10",
                          "10", "EUR"))
    total = ("INSERT INTO cloudops.billing_total (tenant_id, account_id, source_system, kind,"
             " invoice_period, as_of, amount, currency, document_ref) VALUES (%s, %s, 'azure',"
             " %s, %s, %s, %s, %s, %s)")
    # eur: October to date agrees exactly; September's invoice agrees.
    eur_oct = sum(Decimal(1050) for _ in _days(date(2026, 10, 1), LAST)) + Decimal(2000)
    cx.execute(total, (db.eur.id, db.eur.account, "provider_total", "2026-10", LAST, eur_oct,
                       "EUR", None))
    cx.execute(total, (db.eur.id, db.eur.account, "invoice", "2026-09", date(2026, 10, 3),
                       Decimal(1050 * 30), "EUR", "INV-EUR-09"))
    # gbp's USD account: the provider says 1% more than the line items.
    gbp_oct = (sum(Decimal(500) for _ in _days(date(2026, 10, 1), LAST)) + Decimal(300)) \
        * Decimal("1.01")
    cx.execute(total, (db.gbp.id, db.gbp.account, "provider_total", "2026-10", LAST, gbp_oct,
                       "USD", None))
    item = ("INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
            " closed_at, assignment_workgroup, actual_hours) VALUES (%s, %s, %s, 'x', %s, %s,"
            " %s, %s)")
    for n, (c, month, kind, group, hours) in enumerate((
            (db.eur, 9, "incident", "Cloud Ops", "10"), (db.eur, 9, "incident", "Cloud Ops", "6"),
            (db.eur, 9, "change_task", "Network", "4"),
            (db.eur, 10, "incident", "Cloud Ops", "8"), (db.eur, 10, "change_task", "Network",
                                                         None),
            (db.gbp, 9, "change_task", "Network", "150"), (db.gbp, 9, "incident", "Cloud Ops",
                                                           "50"))):
        closed = datetime(2026, month, 10, 12, tzinfo=UTC)
        cx.execute(item, (c.id, kind, f"WI{n:05d}", closed - timedelta(days=1), closed, group,
                          hours))


@pytest.fixture(scope="module")
async def fdb(admin_server: str) -> AsyncIterator[Db]:
    with _database(admin_server, "finops") as p:
        db = Db(p.admin_dsn, p.ingest_dsn, p.app_dsn,
                *(Customer(uuid4(), uuid4(), ccy) for ccy in ("EUR", "USD", "EUR", "USD")))
        with psycopg.connect(p.admin_dsn, autocommit=True) as cx:
            _seed(cx, db)
        yield db


@pytest.fixture
async def ingest(fdb: Db) -> AsyncIterator[Database]:
    database = Database.create(fdb.ingest, pool_size=1, max_overflow=0)
    try:
        yield database
    finally:
        await database.dispose()


def q(admin: str, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(admin) as cx:
        return [tuple(r) for r in cx.execute(sql, params).fetchall()]


# --- reconciliation --------------------------------------------------------------------------


async def test_reconcile_verifies_and_flags_drift(fdb: Db, ingest: Database) -> None:
    r = await jobs.reconcile(ingest, ON)
    rows = {(a, p, k): (st, reason, drift, alerted) for a, p, k, st, reason, drift, alerted in q(
        fdb.admin, "SELECT account_id, invoice_period, kind, status, reason, drift,"
        " alerted_at IS NOT NULL FROM cloudops.cost_reconciliation WHERE checked_on = %s", ON)}
    assert rows[(fdb.eur.account, "2026-10", "provider_total")] == ("verified", None,
                                                                    Decimal(0), False)
    assert rows[(fdb.eur.account, "2026-09", "invoice")][:2] == ("verified", None)
    status, reason, drift, alerted = rows[(fdb.gbp.account, "2026-10", "provider_total")]
    assert (status, reason, alerted) == ("unverified", "drift", True)
    assert drift == (Decimal("0.01") / Decimal("1.01")).quantize(Decimal("0.00000001"))
    # Cost and no reference: unverified, shown, not paged.
    assert rows[(fdb.nosow.account, "2026-10", "provider_total")] == (
        "unverified", "no_reference", None, False)
    assert r.alerted == 1
    again = await jobs.reconcile(ingest, ON)
    assert again.alerted == 0  # once per account, period, kind and day


async def test_reconcile_compares_only_the_days_the_provider_total_covers(
    fdb: Db, ingest: Database
) -> None:
    """A provider total as of the 19th is compared with line items through the
    19th, even when later days have landed."""
    late = date(2026, 10, 20)
    with psycopg.connect(fdb.admin, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.cost_fact (tenant_id, account_id, usage_date,"
                   " invoice_period, service_native, charge_class, unblended_cost,"
                   " amortized_cost, currency) VALUES (%s, %s, %s, '2026-10', 'Late',"
                   " 'consumption', 999, 999, 'EUR')", (fdb.eur.id, fdb.eur.account, late))
        try:
            await jobs.reconcile(ingest, ON + timedelta(days=1))
        finally:
            cx.execute("DELETE FROM cloudops.cost_fact WHERE service_native = 'Late'")
    status = q(fdb.admin, "SELECT status FROM cloudops.cost_reconciliation WHERE account_id = %s"
               " AND invoice_period = '2026-10' AND checked_on = %s", fdb.eur.account,
               ON + timedelta(days=1))
    assert status == [("verified",)]


async def test_invoice_documents_issued_on_different_days_are_summed(
    fdb: Db, ingest: Database
) -> None:
    """September's invoice, issued 3 Oct, and a credit memo of -10 issued on
    12 Oct are one reference of 31,490: within 0.5% of the ledger, verified. The
    latest document alone (-10) would page a healthy account."""
    day = ON + timedelta(days=2)
    with psycopg.connect(fdb.admin, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.billing_total (tenant_id, account_id, source_system,"
                   " kind, invoice_period, as_of, amount, currency, document_ref) VALUES"
                   " (%s, %s, 'azure', 'invoice', '2026-09', '2026-10-12', -10, 'EUR',"
                   " 'CN-EUR-09')", (fdb.eur.id, fdb.eur.account))
        try:
            r = await jobs.reconcile(ingest, day)
        finally:
            cx.execute("DELETE FROM cloudops.billing_total WHERE document_ref = 'CN-EUR-09'")
    rows = q(fdb.admin, "SELECT status, reason, reference_amount, reference_as_of,"
             " alerted_at IS NOT NULL FROM cloudops.cost_reconciliation WHERE account_id = %s"
             " AND invoice_period = '2026-09' AND kind = 'invoice' AND checked_on = %s",
             fdb.eur.account, day)
    assert rows == [("verified", None, Decimal(1050 * 30 - 10), date(2026, 10, 12), False)]
    assert r.alerted == 1  # gbp's drifting provider total only


async def test_invoice_documents_in_two_currencies_are_never_summed(
    fdb: Db, ingest: Database
) -> None:
    day = ON + timedelta(days=3)
    with psycopg.connect(fdb.admin, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.billing_total (tenant_id, account_id, source_system,"
                   " kind, invoice_period, as_of, amount, currency, document_ref) VALUES"
                   " (%s, %s, 'azure', 'invoice', '2026-09', '2026-10-12', 25, 'USD',"
                   " 'MP-USD-09')", (fdb.eur.id, fdb.eur.account))
        try:
            await jobs.reconcile(ingest, day)
        finally:
            cx.execute("DELETE FROM cloudops.billing_total WHERE document_ref = 'MP-USD-09'")
    rows = q(fdb.admin, "SELECT status, reason, reference_amount, alerted_at IS NOT NULL"
             " FROM cloudops.cost_reconciliation WHERE account_id = %s"
             " AND invoice_period = '2026-09' AND kind = 'invoice' AND checked_on = %s",
             fdb.eur.account, day)
    assert rows == [("unverified", "currency_mismatch", None, True)]


# --- anomalies -------------------------------------------------------------------------------


async def test_day_over_day_spikes_in_two_currencies(fdb: Db, ingest: Database) -> None:
    await jobs.anomalies(ingest, ON)
    found = {(a, g, o): (cur, direction, amount, alerted) for a, g, o, cur, direction, amount,
             alerted in q(fdb.admin, "SELECT account_id, granularity, observed_on, currency,"
                          " direction, threshold_amount, alerted_at IS NOT NULL"
                          " FROM cloudops.cost_anomaly")}
    assert found[(fdb.eur.account, "day", date(2026, 10, 18))] == (
        "EUR", "spike", Decimal("100.000000"), True)
    # The floor is 100 EUR, converted into the account's currency at the rate in force.
    assert found[(fdb.gbp.account, "day", date(2026, 10, 17))] == (
        "USD", "spike", Decimal("108.000000"), True)
    # The day after a spike drops back: the trailing mean is lifted, not past the ratio.
    assert (fdb.eur.account, "day", date(2026, 10, 19)) not in found


async def test_a_restated_day_that_no_longer_crosses_is_removed(fdb: Db, ingest: Database) -> None:
    await jobs.anomalies(ingest, ON)
    with psycopg.connect(fdb.admin, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.cost_fact SET amortized_cost = 1000"
                   " WHERE account_id = %s AND usage_date = '2026-10-18'"
                   "   AND charge_class = 'consumption'", (fdb.eur.account,))
        try:
            await jobs.anomalies(ingest, ON)
            left = q(fdb.admin, "SELECT 1 FROM cloudops.cost_anomaly WHERE account_id = %s"
                     " AND observed_on = '2026-10-18'", fdb.eur.account)
            assert left == []
        finally:
            cx.execute("UPDATE cloudops.cost_fact SET amortized_cost = 3000"
                       " WHERE account_id = %s AND usage_date = '2026-10-18'"
                       "   AND charge_class = 'consumption'", (fdb.eur.account,))


async def test_tenant_threshold_overrides_the_default(fdb: Db, ingest: Database) -> None:
    with psycopg.connect(fdb.admin, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.anomaly_threshold VALUES (%s, 'day', 5, 1, 'GBP')",
                   (fdb.eur.id,))
        try:
            await jobs.anomalies(ingest, ON)
            spikes = q(fdb.admin, "SELECT 1 FROM cloudops.cost_anomaly WHERE account_id = %s"
                       " AND granularity = 'day' AND observed_on = '2026-10-18'",
                       fdb.eur.account)
            assert spikes == []  # +200% is below a 500% ratio
        finally:
            cx.execute("DELETE FROM cloudops.anomaly_threshold")


# --- margin ----------------------------------------------------------------------------------


def _snapshot(admin: str, tenant: UUID, period: date) -> dict[str, Any]:
    with psycopg.connect(admin) as cx:
        cur = cx.execute("SELECT * FROM cloudops.margin_snapshot WHERE tenant_id = %s"
                         " AND period = %s ORDER BY version DESC LIMIT 1", (tenant, period))
        cols = [d.name for d in cur.description or []]
        row = cur.fetchone()
    assert row is not None
    return dict(zip(cols, row, strict=True))


def six(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Decimal("0.000001"))


async def test_margins_close_last_month_and_keep_this_one_provisional(
    fdb: Db, ingest: Database
) -> None:
    r = await jobs.margins(ingest, ON)
    oct_eur = _snapshot(fdb.admin, fdb.eur.id, date(2026, 10, 1))
    sep_eur = _snapshot(fdb.admin, fdb.eur.id, date(2026, 9, 1))
    assert (oct_eur["status"], oct_eur["through"]) == ("provisional", LAST)
    assert (sep_eur["status"], sep_eur["through"]) == ("closed", date(2026, 9, 30))
    assert _snapshot(fdb.admin, fdb.eur.id, date(2026, 6, 1))["status"] == "closed"
    assert r.closed >= 4 * 4  # September and June-August, for each customer


async def test_eur_margin_matches_the_engine(fdb: Db, ingest: Database) -> None:
    await jobs.margins(ingest, ON)
    s = _snapshot(fdb.admin, fdb.eur.id, date(2026, 9, 1))
    expected = margin.compute(MarginInputs(
        period=date(2026, 9, 1), contract_currency="EUR", revenue=Decimal(20000),
        target_margin=Decimal("0.25"), location="India", coverage="24x7", support="Gold",
        priced_inventory={"Azure Virtual Machines / Amazon EC2": 10}, unpriced_cis=2,
        consumption_eur=Decimal(30000), has_cost_data=True,  # licence excluded
        effort=(EffortBucket("Cloud Ops", "incident", Decimal(16), 2, 0),
                EffortBucket("Network", "change_task", Decimal(4), 1, 0)),
        rate=margin.MonthRate(Decimal(1), date(2026, 9, 1), date(2026, 9, 30), 1),
        prior_rate=margin.MonthRate(Decimal(1), date(2026, 8, 1), date(2026, 8, 31), 1)),
        V2_14)
    assert s["not_captured"] is None
    assert s["consumption"] == Decimal("30000.000000")
    assert s["unpriced_cis"] == 2
    assert s["actual_margin"] == six(expected.actual_margin)
    assert s["modelled_cost"] == six(expected.modelled_cost)
    assert s["band"] == expected.band
    assert s["band_basis"] == "actual"
    assert s["effort_complete"] is True
    assert s["gap_to_target"] == expected.gap_to_target.quantize(  # type: ignore[union-attr]
        Decimal("0.00000001"))


async def test_gbp_margin_is_computed_in_gbp_before_any_conversion(
    fdb: Db, ingest: Database
) -> None:
    """USD cost -> EUR (rate card) at each day's rate; EUR cost -> GBP at the
    month's average; then subtracted from GBP revenue."""
    await jobs.margins(ingest, ON)
    s = _snapshot(fdb.admin, fdb.gbp.id, date(2026, 9, 1))
    consumption = Decimal(500 * 30) * Decimal(USD_EUR)
    expected = margin.compute(MarginInputs(
        period=date(2026, 9, 1), contract_currency="GBP", revenue=Decimal(18000),
        target_margin=Decimal("0.30"), location="India", coverage="24x7", support="Gold",
        priced_inventory={"MS SQL DB / Azure SQL / Amazon RDS": 5}, unpriced_cis=0,
        consumption_eur=consumption, has_cost_data=True,
        effort=(EffortBucket("Network", "change_task", Decimal(150), 1, 0),
                EffortBucket("Cloud Ops", "incident", Decimal(50), 1, 0)),
        rate=margin.MonthRate(Decimal("0.86"), date(2026, 9, 1), date(2026, 9, 30), 30),
        prior_rate=margin.MonthRate(Decimal("0.85"), date(2026, 8, 1), date(2026, 8, 31),
                                    31)), V2_14)
    assert s["contract_currency"] == "GBP"
    assert s["consumption"] == six(consumption)
    assert s["fx_rate"] == Decimal("0.86000000")
    assert s["prior_fx_rate"] == Decimal("0.85000000")
    assert s["actual_cost"] == six(expected.actual_cost)
    assert s["actual_margin"] == six(expected.actual_margin)
    assert s["margin_at_prior_rates"] == six(expected.margin_at_prior_rates)
    assert s["exceeds_slab"] is True
    variance = q(fdb.admin, "SELECT workgroup, activity, currency, excess_hours > 0"
                 " FROM cloudops.margin_variance WHERE snapshot_id = %s ORDER BY hours DESC",
                 s["id"])
    assert variance == [("Network", "change_task", "GBP", True),
                        ("Cloud Ops", "incident", "GBP", True)]


async def test_gaps_are_not_captured_never_zero(fdb: Db, ingest: Database) -> None:
    await jobs.margins(ingest, ON)
    nosow = _snapshot(fdb.admin, fdb.nosow.id, date(2026, 9, 1))
    mixed = _snapshot(fdb.admin, fdb.mixed.id, date(2026, 9, 1))
    assert nosow["not_captured"] == "no_sow"
    assert mixed["not_captured"] == "no_people_cost"
    for s in (nosow, mixed):
        assert s["actual_margin"] is None
        assert s["margin_pct"] is None
        assert s["band"] is None


async def test_partial_effort_bands_on_the_model(fdb: Db, ingest: Database) -> None:
    await jobs.margins(ingest, ON)
    s = _snapshot(fdb.admin, fdb.eur.id, date(2026, 10, 1))
    assert s["effort_items_without_hours"] == 1
    assert s["effort_complete"] is False
    assert s["band_basis"] == "modelled"


async def test_a_closed_month_is_restated_never_overwritten(fdb: Db, ingest: Database) -> None:
    """Finance corrects September's rate: reprocessing writes version 2, which
    names version 1; version 1 is untouched. A second reprocess with nothing
    moved writes nothing."""
    await jobs.margins(ingest, ON)
    before = _snapshot(fdb.admin, fdb.gbp.id, date(2026, 9, 1))
    with psycopg.connect(fdb.admin, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.fx_rate SET rate = 0.88 WHERE from_currency = 'EUR'"
                   " AND to_currency = 'GBP' AND effective_date = '2026-09-15'")
        try:
            r = await jobs.reprocess(ingest, date(2026, 9, 1))
            after = _snapshot(fdb.admin, fdb.gbp.id, date(2026, 9, 1))
            assert r.restated == 1  # only the GBP contract moved
            assert after["version"] == before["version"] + 1
            assert after["restates"] == before["id"]
            assert after["status"] == "closed"
            assert after["fx_rate"] != before["fx_rate"]
            v1 = q(fdb.admin, "SELECT actual_margin, fx_rate FROM cloudops.margin_snapshot"
                   " WHERE id = %s", before["id"])
            assert v1 == [(before["actual_margin"], before["fx_rate"])]
            assert (await jobs.reprocess(ingest, date(2026, 9, 1))).restated == 0
        finally:
            cx.execute("UPDATE cloudops.fx_rate SET rate = 0.86 WHERE from_currency = 'EUR'"
                       " AND to_currency = 'GBP' AND effective_date = '2026-09-15'")
            await jobs.reprocess(ingest, date(2026, 9, 1))


@pytest.mark.isolation
async def test_the_worker_writes_only_under_the_tenants_it_names(
    fdb: Db, ingest: Database
) -> None:
    """The finops role writes margin; an engineer or customer scope in the same
    tenant reads none of it (the restrictive policy, not the API)."""
    await jobs.reconcile(ingest, ON)
    await jobs.margins(ingest, ON)
    app = Database.create(fdb.app, pool_size=1, max_overflow=0)
    try:
        for role, visible in ((Role.ENGINEER, False), (Role.SECOPS, False),
                              (Role.FINOPS, True), (Role.CFO, True)):
            async with app.transaction(TenantScope([fdb.eur.id], role, "t")) as conn:
                n = await conn.scalar(_count_snapshots)
                v = await conn.scalar(_count_variance)
                c = await conn.scalar(_count_recon)
            assert (n > 0, v > 0) == (visible, visible), role
            assert c > 0  # reconciliation is cost data, not commercial
    finally:
        await app.dispose()


_count_snapshots = text("SELECT count(*) FROM cloudops.margin_snapshot")
_count_variance = text("SELECT count(*) FROM cloudops.margin_variance")
_count_recon = text("SELECT count(*) FROM cloudops.cost_reconciliation")


@pytest.mark.isolation
def test_only_the_ingest_login_can_list_every_tenant(fdb: Db) -> None:
    """The worker's tenant list is closed to the application login: a request
    path can never learn which customers exist outside its scope."""
    with psycopg.connect(fdb.admin) as cx:
        row = cx.execute(
            "SELECT has_function_privilege('cloudops_app', 'cloudops.finops_tenants()',"
            " 'EXECUTE'), has_function_privilege('cloudops_ingest',"
            " 'cloudops.finops_tenants()', 'EXECUTE'), has_function_privilege('public',"
            " 'cloudops.finops_tenants()', 'EXECUTE')").fetchone()
    assert row == (False, True, False)


async def test_one_customers_margin_failure_stops_only_theirs(
    fdb: Db, ingest: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other customer's figures are written; the run then fails so it retries."""
    real = jobs.compute_period
    seen: list[UUID] = []

    async def flaky(conn: Any, tenant: UUID, *a: Any, **kw: Any) -> None:
        if tenant == fdb.gbp.id:
            raise RuntimeError("boom")
        seen.append(tenant)
        await real(conn, tenant, *a, **kw)

    monkeypatch.setattr(jobs, "compute_period", flaky)
    with pytest.raises(jobs.MarginRunError):
        await jobs.margins(ingest, ON)
    assert {fdb.eur.id, fdb.nosow.id, fdb.mixed.id} <= set(seen)
    assert fdb.gbp.id not in seen
