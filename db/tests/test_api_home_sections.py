"""Home dashboard sections (ADR 0038): operations, revenue, security, platform, top
customers, and the size band settings.

Against their own database, seeded like ``seeded`` and then given a past month's
work, cost in three currencies with rates, margin snapshots, vulnerabilities and
a fourth customer priced through its SOW, so no other suite's counts move. Every
figure below is in the month before last, wholly in the past, so the counts do not
depend on the day the suite runs.

Each section is checked for its figures, for the date range and compare (the same
endpoint over two ranges), for what other tenants' rows do (nothing, unless the
caller's scope includes them), for the customer selector's narrowing, and for
what a customer and a role without the permission are refused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role

from .conftest import Api, Seeded, _access, _database, _seeded, onboard_portal
from .support import raw_scope, scope

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Sections:
    seeded: Seeded
    api: Api
    db: Database
    delta: UUID  # a fourth customer, in USD on AWS, with a consumption uplift
    admin: UUID  # platform administrator, all tenants: every permission
    engineer_a: UUID  # Engineer, tenant a: tickets, vulnerabilities, inventory; no cost
    finops_all: UUID  # FinOps, all tenants: cost and commercial, no tickets
    ceo_all: UUID  # CEO, all tenants: operations, cost and commercial
    cxo_a: UUID  # customer CXO of a: tickets, cost (its price), vulnerabilities
    security_a: UUID  # customer Security officer of a: vulnerabilities, no tickets
    month: tuple[date, date]  # the month before last, first and last day
    before: tuple[date, date]  # the month before that


def _first_of_previous(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def _months(today: date) -> tuple[tuple[date, date], tuple[date, date]]:
    # Two months back, so the base seed's rows (a few days old) never fall in it.
    start = _first_of_previous(_first_of_previous(today))
    end = _first_of_previous(today) - timedelta(days=1)
    earlier = _first_of_previous(start)
    return (start, end), (earlier, start - timedelta(days=1))


def _at(d: date, hours: float = 0) -> datetime:
    return datetime.combine(d, time.min, UTC) + timedelta(hours=hours)


def _ticket(cx: psycopg.Connection[Any], tenant: UUID, number: str, kind: str,
            priority: str | None, opened: datetime, closed: datetime | None = None,
            *, breached: bool | None = None) -> None:
    cx.execute(
        "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
        " closed_at, priority, sla_breached, resolve_time_seconds, assignment_workgroup)"
        " VALUES (%s, %s, %s, 'SECTIONS', %s, %s, %s, %s, %s, 'GTN Azure L2')",
        (tenant, kind, number, opened, closed, priority, breached,
         None if closed is None else int((closed - opened).total_seconds())),
    )


def _populate(s: Seeded, delta: UUID, delta_account: UUID, a_aws: UUID,
              month: tuple[date, date], before: tuple[date, date]) -> None:
    start, end = month
    mid = start + timedelta(days=9)
    a, b = s.a.id, s.b.id
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        # Everything the platform section counts existed long before these months.
        cx.execute("UPDATE cloudops.tenant SET created_at = now() - interval '400 days'")
        cx.execute("UPDATE cloudops.resource SET first_seen = now() - interval '400 days'")
        # Tenant a, that month: four incidents, a request, a change and a project task.
        _ticket(cx, a, "S-A-P1-FAST", "incident", "1 - Critical", _at(mid), _at(mid, 3))
        _ticket(cx, a, "S-A-P1-SLOW", "incident", "1 - Critical", _at(mid), _at(mid, 6),
                breached=True)
        _ticket(cx, a, "S-A-P2-OPEN", "incident", "2 - High", _at(mid, 1))
        _ticket(cx, a, "S-A-P3", "incident", "3 - Moderate", _at(mid, 2), _at(mid, 12))
        _ticket(cx, a, "S-A-REQ", "service_request", "4 - Low", _at(mid, 3))
        _ticket(cx, a, "S-A-CHG", "change_task", None, _at(mid, 4), _at(mid, 5))
        _ticket(cx, a, "S-A-PRJ", "project_task", None, _at(mid, 5))
        # The month before: one incident, to compare against.
        _ticket(cx, a, "S-A-EARLIER", "incident", "3 - Moderate", _at(before[0], 30))
        # Tenant b, that month: one open P1. Only a scope with b sees it.
        _ticket(cx, b, "S-B-P1", "incident", "1", _at(mid))
        for number, route in (("S-A-P1-FAST", "ready_for_planning"), ("S-A-P2-OPEN", "human")):
            cx.execute(
                "INSERT INTO cloudops.triage_result (tenant_id, number, route, stage,"
                " classification, cost_currency, workflow_id)"
                " VALUES (%s, %s, %s, 3, '{}', 'USD', %s)", (a, number, route, f"s-{number}"))
        # Vulnerabilities: found and fixed that month; open since long before.
        for qid, sev, first, fixed in ((70001, 5, _at(start, 100), _at(start, 400)),
                                       (70002, 5, _at(end) - timedelta(days=100), None),
                                       (70003, 2, _at(start, 50), None)):
            cx.execute(
                "INSERT INTO cloudops.vulnerability (tenant_id, qid, asset_name, scope, title,"
                " severity, first_detected, remediated_at)"
                " VALUES (%s, %s, 'vm-alpha-01', 'os', 'Finding', %s, %s, %s)",
                (a, qid, sev, first, fixed))
        for fid, category, severity in (("s-ev-1", "security", "medium"),
                                        ("s-cmp-1", "compliance", "low")):
            cx.execute(
                "INSERT INTO cloudops.security_finding (tenant_id, source_system, source_scope,"
                " finding_id, category, severity, title, status, customer_raw, detected_at,"
                " updated_at) VALUES (%s, 'corestack', 'cs', %s, %s, %s, 'Event', 'open',"
                " 'alpha', %s, %s)", (a, fid, category, severity, _at(mid), _at(mid)))
        # Rates in force before that month: USD and GBP to EUR.
        for frm, rate in (("USD", "0.90000000"), ("GBP", "1.15000000")):
            cx.execute(
                "INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate, rate_type,"
                " effective_date, source) VALUES (%s, 'EUR', %s, 'average', %s, 'test')",
                (frm, rate, before[0]))
        # Cost that month: a 60,000 EUR on Azure and 500 USD on AWS; delta 1,000 USD.
        for tenant, account, amount, ccy in ((a, s.a.account_id, "60000.000000", "EUR"),
                                             (a, a_aws, "500.000000", "USD"),
                                             (delta, delta_account, "1000.000000", "USD")):
            cx.execute(
                "INSERT INTO cloudops.cost_fact (tenant_id, account_id, usage_date,"
                " invoice_period, service_native, unblended_cost, amortized_cost, currency)"
                " VALUES (%s, %s, %s, %s, 'Compute', %s, %s, %s)",
                (tenant, account, mid, f"{mid:%Y-%m}", amount, amount, ccy))
        # Managed services that month: a closed and measured in EUR; b provisional
        # and modelled in GBP.
        for tenant, ccy, status, basis, revenue, actual, modelled in (
            (a, "EUR", "closed", "actual", "5000.000000", "1500.000000", "1400.000000"),
            (b, "GBP", "provisional", "modelled", "2000.000000", "380.000000", "400.000000"),
        ):
            cx.execute(
                "INSERT INTO cloudops.margin_snapshot (tenant_id, period, through, status,"
                " rate_card_version, contract_currency, revenue, modelled_margin,"
                " actual_margin, band_basis, effort_complete) VALUES (%s, %s, %s, %s,"
                " '2026-v2.14', %s, %s, %s, %s, %s, true)",
                (tenant, start, end, status, ccy, revenue, modelled, actual, basis))
        # Tenant b: 250 resources (Medium), four critical CIs of which two stopped.
        cx.execute(
            "INSERT INTO cloudops.resource (tenant_id, account_id, native_id, name,"
            " type_native, type_canonical, is_critical_ci, state, first_seen)"
            " SELECT %s, %s, '/r/b/' || g, 'r-b-' || g, 'x', 'x', g <= 4,"
            "   CASE WHEN g <= 2 THEN 'Stopped' ELSE 'running' END, now() - interval '400 days'"
            " FROM generate_series(1, 250) g", (b, s.b.account_id))
        # Seeded resources are critical by default: only b's four above are.
        cx.execute("UPDATE cloudops.resource SET is_critical_ci = false"
                   " WHERE native_id NOT LIKE '/r/b/%%'")


@pytest.fixture(scope="module")
async def sections(admin_server: str) -> AsyncIterator[Sections]:
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from cloudops_api.app import create_app
    from cloudops_api.auth import StaticKeys, TokenValidator
    from cloudops_config import AuthConfig
    from cloudops_devdata.tokens import new_signing_key
    from cloudops_domain.assurance.pack import Ed25519Signer
    from cloudops_tenancy import TenantScope

    month, before = _months(datetime.now(UTC).date())
    with _database(admin_server, "sections") as provisioned:
        seeded = _seeded(provisioned)
        delta, delta_account, a_aws, internal = uuid4(), uuid4(), uuid4(), uuid4()
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code)"
                       " VALUES (%s, 'delta', 'DELTA')", (delta,))
            cx.execute("INSERT INTO cloudops.sow (tenant_id, reference, effective_from,"
                       " currency, monthly_manage_revenue, consumption_uplift)"
                       " VALUES (%s, 'SOW-delta', '2020-01-01', 'USD', 0, 0.100000)", (delta,))
            cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code, internal)"
                       " VALUES (%s, 'Getronics lab', 'GTNLAB', true)", (internal,))
            for tenant, account, provider in ((delta, delta_account, "aws"),
                                              (seeded.a.id, a_aws, "aws"),
                                              (internal, uuid4(), "azure")):
                cx.execute("INSERT INTO cloudops.cloud_account (id, tenant_id, provider,"
                           " native_id, display_name) VALUES (%s, %s, %s, %s, 'acct')",
                           (account, tenant, provider, f"acct-{account}"))
        _populate(seeded, delta, delta_account, a_aws, month, before)
        admin, engineer_a, finops_all, ceo_all, cxo_a, security_a = (uuid4() for _ in range(6))
        db = Database.create(seeded.app_dsn, pool_size=4, max_overflow=0)
        a = seeded.a.id
        async with db.transaction(TenantScope.for_resolution("setup:sections")) as conn:
            await _access(conn, "SELECT access.bootstrap_admin(:o, 'Admin')", o=admin)
            for oid, role, granted, everyone in (
                (engineer_a, "Engineer", a, False),
                (finops_all, "FinOps", None, True),
                (ceo_all, "CEO", None, True),
            ):
                await _access(conn, "SELECT access.set_principal(:a, :o, 'staff', :n, :r, :all)",
                              a=admin, o=oid, n=f"{role} {oid}", r=role, all=everyone)
                if granted is not None:
                    await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)",
                                  a=admin, o=oid, t=granted)
            await onboard_portal(conn, admin, a, cxo_a)
            await _access(conn, "SELECT access.set_principal(:a, :o, 'customer', 'Security',"
                          " 'Security officer', false)", a=admin, o=security_a)
            await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)",
                          a=admin, o=security_a, t=a)
        auth = AuthConfig(
            issuer="https://login.example.test/77777777-7777-4777-8777-777777777777/v2.0",
            audience="api://cloudops-sections", tenant_id="77777777-7777-4777-8777-777777777777",
            required_scope="access_as_user", jwks_url=None, jwks_secret=None,
        )
        private, public = new_signing_key("sections")
        app = create_app(db=db, validator=TokenValidator(auth, StaticKeys(public)),
                         signer=Ed25519Signer(Ed25519PrivateKey.generate()))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://api") as client:
                yield Sections(seeded, Api(client, private, auth), db, delta, admin,
                               engineer_a, finops_all, ceo_all, cxo_a, security_a, month,
                               before)
        finally:
            await db.dispose()


async def _get(s: Sections, path: str, oid: UUID, status: int = 200,
               **params: Any) -> Any:
    query = {k: str(v) for k, v in params.items() if v is not None}
    response = await s.api.get(path, oid, params=query)
    assert response.status_code == status, (path, params, response.text)
    return response.json()


def _span(r: tuple[date, date]) -> dict[str, Any]:
    return {"start": r[0].isoformat(), "end": r[1].isoformat()}


def _tenants(body: Any) -> set[str]:
    found: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if set(v) == {"id", "name"}:
                found.add(v["id"])
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(body)
    return found


OPS = "/v1/home/sections/operations"
REVENUE = "/v1/home/sections/revenue"
SECURITY = "/v1/home/sections/security"
PLATFORM = "/v1/home/sections/platform"
TOP = "/v1/home/sections/top-customers"
SETTINGS = "/v1/settings/size-bands"

# --- operations ------------------------------------------------------------------------


async def test_received_by_kind_and_priority(sections: Sections) -> None:
    body = await _get(sections, OPS, sections.engineer_a, **_span(sections.month))
    assert body["range"]["start"] == sections.month[0].isoformat()
    kinds = {k["kind"]: k for k in body["received"]}
    assert list(kinds)[:4] == ["incident", "service_request", "change_task", "project_task"]
    assert {k: v["received"]["value"] for k, v in kinds.items()} == {
        "incident": 4, "service_request": 1, "change_task": 1, "project_task": 1}
    incidents = {p["priority"]: p["received"]["value"] for p in kinds["incident"]["by_priority"]}
    assert incidents == {"1": 2, "2": 1, "3": 1, "4": 0, "5": 0, "none": 0}
    assert body["received_total"]["value"] == 7


async def test_triage_and_remediation(sections: Sections) -> None:
    body = await _get(sections, OPS, sections.engineer_a, **_span(sections.month))
    t = body["triage"]
    assert (t["eligible"]["value"], t["triaged"]["value"], t["ready_for_planning"]["value"],
            t["human"]["value"]) == (5, 2, 1, 1)
    assert Decimal(t["share"]["value"]) == Decimal("0.4")
    # The execution plane is not built: zero, with its reason, never hidden.
    r = body["remediation"]
    assert (r["available"], r["reason"], r["auto_remediated"]["value"]) == (
        False, "execution_plane_not_built", 0)


async def test_sla_attainment_is_modelled_against_the_targets(sections: Sections) -> None:
    body = await _get(sections, OPS, sections.engineer_a, **_span(sections.month))
    sla = {line["priority"]: line for line in body["sla"]}
    p1 = sla["1"]
    # 3h and 6h against a 4h target: one of two.
    assert (p1["resolved"]["value"], p1["within_target"]["value"]) == (2, 1)
    assert Decimal(p1["attainment"]["value"]) == Decimal("0.5")
    assert p1["attainment"]["is_modelled"] is True
    assert p1["target_hours"]["value"] == 4
    assert p1["breached_flagged"]["value"] == 1  # ServiceNow's own flag, apart
    assert Decimal(sla["3"]["attainment"]["value"]) == Decimal(1)
    assert sla["2"]["attainment"] is None  # nothing resolved: absent, not zero
    assert sla["5"]["target_hours"] is None  # no target: says so
    assert sla["5"]["within_target"] is None
    assert Decimal(body["attainment"]["value"]) == Decimal(2) / Decimal(3)
    assert body["attainment"]["is_modelled"] is True


async def test_compare_is_the_same_section_over_another_range(sections: Sections) -> None:
    earlier = await _get(sections, OPS, sections.engineer_a, **_span(sections.before))
    later = await _get(sections, OPS, sections.engineer_a, **_span(sections.month))
    count = {b["range"]["start"]: {k["kind"]: k["received"]["value"] for k in b["received"]}
             for b in (earlier, later)}
    assert count[sections.before[0].isoformat()]["incident"] == 1
    assert count[sections.month[0].isoformat()]["incident"] == 4


@pytest.mark.parametrize("params", [
    {"start": "2026-10-02", "end": "2026-10-01"},
    {"start": "2024-01-01", "end": "2026-01-01"},
])
async def test_a_bad_range_is_refused(sections: Sections, params: dict[str, str]) -> None:
    await _get(sections, OPS, sections.engineer_a, 422, **params)


@pytest.mark.isolation
async def test_operations_follow_scope_and_the_selector(sections: Sections) -> None:
    mine = await _get(sections, OPS, sections.engineer_a, **_span(sections.month))
    everyone = await _get(sections, OPS, sections.admin, **_span(sections.month))
    incidents = {b["received"][0]["received"]["value"] for b in (mine,)}
    assert incidents == {4}
    assert everyone["received"][0]["received"]["value"] == 5  # b's P1 too
    # The selector narrows the admin to a: exactly the engineer's figures.
    narrowed = await _get(sections, OPS, sections.admin, tenant_id=sections.seeded.a.id,
                          **_span(sections.month))
    assert narrowed["received"] == mine["received"]
    # It never widens: a tenant outside scope is not found.
    await _get(sections, OPS, sections.engineer_a, 404, tenant_id=sections.seeded.b.id,
               **_span(sections.month))


@pytest.mark.isolation
async def test_a_customer_reads_its_own_operations_without_triage(sections: Sections) -> None:
    body = await _get(sections, OPS, sections.cxo_a, **_span(sections.month))
    staff = await _get(sections, OPS, sections.engineer_a, **_span(sections.month))
    assert body["triage"] is None  # triage is staff-only
    assert body["received"] == staff["received"]
    assert body["sla"] == staff["sla"]
    await _get(sections, OPS, sections.cxo_a, 404, tenant_id=sections.seeded.b.id)
    await _get(sections, OPS, sections.security_a, 403)  # no tickets.read
    await _get(sections, OPS, sections.finops_all, 403)


# --- revenue ---------------------------------------------------------------------------


async def test_revenue_in_transaction_currencies_and_converted(sections: Sections) -> None:
    body = await _get(sections, REVENUE, sections.finops_all, currency="EUR",
                      **_span(sections.month))
    assert (body["view"], body["commercial"]) == ("staff", True)
    hosting = {(h["provider"], h["currency"]): h for h in body["hosting"]}
    assert hosting[("azure", "EUR")]["cost"]["value"] == "60000.000000"
    aws = hosting[("aws", "USD")]
    assert aws["cost"]["value"] == "1500.000000"  # never added to the EUR line
    assert Decimal(aws["cost_converted"]["value"]) == Decimal("1350")
    assert aws["price"] is None  # a's SOW has no uplift: the line has no price
    managed = {m["currency"]: m for m in body["managed"]}
    assert managed["EUR"]["revenue"]["value"] == "5000.000000"
    assert Decimal(managed["EUR"]["margin"]["value"]) == Decimal(1500)
    assert managed["EUR"]["margin"]["is_modelled"] is False
    assert Decimal(managed["GBP"]["margin"]["value"]) == Decimal(400)
    assert managed["GBP"]["margin"]["is_modelled"] is True  # the engine modelled it
    assert managed["GBP"]["provisional"]["value"] == 1
    assert Decimal(managed["GBP"]["margin_converted"]["value"]) == Decimal(460)
    totals = body["totals"]
    assert Decimal(totals["hosting_cost"]["value"]) == Decimal("61350")
    assert Decimal(totals["managed_revenue"]["value"]) == Decimal(7300)
    assert Decimal(totals["managed_margin"]["value"]) == Decimal(1960)
    assert totals["revenue"] is None  # a hosting line has no price: no total
    assert body["projects"] == {"available": False, "reason": "no_data_source",
                                "new_project_price": None, "proposed_price": None}


async def test_the_hosting_price_follows_the_sow_uplift(sections: Sections) -> None:
    body = await _get(sections, REVENUE, sections.admin, currency="EUR",
                      tenant_id=sections.delta, **_span(sections.month))
    [line] = body["hosting"]
    assert (line["provider"], line["currency"]) == ("aws", "USD")
    assert Decimal(line["price"]["value"]) == Decimal("1100")
    assert Decimal(line["price_converted"]["value"]) == Decimal("990")
    assert Decimal(body["totals"]["revenue"]["value"]) == Decimal("990")
    assert _tenants(body) == set()  # totals only: no tenant named


async def test_without_a_currency_nothing_is_totalled(sections: Sections) -> None:
    body = await _get(sections, REVENUE, sections.finops_all, **_span(sections.month))
    assert body["totals"] is None
    assert all(h["cost_converted"] is None for h in body["hosting"])


@pytest.mark.isolation
async def test_a_customer_sees_its_price_never_cost_or_margin(sections: Sections) -> None:
    body = await _get(sections, REVENUE, sections.cxo_a, currency="EUR",
                      **_span(sections.month))
    assert (body["view"], body["commercial"]) == ("customer", False)
    assert (body["hosting"], body["managed"], body["projects"], body["totals"]) == (
        None, None, None, None)
    assert body["customer_price"]  # that month's lines, by provider and currency
    assert "60000" not in str(body)
    assert "margin" not in str(body)
    await _get(sections, REVENUE, sections.security_a, 403)  # no cost.read
    await _get(sections, REVENUE, sections.engineer_a, 403)


@pytest.mark.isolation
async def test_one_customer_chosen_holds_that_customer_only(sections: Sections) -> None:
    """The selector narrows the CEO to a: no other tenant's cost or margin is in it."""
    a_only = await _get(sections, REVENUE, sections.ceo_all, currency="EUR",
                        tenant_id=sections.seeded.a.id, **_span(sections.month))
    managed = {m["currency"] for m in a_only["managed"]}
    assert managed == {"EUR"}  # b's GBP month is not in a's view
    hosting = {(h["provider"], h["currency"]): h["cost"]["value"] for h in a_only["hosting"]}
    assert hosting == {("aws", "USD"): "500.000000", ("azure", "EUR"): "60000.000000"}


