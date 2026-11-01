"""Monthly report packs against the real schema (step 17, ADR 0020; HLD M19).

A pack is generated from live rows in the customer's contract currency, each
cost row converted at the average rate on its own date; a missing rate or SOW
is a visible gap, never a zero; narrative is never released without a
person's text, which the database enforces as well as the API; customers see
published packs only; and the portfolio consolidates the same rows in the
viewer's reporting currency. Every money test uses at least two currencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from cloudops_domain.reporting.render import DRAFT_BANNER

from .conftest import AGREED_ROLES, Step17, Tenant, onboard_portal

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

TODAY = datetime.now(UTC).date()
_FIRST = TODAY.replace(day=1)
# Two months back: closed, its partitions exist (partman premakes four back), and no
# row the shared seed writes "a few days ago" can land in it, even on the 1st.
PERIOD: date = ((_FIRST - timedelta(days=1)).replace(day=1) - timedelta(days=1)).replace(day=1)
LABEL = f"{PERIOD:%Y-%m}"
D1, D2 = PERIOD + timedelta(days=2), PERIOD + timedelta(days=15)
UPLIFT_A = Decimal("0.08")  # alpha's SOW; bravo's and charlie's have none (ADR 0021)

# Rates in force on each row's date. CHF is used by no other suite.
RATES = (
    ("CHF", "EUR", PERIOD, Decimal("1.05000000")),
    ("CHF", "EUR", D2, Decimal("1.10000000")),
    ("EUR", "GBP", PERIOD, Decimal("0.85000000")),
    ("CHF", "GBP", PERIOD, Decimal("0.90000000")),
)


@dataclass(frozen=True)
class Month:
    expected_eur: Decimal  # tenant a's consumption in EUR, row by row
    expected_gbp: Decimal  # tenant a's consumption in GBP, from the same rows
    expected_price_eur: Decimal  # tenant a's price for it: cost x (1 + its SOW's uplift)


@pytest.fixture(scope="module")
def month(step17: Step17) -> Month:
    s = step17.seeded
    a, b, c = s.a, s.b, s.c
    t0 = datetime.combine(PERIOD, datetime.min.time(), UTC)
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        for source, target, on, rate in RATES:
            cx.execute("INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate,"
                       " rate_type, effective_date, source) VALUES (%s, %s, %s, 'average', %s,"
                       " 'test') ON CONFLICT DO NOTHING", (source, target, rate, on))
        # tenant, day, service, amount, currency
        rows: tuple[tuple[Tenant, date, str, Decimal, str], ...] = (
            (a, D1, "Compute", Decimal("100.000000"), "EUR"),
            (a, D1, "Storage", Decimal("200.000000"), "CHF"),
            (a, D2, "Network", Decimal("50.000000"), "CHF"),
            (b, D1, "Compute", Decimal("70.000000"), "JPY"),  # no JPY rate anywhere
            (c, D1, "Compute", Decimal("30.000000"), "CHF"),  # no CHF->USD rate
        )
        for t, day, service, amount, ccy in rows:
            cx.execute(
                "INSERT INTO cloudops.cost_fact (tenant_id, account_id, usage_date,"
                " invoice_period, service_native, unblended_cost, amortized_cost, currency)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (t.id, t.account_id, day, LABEL, service, amount, amount, ccy))
        for i, (opened, closed, made) in enumerate((
            (1, 2, True), (3, 5, True), (4, 9, False), (6, None, None),
        )):
            cx.execute(
                "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
                " closed_at, made_sla, priority) VALUES (%s, 'incident', %s, 'ALPHA', %s, %s,"
                " %s, '2 - High')",
                (a.id, f"INC-R-{i}", t0 + timedelta(days=opened),
                 None if closed is None else t0 + timedelta(days=closed), made))
        cx.execute(
            "INSERT INTO cloudops.vulnerability (tenant_id, qid, asset_name, scope, title,"
            " severity, first_detected, remediated_at) VALUES"
            " (%s, 1001, 'vm-alpha-01', 'os', 'Old', 5, %s, NULL),"
            " (%s, 1002, 'vm-alpha-01', 'os', 'Fixed', 3, %s, %s)",
            (a.id, t0 - timedelta(days=100), a.id, t0 + timedelta(days=1),
             t0 + timedelta(days=4)))
        # Step 16's stored figures: a closed EUR margin (version 2 restates 1) with
        # its variance for alpha, a USD one for charlie, none for bravo.
        for t, ccy, version, revenue, cost, prior in (
            (a, "EUR", 1, "40000.000000", "39000.000000", "900.000000"),
            (a, "EUR", 2, "41250.500000", "31000.000000", "10200.000000"),
            (c, "USD", 1, "41250.500000", "45000.000000", "-3500.000000"),
        ):
            margin = Decimal(revenue) - Decimal(cost)
            sid = cx.execute(
                "INSERT INTO cloudops.margin_snapshot (tenant_id, period, through, version,"
                " status, rate_card_version, contract_currency, revenue, modelled_cost,"
                " actual_cost, modelled_margin, actual_margin, margin_at_prior_rates,"
                " margin_pct, band, band_basis, target_margin, gap_to_target, effort_hours,"
                " effort_items, effort_items_without_hours, effort_complete) VALUES"
                " (%s, %s, %s, %s, 'closed', '2026-v2.14', %s, %s, 30000, %s, %s, %s, %s,"
                " %s, 'watch', 'actual', 0.3, %s, 120.25, 4, 1, false) RETURNING id",
                (t.id, PERIOD, D2, version, ccy, revenue, cost, Decimal(revenue) - 30000,
                 margin, prior, margin / Decimal(revenue),
                 margin / Decimal(revenue) - Decimal("0.3"))).fetchone()
            if t is a and version == 2 and sid:
                cx.execute(
                    "INSERT INTO cloudops.margin_variance (tenant_id, snapshot_id, workgroup,"
                    " activity, hours, items, items_without_hours, share, excess_hours,"
                    " excess_cost, currency) VALUES (%s, %s, 'Cloud Ops', 'incident', 90.25,"
                    " 3, 1, 0.75, 10, 1234.567800, 'EUR')", (a.id, sid[0]))
        cx.execute("UPDATE cloudops.sow SET hosting_acv = 120000, manage_acv = 495006,"
                   " professional_services = 0, consumption_uplift = %s WHERE tenant_id = %s",
                   (UPLIFT_A, a.id))
    eur = Decimal("100") + Decimal("200") * Decimal("1.05") + Decimal("50") * Decimal("1.10")
    gbp = Decimal("100") * Decimal("0.85") + Decimal("250") * Decimal("0.90")
    return Month(eur, gbp, eur * (1 + UPLIFT_A))


async def _generate(step17: Step17, oid: UUID, tenant: UUID, period: str = LABEL) -> Any:
    return await step17.api.post("/v1/reports/packs", oid,
                                 {"tenant_id": str(tenant), "period": period})


def _section(pack: dict[str, Any], key: str) -> dict[str, Any]:
    return next(s for s in pack["sections"] if s["key"] == key)


def _figure(pack: dict[str, Any], section: str, key: str) -> dict[str, Any]:
    return next(f for f in _section(pack, section)["figures"] if f["key"] == key)


async def test_a_pack_is_generated_from_live_rows_in_the_contract_currency(
    step17: Step17, month: Month
) -> None:
    got = await _generate(step17, step17.delivery_a, step17.seeded.a.id)
    assert got.status_code == 201, got.text
    pack = got.json()
    assert (pack["contract_currency"], pack["status"], pack["period"]) == ("EUR", "draft", LABEL)
    spend = _figure(pack, "executive_summary", "cloud_cost")
    assert (spend["kind"], spend["unit"]) == ("money", "EUR")
    assert Decimal(spend["value"]) == month.expected_eur  # unrounded until presentation
    # The approver sees the price the customer will see, beside the cost.
    price = _figure(pack, "executive_summary", "customer_price")
    assert (price["kind"], price["unit"], price["label"]) == ("money", "EUR",
                                                             "Cloud consumption")
    assert Decimal(price["value"]) == month.expected_price_eur
    assert _figure(pack, "volumetrics", "incidents_opened")["value"] == "4"
    assert _figure(pack, "volumetrics", "incidents_closed")["value"] == "3"
    assert Decimal(_figure(pack, "volumetrics", "incident_sla_attainment")["value"]
                   ).quantize(Decimal("0.01")) == Decimal("66.67")
    assert _figure(pack, "vulnerabilities", "open_high")["value"] == "1"
    assert _figure(pack, "vulnerabilities", "remediated_total")["value"] == "1"
    assert [s["key"] for s in pack["sections"]] == [
        "executive_summary", "volumetrics", "financial_dashboard", "vulnerabilities", "csat",
        "availability", "automation_progress", "revenue_mix"]
    assert _section(pack, "csat")["captured"] is False
    assert _section(pack, "availability")["captured"] is False  # no telemetry source here
    # From step 16's latest snapshot, never recomputed: version 2, unrounded.
    assert _section(pack, "financial_dashboard")["captured"] is True
    revenue = _figure(pack, "financial_dashboard", "revenue")
    assert (revenue["value"], revenue["unit"]) == ("41250.500000", "EUR")
    assert "version 2" in revenue["note"]
    assert Decimal(_figure(pack, "financial_dashboard", "margin")["value"]) == Decimal("10250.5")
    assert Decimal(_figure(pack, "financial_dashboard", "fx_impact")["value"]) == Decimal("50.5")
    pct = _figure(pack, "financial_dashboard", "margin_pct")
    assert Decimal(pct["value"]).quantize(Decimal("0.01")) == Decimal("24.85")
    assert pct["note"] == "health band: watch"
    hours = _figure(pack, "financial_dashboard", "effort_hours")
    assert (hours["kind"], hours["note"]) == ("hours", "partial: 1 work items without hours")
    [variance] = _section(pack, "financial_dashboard")["tables"]
    assert variance["rows"] == [["Cloud Ops", "incident", "90.2500", "75.00000000",
                                 "1234.567800"]]
    mix = _section(pack, "revenue_mix")
    assert _figure(pack, "revenue_mix", "monthly_manage_revenue")["value"] == "41250.500000"
    shares = {r[0]: r[2] for r in mix["tables"][0]["rows"]}
    assert Decimal(shares["Hosting (annual contract value)"]).quantize(Decimal("0.01")) == (
        Decimal("19.51"))
    assert len(mix["tables"]) == 1  # every component recorded
    # The summary carries nothing commercial, not even a commercial gap.
    summary = _section(pack, "executive_summary")
    assert not any(f["source_system"] in ("commercial_engine", "sow_register")
                   for f in summary["figures"])
    assert {n["section"] for n in pack["narratives"]} == {
        "executive_summary", "volumetrics", "vulnerabilities", "automation_progress"}
    assert all(n["draft_state"] == "requested" and n["text"] is None for n in pack["narratives"])
    assert pack["releasable"] is False


async def test_commercial_sections_follow_their_own_currency_and_gaps(
    step17: Step17, month: Month
) -> None:
    # Charlie's margin is in USD, as its pack; Bravo has no snapshot and a JPY SOW
    # with no component values recorded.
    c_pack = (await _generate(step17, step17.admin, step17.seeded.c.id)).json()
    margin = _figure(c_pack, "financial_dashboard", "margin")
    assert (Decimal(margin["value"]), margin["unit"]) == (Decimal("-3749.5"), "USD")
    from cloudops_domain.reporting.render import value_text
    assert value_text(margin["value"], "money", "USD") == "USD -3,749.50"
    b_pack = (await _generate(step17, step17.admin, step17.seeded.b.id)).json()
    dash = _section(b_pack, "financial_dashboard")
    assert (dash["captured"], dash["gap"]) == (
        False, "the commercial engine has not computed margin for the month")
    mix = _section(b_pack, "revenue_mix")
    assert [t["key"] for t in mix["tables"]] == ["revenue_mix", "not_recorded"]
    assert all(r[2] == "" for r in mix["tables"][0]["rows"][:2])  # no share of nothing
    gaps = _section(b_pack, "executive_summary")["tables"][0]["rows"]
    assert "Financial dashboard" not in {r[0] for r in gaps}


async def test_commercial_sections_are_stored_apart_for_commercial_roles(
    step17: Step17, month: Month
) -> None:
    """Margin and SOW revenue never sit in report_pack.sections, where every
    reports.read holder could read them; report_pack_commercial carries
    commercial_visibility, so a customer or engineer scope reads no row."""
    s = step17.seeded
    pid = (await _generate(step17, step17.delivery_a, s.a.id)).json()["id"]
    with psycopg.connect(s.admin_dsn) as cx:
        public = cx.execute("SELECT sections FROM cloudops.report_pack WHERE id = %s",
                            (pid,)).fetchone()
    assert public
    assert {"financial_dashboard", "revenue_mix"}.isdisjoint(public[0])
    with psycopg.connect(s.app_dsn) as cx:
        for role, seen in (("delivery_lead", 1), ("engineer", 0), ("customer", 0),
                           ("secops", 0)):
            _as_role(cx, s.a.id, role)
            got = cx.execute("SELECT count(*) FROM cloudops.report_pack_commercial"
                             " WHERE pack_id = %s", (pid,)).fetchone()
            assert got == (seen,), role
            cx.rollback()
        _as_role(cx, s.a.id, "engineer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cx.execute("INSERT INTO cloudops.report_pack_commercial (pack_id, tenant_id,"
                       " sections) VALUES (gen_random_uuid(), %s, '{}')", (s.a.id,))
        cx.rollback()
        _as_role(cx, s.a.id, "delivery_lead")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cx.execute("UPDATE cloudops.report_pack_commercial SET sections = '{}'"
                       " WHERE pack_id = %s", (pid,))


async def test_a_missing_rate_is_a_gap_never_zero(step17: Step17, month: Month) -> None:
    # Charlie's contract is USD and its CHF row has no CHF->USD rate.
    admin_pack = (await _generate(step17, step17.admin, step17.seeded.c.id)).json()
    spend = _figure(admin_pack, "executive_summary", "cloud_cost")
    assert spend["value"] == ""
    assert spend["kind"] == "text"
    assert spend["note"] == f"not shown: no average FX rate for CHF->USD from {D1.isoformat()}"
    # Charlie's SOW has no uplift: the price is a named gap, never the cost.
    price = _figure(admin_pack, "executive_summary", "customer_price")
    assert (price["value"], price["kind"]) == ("", "text")
    assert price["note"].startswith("not captured: no consumption uplift agreed for 1 of 1")


async def test_a_priced_day_without_a_rate_is_a_gap_never_a_partial_price(
    step17: Step17, month: Month
) -> None:
    s = step17.seeded
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.sow SET consumption_uplift = 0.1 WHERE tenant_id = %s",
                   (s.c.id,))
    try:
        pack = (await _generate(step17, step17.admin, s.c.id)).json()
    finally:
        with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
            cx.execute("UPDATE cloudops.sow SET consumption_uplift = NULL WHERE tenant_id = %s",
                       (s.c.id,))
    price = _figure(pack, "executive_summary", "customer_price")
    assert (price["value"], price["kind"]) == ("", "text")
    assert price["note"] == (
        f"not captured: no average FX rate for CHF->USD from {D1.isoformat()}")


def test_only_commercial_roles_may_compute_a_packs_price(step17: Step17, month: Month) -> None:
    """Price over cost is the uplift: the pack's price function answers only the
    roles that may read the SOW, and a customer scope cannot call it at all."""
    s = step17.seeded
    sql = ("SELECT sum(price) FROM cloudops.pack_customer_price(%s, %s, %s)")
    args = (s.a.id, PERIOD, D2)
    with psycopg.connect(s.app_dsn) as cx:
        for role in ("engineer", "secops", "customer", "ingest", ""):
            _as_role(cx, s.a.id, role)
            with pytest.raises(psycopg.errors.InsufficientPrivilege,
                               match="pack_customer_price"):
                cx.execute(sql, args)
            cx.rollback()
        cx.execute("SET LOCAL ROLE cloudops_customer")
        _as_role(cx, s.a.id, "delivery_lead")  # a forged role class does not help
        with pytest.raises(psycopg.errors.InsufficientPrivilege,
                           match="permission denied for function pack_customer_price"):
            cx.execute(sql, args)
        cx.rollback()
        _as_role(cx, s.a.id, "delivery_lead")
        got = cx.execute(sql, args).fetchone()
        assert got
        assert got[0] is not None
        cx.rollback()
        # Row-level security still scopes the definer: another tenant prices nothing.
        _as_role(cx, s.a.id, "delivery_lead")
        assert cx.execute(sql, (s.c.id, PERIOD, D2)).fetchone() == (None,)


async def test_who_may_generate(step17: Step17, month: Month) -> None:
    s = step17.seeded
    assert (await _generate(step17, step17.engineer_a, s.a.id)).status_code == 403
    assert (await _generate(step17, step17.secops_a, s.a.id)).status_code == 403
    assert (await _generate(step17, step17.customer_a, s.a.id)).status_code == 403
    assert (await _generate(step17, step17.delivery_a, s.b.id)).status_code == 404
    assert (await _generate(step17, step17.delivery_limited, s.c.id)).status_code == 404
    before_sow = await _generate(step17, step17.delivery_a, s.a.id, "2025-06")
    assert (before_sow.status_code, before_sow.json()["error"]["code"]) == (
        409, "contract_currency_not_captured")
    future = f"{TODAY.year + 1}-01"
    assert (await _generate(step17, step17.delivery_a, s.a.id, future)).status_code == 422


# The pack's sections as the API assembles them (reports._PACK_COLS), under
# whatever scope runs it: each table joins in only where its policy admits the row.
READABLE = (
    "SELECT p.sections"
    " || coalesce((SELECT r.sections FROM cloudops.report_pack_price r"
    "  WHERE r.pack_id = p.id), '{}'::jsonb)"
    " || coalesce((SELECT c.sections FROM cloudops.report_pack_commercial c"
    "  WHERE c.pack_id = p.id), '{}'::jsonb)"
    " FROM cloudops.report_pack p WHERE p.id = %s")


def _summary_keys(cx: psycopg.Connection[Any], pack: UUID) -> set[str]:
    found = cx.execute(READABLE, (pack,)).fetchone()
    assert found
    return {f["key"] for f in found[0]["executive_summary"]["figures"]}


def _as_role(cx: psycopg.Connection[Any], tenant: UUID, role: str) -> None:
    cx.execute("SELECT set_config('app.tenant_ids', %s, true),"
               " set_config('app.role', %s, true),"
               " set_config('app.account_ids', 'all', true)", (str(tenant), role))


async def test_nothing_is_released_without_a_person(step17: Step17, month: Month) -> None:
    s, api = step17.seeded, step17.api
    pack = (await _generate(step17, step17.delivery_a, s.a.id)).json()
    pid = pack["id"]
    early = await api.post(f"/v1/reports/packs/{pid}/approve", step17.delivery_a)
    assert (early.status_code, early.json()["error"]["code"]) == (409, "narrative_not_reviewed")
    with psycopg.connect(s.app_dsn) as cx:
        _as_role(cx, s.a.id, "platform_admin")
        with pytest.raises(psycopg.errors.CheckViolation, match="person"):
            cx.execute("UPDATE cloudops.report_pack SET status = 'approved', approved_by = %s,"
                       " approved_at = now() WHERE id = %s", (step17.delivery_a, pid))
    for n in pack["narratives"]:
        edited = await api.post(f"/v1/reports/packs/{pid}/narratives/{n['section']}",
                                step17.delivery_a, {"text": f"Reviewed {n['section']}."})
        assert edited.status_code == 200
    # A publisher must be staff holding reports.publish, in the database too.
    with psycopg.connect(s.app_dsn) as cx:
        _as_role(cx, s.a.id, "platform_admin")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match=r"reports\.publish"):
            cx.execute("UPDATE cloudops.report_pack SET status = 'approved', approved_by = %s,"
                       " approved_at = now() WHERE id = %s", (step17.engineer_a, pid))
    assert (await api.post(f"/v1/reports/packs/{pid}/publish", step17.delivery_a)
            ).json()["error"]["code"] == "pack_not_approved"
    assert (await api.post(f"/v1/reports/packs/{pid}/approve", step17.auditor_all)
            ).status_code == 403
    approved = await api.post(f"/v1/reports/packs/{pid}/approve", step17.delivery_a)
    assert approved.json()["status"] == "approved"
    late = await api.post(f"/v1/reports/packs/{pid}/narratives/volumetrics", step17.delivery_a,
                          {"text": "Too late."})
    assert late.json()["error"]["code"] == "pack_not_editable"

    # Customers see a pack only once published (row-level security, not serialisation).
    with psycopg.connect(s.app_dsn) as cx:
        _as_role(cx, s.a.id, "customer")
        seen = cx.execute("SELECT count(*) FROM cloudops.report_pack WHERE id = %s",
                          (pid,)).fetchone()
        texts = cx.execute("SELECT count(*) FROM cloudops.report_narrative WHERE pack_id = %s",
                           (pid,)).fetchone()
        priced = cx.execute("SELECT count(*) FROM cloudops.report_pack_price WHERE pack_id = %s",
                            (pid,)).fetchone()
    assert seen == (0,)
    assert texts == (0,)
    assert priced == (0,)

    draft_pdf = await api.get(f"/v1/reports/packs/{pid}/pdf", step17.auditor_all)
    assert draft_pdf.headers["content-type"] == "application/pdf"
    assert DRAFT_BANNER.encode() in draft_pdf.content
    published = await api.post(f"/v1/reports/packs/{pid}/publish", step17.delivery_a)
    assert published.json()["status"] == "published"
    with psycopg.connect(s.app_dsn) as cx:
        _as_role(cx, s.a.id, "customer")
        seen = cx.execute("SELECT count(*) FROM cloudops.report_pack WHERE id = %s",
                          (pid,)).fetchone()
    assert seen == (1,)
    # Getronics' cloud cost, and anything commercial, never reaches what a customer
    # scope reads: the pack as the API assembles it, under that scope.
    with psycopg.connect(s.app_dsn) as cx:
        _as_role(cx, s.a.id, "customer")
        found = cx.execute(READABLE, (pid,)).fetchone()
    assert found
    readable = found[0]
    assert {"financial_dashboard", "revenue_mix"}.isdisjoint(readable)
    money = [f for sec in readable.values() for f in sec["figures"] if f["kind"] == "money"]
    # The customer's price is the only money a customer reads, in the cost's place.
    assert [(f["key"], f["unit"], Decimal(f["value"])) for f in money] == [
        ("customer_price", "EUR", month.expected_price_eur)]
    assert "cloud_cost" not in {f["key"] for f in readable["executive_summary"]["figures"]}
    # And the same scope is refused the cost itself, and the pack's price function.
    with psycopg.connect(s.app_dsn) as cx:
        cx.execute("SET LOCAL ROLE cloudops_customer")
        _as_role(cx, s.a.id, "customer")
        for sql in ("SELECT unblended_cost FROM cloudops.cost_fact",
                    "SELECT amortized_cost FROM cloudops.cost_fact",
                    "SELECT * FROM cloudops.pack_customer_price("
                    f"'{s.a.id}', '{PERIOD}', '{D2}')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cx.execute(sql)
            cx.rollback()
            cx.execute("SET LOCAL ROLE cloudops_customer")
            _as_role(cx, s.a.id, "customer")
    staff_view = (await api.get(f"/v1/reports/packs/{pid}", step17.delivery_a)).json()
    assert Decimal(_figure(staff_view, "executive_summary", "cloud_cost")["value"]) == (
        month.expected_eur)
    # What was released stays what was approved, for the table owner too.
    with psycopg.connect(s.admin_dsn) as cx:
        for sql in ("UPDATE cloudops.report_narrative SET text = 'Rewritten.'"
                    " WHERE pack_id = %s",
                    "DELETE FROM cloudops.report_narrative WHERE pack_id = %s"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="fixed"):
                cx.execute(sql, (pid,))
            cx.rollback()
    pdf = (await api.get(f"/v1/reports/packs/{pid}/pdf", step17.delivery_a)).content
    # The text endpoint is the PDF's own lines: what the acceptance diff compares.
    lines = (await api.get(f"/v1/reports/packs/{pid}/text", step17.delivery_a)).text.splitlines()
    assert "Reviewed volumetrics." in lines
    assert DRAFT_BANNER not in lines
    assert (f"Cloud cost (unblended): EUR {month.expected_eur.quantize(Decimal('0.01')):,}"
            " (unreconciled; each row at the average rate on its usage date)") in lines
    assert (f"Cloud consumption: EUR {month.expected_price_eur.quantize(Decimal('0.01')):,}"
            " (each day at the average rate on its usage date)") in lines
    assert pdf.startswith(b"%PDF")
    assert DRAFT_BANNER.encode() not in pdf
    assert b"Reviewed volumetrics." in pdf
    spend = f"EUR {month.expected_eur.quantize(Decimal('0.01')):,}".encode()
    assert spend in pdf  # rounded once, at presentation

    # A new version supersedes drafts; the published one stays until its successor is.
    again = (await _generate(step17, step17.delivery_a, s.a.id)).json()
    listed = (await api.get("/v1/reports/packs", step17.delivery_a,
                            params={"tenant_id": str(s.a.id), "period": LABEL})).json()
    status = {p["id"]: p["status"] for p in listed}
    assert status[pid] == "published"
    assert status[again["id"]] == "draft"
    assert int(again["version"]) > int(pack["version"])
    for n in again["narratives"]:
        await api.post(f"/v1/reports/packs/{again['id']}/narratives/{n['section']}",
                       step17.delivery_a, {"text": "Second review."})
    await api.post(f"/v1/reports/packs/{again['id']}/approve", step17.delivery_a)
    second = await api.post(f"/v1/reports/packs/{again['id']}/publish", step17.delivery_a)
    assert second.status_code == 200, second.text
    listed = (await api.get("/v1/reports/packs", step17.delivery_a,
                            params={"tenant_id": str(s.a.id), "period": LABEL})).json()
    first = next(p for p in listed if p["id"] == pid)
    assert first["status"] == "superseded"
    assert first["published_at"] is not None  # the record that it was published stays


async def test_another_tenant_sees_none_of_it(step17: Step17, month: Month) -> None:
    s, api = step17.seeded, step17.api
    with psycopg.connect(s.admin_dsn) as cx:
        b_pack = cx.execute("SELECT id FROM cloudops.report_pack WHERE tenant_id = %s",
                            (s.b.id,)).fetchone()
    assert b_pack
    for path in (f"/v1/reports/packs/{b_pack[0]}", f"/v1/reports/packs/{b_pack[0]}/pdf"):
        assert (await api.get(path, step17.delivery_a)).status_code == 404, path
    for path in (f"/v1/reports/packs/{b_pack[0]}/approve",
                 f"/v1/reports/packs/{b_pack[0]}/narratives/executive_summary/draft"):
        assert (await api.post(path, step17.delivery_a)).status_code == 404, path
    listed = (await api.get("/v1/reports/packs", step17.delivery_a)).json()
    assert {p["tenant"]["id"] for p in listed} == {str(s.a.id)}
    assert (await api.get("/v1/reports/packs", step17.customer_a)).status_code == 403


async def test_the_portfolio_consolidates_the_same_rows_in_the_reporting_currency(
    step17: Step17, month: Month
) -> None:
    s, api = step17.seeded, step17.api
    got = (await api.get("/v1/reports/portfolio", step17.admin,
                         params={"period": LABEL, "currency": "GBP"})).json()
    lines = {c["tenant"]["id"]: c for c in got["customers"]}
    assert Decimal(lines[str(s.a.id)]["consumption"]["value"]) == month.expected_gbp
    assert lines[str(s.a.id)]["consumption"]["unit"] == "GBP"
    # Bravo's JPY row has no rate: a gap, not a smaller total.
    assert lines[str(s.b.id)]["consumption"] is None
    assert "JPY->GBP" in lines[str(s.b.id)]["gap"]
    assert got["complete"] is False
    assert Decimal(lines[str(s.c.id)]["consumption"]["value"]) == Decimal("27")  # 30 CHF x 0.9
    assert Decimal(got["total_consumption"]["value"]) == month.expected_gbp + Decimal("27")
    assert lines[str(s.a.id)]["incidents_opened"]["value"] == 4
    mine = (await api.get("/v1/reports/portfolio", step17.delivery_a,
                          params={"period": LABEL, "currency": "EUR"})).json()
    assert [c["tenant"]["id"] for c in mine["customers"]] == [str(s.a.id)]
    assert Decimal(mine["total_consumption"]["value"]) == month.expected_eur
    assert (await api.get("/v1/reports/portfolio", step17.delivery_limited,
                          params={"period": LABEL, "currency": "EUR"})).status_code == 404


async def test_a_customer_scope_reads_the_granted_report_columns_only(
    step17: Step17, month: Month
) -> None:
    """Customer scopes run as cloudops_customer (migration 0011). Migration 0025
    grants it a pack's snapshot, its price and a section's published text, and
    nothing else: naming who generated or edited a pack, an AI draft or the
    commercial sections is refused, not hidden. Assurance stays ungranted."""
    from sqlalchemy import text

    from cloudops_tenancy import Database, Role, TenantScope

    db = Database.create(step17.seeded.app_dsn, pool_size=1, max_overflow=0)
    try:
        scope = TenantScope([step17.seeded.a.id], Role.CUSTOMER, "test:customer")
        async with db.transaction(scope) as conn:
            for sql in (
                "SELECT id, period, status, sections, published_at FROM cloudops.report_pack",
                "SELECT pack_id, sections FROM cloudops.report_pack_price",
                "SELECT pack_id, section, text, edited_at FROM cloudops.report_narrative",
            ):
                await conn.execute(text(sql))
        for sql in (
            "SELECT generated_by FROM cloudops.report_pack",
            "SELECT approved_by FROM cloudops.report_pack",
            "SELECT published_by FROM cloudops.report_pack",
            "SELECT draft_text FROM cloudops.report_narrative",
            "SELECT draft_state FROM cloudops.report_narrative",
            "SELECT edited_by FROM cloudops.report_narrative",
            "SELECT sections FROM cloudops.report_pack_commercial",
            "SELECT count(*) FROM cloudops.assessment",
            "SELECT count(*) FROM cloudops.evidence",
        ):
            with pytest.raises(Exception, match="permission denied"):
                async with db.transaction(scope) as conn:
                    await conn.execute(text(sql))
    finally:
        await db.dispose()
    with psycopg.connect(step17.seeded.admin_dsn) as cx:
        roles = cx.execute(
            "SELECT DISTINCT unnest(array(SELECT rolname FROM pg_roles WHERE oid = ANY(polroles)))"
            " FROM pg_policy WHERE polname = 'published_only_for_customers' ORDER BY 1").fetchall()
        # The default roles that read packs (0025), and only those.
        holders = cx.execute(
            "SELECT r.name FROM access.role_definition r JOIN access.role_permission p"
            " ON p.role_id = r.id WHERE r.role_class = 'customer'"
            " AND p.permission = 'reports.read' ORDER BY 1").fetchall()
    assert roles == [("cloudops_app",), ("cloudops_customer",)]
    assert holders == [("CXO",), ("VP or director",)]


async def test_a_non_commercial_role_with_reports_and_cost_never_reads_the_price(
    step17: Step17, month: Month
) -> None:
    """reports.read and cost.read are both non-commercial, so an administrator may
    grant both to an Engineer-class role. Price over cost is the uplift: such a
    role must read neither the price nor the cost from a pack, draft or published,
    and the database, not the API, is what refuses it (ADR 0021, decision 8)."""
    s = step17.seeded
    api = step17.api
    pack = (await _generate(step17, step17.delivery_a, s.a.id)).json()
    pid = pack["id"]
    for stage in ("draft", "published"):
        if stage == "published":
            for n in pack["narratives"]:
                await api.post(f"/v1/reports/packs/{pid}/narratives/{n['section']}",
                               step17.delivery_a, {"text": "Reviewed."})
            await api.post(f"/v1/reports/packs/{pid}/approve", step17.delivery_a)
            got = await api.post(f"/v1/reports/packs/{pid}/publish", step17.delivery_a)
            assert got.json()["status"] == "published"
        with psycopg.connect(s.app_dsn) as cx:
            for role in ("engineer", "secops", "", "ingest"):
                _as_role(cx, s.a.id, role)
                assert cx.execute("SELECT count(*) FROM cloudops.report_pack_price"
                                  " WHERE pack_id = %s", (pid,)).fetchone() == (0,), role
                assert _summary_keys(cx, pid).isdisjoint(
                    {"customer_price", "cloud_cost"}), (stage, role)
                cx.rollback()
            # A commercial role sees both; the customer, once published, the price only.
            _as_role(cx, s.a.id, "auditor")
            assert {"customer_price", "cloud_cost"} <= _summary_keys(cx, pid)
            cx.rollback()
            _as_role(cx, s.a.id, "customer")
            if stage == "published":
                keys = _summary_keys(cx, pid)
                assert "customer_price" in keys
                assert "cloud_cost" not in keys
            cx.rollback()
            # Nobody but a commercial role writes a price, and nobody rewrites one.
            for role in ("engineer", "customer"):
                _as_role(cx, s.a.id, role)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cx.execute("INSERT INTO cloudops.report_pack_price (pack_id, tenant_id,"
                               " sections) VALUES (%s, %s, '{}')", (pid, s.a.id))
                cx.rollback()
            _as_role(cx, s.a.id, "delivery_lead")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cx.execute("UPDATE cloudops.report_pack_price SET sections = '{}'"
                           " WHERE pack_id = %s", (pid,))
            cx.rollback()


async def test_a_month_without_billing_rows_has_no_price_never_zero(
    step17: Step17, month: Month
) -> None:
    earlier = (PERIOD - timedelta(days=1)).replace(day=1)  # no cost rows seeded
    got = await _generate(step17, step17.delivery_a, step17.seeded.a.id, f"{earlier:%Y-%m}")
    assert got.status_code == 201, got.text
    price = _figure(got.json(), "executive_summary", "customer_price")
    assert (price["value"], price["kind"], price["note"]) == (
        "", "text", "not captured: no billing rows for this month")


async def test_an_open_month_has_no_price_so_no_day_can_be_isolated(
    step17: Step17, month: Month
) -> None:
    """ADR 0021, decision 9: a pack regenerated day by day through the current
    month would show each day's price, and from it the uplift."""
    got = await _generate(step17, step17.delivery_a, step17.seeded.a.id, f"{TODAY:%Y-%m}")
    assert got.status_code == 201, got.text
    pack = got.json()
    price = _figure(pack, "executive_summary", "customer_price")
    assert (price["value"], price["kind"], price["note"]) == (
        "", "text", "not captured: the month is not closed; the price is shown for closed"
        " months")
    # Staff still see the month's cost to date.
    assert _figure(pack, "executive_summary", "cloud_cost")["kind"] == "money"