# --- security --------------------------------------------------------------------------


async def test_vulnerabilities_open_at_the_end_by_severity_and_age(sections: Sections) -> None:
    body = await _get(sections, SECURITY, sections.engineer_a, **_span(sections.month))
    sev = {v["severity"]: v for v in body["vulnerabilities"]}
    assert list(sev) == [5, 4, 3, 2, 1]
    five = sev[5]
    # 70001 was found and fixed in the month; 70002 is open, 100 days old at the end.
    assert (five["open"]["value"], five["found"]["value"], five["fixed"]["value"]) == (1, 1, 1)
    ages = {a["band"]: a["open"]["value"] for a in five["by_age"]}
    assert ages == {"0-30": 0, "31-90": 0, "91+": 1}
    assert sev[2]["open"]["value"] == 1
    # The seeded severity 4 was first detected three days ago: not open at the end.
    assert sev[4]["open"]["value"] == 0
    assert body["open_total"]["value"] == 2
    assert body["assessments"] is None  # the engineer has no assurance.read
    events = {e["severity"]: e for e in body["events"]["by_severity"]}
    assert (events["medium"]["detected"]["value"], events["medium"]["open"]["value"]) == (1, 1)
    assert body["events"]["compliance_open"]["value"] == 1


async def test_security_compare_moves_with_the_range(sections: Sections) -> None:
    earlier = await _get(sections, SECURITY, sections.engineer_a, **_span(sections.before))
    assert next(v for v in earlier["vulnerabilities"] if v["severity"] == 5)["found"][
        "value"] == 0