# --- the portal: customers read their published packs (migration 0025, ADR 0030) ---


@dataclass(frozen=True)
class Portal:
    cxo_a: UUID  # tenant a's first administrator, a CXO
    vp_a: UUID  # added by a's administrator
    cxo_b: UUID


@pytest.fixture(scope="module")
async def portal(step17: Step17) -> Portal:
    """Tenants a and b onboarded to the portal, as a platform administrator does."""
    from cloudops_tenancy import Database, TenantScope

    s = step17.seeded
    users = Portal(uuid4(), uuid4(), uuid4())
    db = Database.create(s.app_dsn, pool_size=1, max_overflow=0)
    try:
        async with db.transaction(TenantScope.for_resolution("test:portal-packs")) as conn:
            for tenant, admin in ((s.a.id, users.cxo_a), (s.b.id, users.cxo_b)):
                await onboard_portal(conn, step17.admin, tenant, admin)
    finally:
        await db.dispose()
    assert "VP or director" in AGREED_ROLES
    added = await step17.api.post("/v1/tenant/users", users.cxo_a, {
        "oid": str(users.vp_a), "display_name": "VP", "role": "VP or director"})
    assert added.status_code == 200, added.text
    return users


async def _published(step17: Step17, oid: UUID, tenant: UUID) -> dict[str, Any]:
    api = step17.api
    pack = await _generate(step17, oid, tenant)
    assert pack.status_code == 201, pack.text
    pid = pack.json()["id"]
    for n in pack.json()["narratives"]:
        await api.post(f"/v1/reports/packs/{pid}/narratives/{n['section']}", oid,
                       {"text": f"Published {n['section']}."})
    assert (await api.post(f"/v1/reports/packs/{pid}/approve", oid)).status_code == 200
    got = await api.post(f"/v1/reports/packs/{pid}/publish", oid)
    assert got.status_code == 200, got.text
    return dict(got.json())