@pytest.mark.isolation
async def test_a_customer_reads_vulnerabilities_only(sections: Sections) -> None:
    body = await _get(sections, SECURITY, sections.security_a, **_span(sections.month))
    staff = await _get(sections, SECURITY, sections.engineer_a, **_span(sections.month))
    assert body["vulnerabilities"] == staff["vulnerabilities"]
    assert (body["assessments"], body["events"]) == (None, None)
    admin = await _get(sections, SECURITY, sections.admin, **_span(sections.month))
    assert admin["assessments"] == []  # permitted; no assessment opened yet


# --- platform --------------------------------------------------------------------------


async def test_customers_by_cloud_and_size_band(sections: Sections) -> None:
    body = await _get(sections, PLATFORM, sections.admin, **_span(sections.month))
    assert body["customers"]["value"] == 4  # a, b, c and delta; the lab is internal
    assert body["internal"]["value"] == 1
    clouds = {p["provider"]: p["customers"]["value"] for p in body["by_provider"]}
    # c's second subscription is on AWS (conftest): a and c are on both.
    assert clouds == {"azure": 3, "aws": 3}
    assert body["both_clouds"]["value"] == 2
    assert body["spend_month"] == f"{sections.month[0]:%Y-%m}"
    sizes = {s["tenant"]["name"]: s for s in body["sizes"]}
    a = sizes["alpha"]
    assert Decimal(a["spend"]["value"]) == Decimal("60450")  # 60,000 EUR + 500 USD at 0.9
    assert (a["spend_band"], a["resource_band"], a["band"]) == ("large", "small", "large")
    b = sizes["bravo"]
    assert (b["spend"], b["spend_reason"], b["resource_band"], b["band"]) == (
        None, "no_cost", "medium", "medium")
    assert b["resources"]["value"] == 251
    assert sizes["delta"]["band"] == "small"
    assert body["sizes"][0]["tenant"]["name"] == "alpha"  # largest first
    assert {x["band"]: x["customers"]["value"] for x in body["bands"]} == {
        "large": 1, "medium": 1, "small": 2}
    assert body["policy"]["version"]["value"] == 1