async def test_a_customer_reads_its_published_packs_and_nothing_else(
    step17: Step17, month: Month, portal: Portal
) -> None:
    s, api = step17.seeded, step17.api
    mine = await _published(step17, step17.delivery_a, s.a.id)
    theirs = await _published(step17, step17.admin, s.b.id)
    draft = (await _generate(step17, step17.delivery_a, s.a.id)).json()

    me = (await api.get("/v1/me", portal.cxo_a)).json()
    assert me["kind"] == "customer"
    assert "reports.read" in me["permissions"]
    for oid in (portal.cxo_a, portal.vp_a):
        listed = (await api.get("/v1/reports/packs", oid)).json()
        assert mine["id"] in {p["id"] for p in listed}
        assert {p["tenant"]["id"] for p in listed} == {str(s.a.id)}
        assert {p["status"] for p in listed} <= {"published", "superseded"}
        assert draft["id"] not in {p["id"] for p in listed}
        # Another tenant's published pack, and the customer's own draft, do not exist.
        for pid in (theirs["id"], draft["id"]):
            for suffix in ("", "/pdf", "/text"):
                got = await api.get(f"/v1/reports/packs/{pid}{suffix}", oid)
                assert got.status_code == 404, (pid, suffix)

    pack = (await api.get(f"/v1/reports/packs/{mine['id']}", portal.cxo_a)).json()
    keys = {sec["key"] for sec in pack["sections"]}
    assert {"financial_dashboard", "revenue_mix"}.isdisjoint(keys)
    summary = {f["key"] for f in _section(pack, "executive_summary")["figures"]}
    assert "customer_price" in summary
    assert "cloud_cost" not in summary
    price = _figure(pack, "executive_summary", "customer_price")
    assert Decimal(price["value"]) == month.expected_price_eur
    money = [f for sec in pack["sections"] for f in sec["figures"] if f["kind"] == "money"]
    assert [f["key"] for f in money] == ["customer_price"]
    assert pack["narratives"]
    for n in pack["narratives"]:
        assert n["text"] == f"Published {n['section']}."
        assert (n["draft_state"], n["draft_text"], n["drafted_at"], n["draft_note"],
                n["edited_by"]) == (None, None, None, None, None)

    lines = (await api.get(f"/v1/reports/packs/{mine['id']}/text", portal.cxo_a)).text
    assert "Cloud consumption: EUR" in lines
    assert "Cloud cost" not in lines
    pdf = await api.get(f"/v1/reports/packs/{mine['id']}/pdf", portal.vp_a)
    assert pdf.content.startswith(b"%PDF")
    assert DRAFT_BANNER.encode() not in pdf.content

    # Staff work stays staff work, whoever holds reports.read.
    period = {"period": LABEL, "currency": "EUR"}
    assert (await api.get("/v1/reports/portfolio", portal.cxo_a, params=period)
            ).status_code == 403
    assert (await _generate(step17, portal.cxo_a, s.a.id)).status_code == 403
    for action in ("approve", "publish"):
        assert (await api.post(f"/v1/reports/packs/{draft['id']}/{action}", portal.cxo_a)
                ).status_code == 403
    assert (await api.post(f"/v1/reports/packs/{draft['id']}/narratives/volumetrics",
                           portal.cxo_a, {"text": "x"})).status_code == 403
    # A customer role without the permission still reads none.
    assert (await api.get("/v1/reports/packs", step17.customer_a)).status_code == 403