@pytest.mark.isolation
async def test_platform_is_staff_and_spend_needs_cost_read(sections: Sections) -> None:
    await _get(sections, PLATFORM, sections.cxo_a, 403)
    mine = await _get(sections, PLATFORM, sections.engineer_a, **_span(sections.month))
    assert _tenants(mine) == {str(sections.seeded.a.id)}
    [a] = mine["sizes"]
    # No cost.read: the band is the resources' alone, and says why.
    assert (a["spend"], a["spend_reason"], a["band"]) == (None, "not_permitted", "small")


# --- top customers ---------------------------------------------------------------------


async def test_top_customers_by_revenue_and_by_health(sections: Sections) -> None:
    by_revenue = await _get(sections, TOP, sections.ceo_all, sort="revenue",
                            **_span(sections.month))
    assert by_revenue["sort"] == "revenue"
    names = [c["tenant"]["name"] for c in by_revenue["customers"]]
    # b 2,000 GBP at 1.15; delta 1,100 USD at 0.9; c nothing; a has no price (last).
    assert names == ["bravo", "delta", "charlie", "alpha"]
    assert [c["rank"]["value"] for c in by_revenue["customers"]] == [1, 2, 3, 4]
    bravo = by_revenue["customers"][0]
    assert Decimal(bravo["revenue"]["total"]["value"]) == Decimal(2300)
    assert by_revenue["customers"][3]["revenue"]["total"] is None
    by_health = await _get(sections, TOP, sections.ceo_all, sort="health",
                           **_span(sections.month))
    order = [(c["tenant"]["name"], Decimal(c["health"]["value"]))
             for c in by_health["customers"]]
    # alpha: an open P2 (10) and a 100-day severity 5 (2); bravo: b's P1 (20) and half
    # its critical CIs stopped (10); delta beats charlie on revenue.
    assert order == [("bravo", Decimal(70)), ("alpha", Decimal(88)),
                     ("delta", Decimal(100)), ("charlie", Decimal(100))]
    assert all(c["health"]["is_modelled"] for c in by_health["customers"])
    parts = {p["part"]: p for p in by_health["customers"][0]["parts"]}
    assert parts["availability"]["observed"]["value"] == 2
    assert Decimal(parts["availability"]["penalty"]["value"]) == Decimal(10)
    assert parts["p1_p2"]["penalty"]["is_modelled"] is True


@pytest.mark.isolation
async def test_top_customers_refusals_and_scope(sections: Sections) -> None:
    await _get(sections, TOP, sections.cxo_a, 403)
    await _get(sections, TOP, sections.finops_all, 403)  # no tickets.read
    mine = await _get(sections, TOP, sections.engineer_a, **_span(sections.month))
    assert (mine["sort"], mine["commercial"]) == ("health", False)
    assert [c["tenant"]["id"] for c in mine["customers"]] == [str(sections.seeded.a.id)]
    assert mine["customers"][0]["revenue"] is None
    # Revenue is commercial: sorting by it without commercial.read is refused.
    await _get(sections, TOP, sections.engineer_a, 403, sort="revenue")


# --- size band settings ----------------------------------------------------------------


@pytest.mark.isolation
async def test_the_size_band_table_is_staff_only_and_immutable(sections: Sections) -> None:
    with pytest.raises(Exception, match="permission denied"):
        async with sections.db.transaction(scope(sections.seeded.a, role=Role.CUSTOMER)) as c:
            await c.scalar(text("SELECT count(*) FROM cloudops.size_band_policy"))
    async with sections.db.transaction(scope(sections.seeded.a)) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM cloudops.size_band_policy")) >= 1
    for role in ("", "customer"):
        with raw_scope(sections.seeded.app_dsn, str(sections.seeded.a.id), role) as cx:
            assert cx.execute("SELECT count(*) FROM cloudops.size_band_policy"
                              ).fetchone() == (0,)
    with psycopg.connect(sections.seeded.admin_dsn) as cx, pytest.raises(
            psycopg.errors.InsufficientPrivilege, match="history"):
        cx.execute("UPDATE cloudops.size_band_policy SET spend_large_above = 1")


@pytest.mark.isolation
def test_the_size_band_policies_are_exactly_these(sections: Sections) -> None:
    """No tenant isolation to widen (no tenant): a read for staff scopes, an insert
    for the setting function's owner only, and FORCE so the owner is held too."""
    with psycopg.connect(sections.seeded.admin_dsn) as cx:
        found = cx.execute(
            "SELECT p.polname, p.polpermissive, p.polcmd::text,"
            " array(SELECT rolname FROM pg_roles WHERE oid = ANY(p.polroles))"
            " FROM pg_policy p WHERE p.polrelid = 'cloudops.size_band_policy'::regclass"
            " ORDER BY 1").fetchall()
        forced = cx.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE oid = 'cloudops.size_band_policy'::regclass").fetchone()
        customer = cx.execute(
            "SELECT has_table_privilege('cloudops_customer', 'cloudops.size_band_policy',"
            " 'SELECT')").fetchone()
    assert forced == (True, True)
    assert customer == (False,)
    assert found == [("size_band_read", True, "r", []),
                     ("size_band_write", True, "a", ["cloudops_access_admin"])]


@pytest.mark.isolation
async def test_setting_size_bands_needs_the_permission(sections: Sections) -> None:
    body = {"spend_currency": "EUR", "spend_medium_from": "5000", "spend_large_above": "20000",
            "resources_medium_from": 100, "resources_large_above": 200,
            "reason": "Tighter bands for the pilot"}
    for oid in (sections.engineer_a, sections.ceo_all, sections.cxo_a):
        response = await sections.api.post(SETTINGS, oid, body)
        assert response.status_code == 403, response.text
    await _get(sections, SETTINGS, sections.cxo_a, 403)
    view = await _get(sections, SETTINGS, sections.engineer_a)
    assert (view["current"]["version"]["value"], view["can_edit"]) == (1, False)


async def test_new_thresholds_are_a_new_version_and_move_the_bands(sections: Sections) -> None:
    bad = {"spend_currency": "EUR", "spend_medium_from": "5000", "spend_large_above": "100",
           "resources_medium_from": 100, "resources_large_above": 200, "reason": "Backwards"}
    assert (await sections.api.post(SETTINGS, sections.admin, bad)).status_code == 422
    as_float = {**bad, "spend_large_above": 20000.5}
    assert (await sections.api.post(SETTINGS, sections.admin, as_float)).status_code == 422
    body = {**bad, "spend_large_above": "20000", "resources_large_above": 240,
            "reason": "Tighter bands for the pilot"}
    response = await sections.api.post(SETTINGS, sections.admin, body)
    assert response.status_code == 201, response.text
    view = response.json()
    assert view["current"]["version"]["value"] == 2
    assert view["current"]["spend_large_above"]["value"] == "20000.000000"
    assert [h["version"]["value"] for h in view["history"]] == [2, 1]
    assert view["history"][0]["set_by_you"] is True
    assert view["history"][0]["reason"] == "Tighter bands for the pilot"
    platform = await _get(sections, PLATFORM, sections.admin, **_span(sections.month))
    sizes = {s["tenant"]["name"]: s for s in platform["sizes"]}
    assert sizes["bravo"]["band"] == "large"  # 251 resources, now above 240
    assert platform["policy"]["version"]["value"] == 2
    with psycopg.connect(sections.seeded.admin_dsn) as cx:
        logged = cx.execute(
            "SELECT count(*) FROM audit.action_log WHERE actor = %s AND action LIKE %s",
            (f"user:{sections.admin}", "%size-bands%")).fetchone()
    assert logged is not None
    assert logged[0] >= 1
