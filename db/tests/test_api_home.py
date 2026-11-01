"""Home dashboard summaries (ADR 0030): /v1/home/operations, /triage, /cost, /ai-usage.

Against their own database, seeded like ``seeded`` and then given tickets of
every SLA shape, unreviewed triage, cost in two currencies across months,
reconciliation in each state and gateway calls, so no other suite's counts move.

Each endpoint is checked for the figures it returns, for what another tenant's
rows do to them (nothing, unless the caller's scope includes that tenant), and
for refusals: a role without the permission, and a customer, which never reads
triage, Getronics' cost or AI spend, and never sees a workgroup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from cloudops_tenancy import Database

from .conftest import Api, Seeded, _access, _database, _seeded, onboard_portal


@dataclass(frozen=True)
class Home:
    seeded: Seeded
    api: Api
    admin: UUID  # platform administrator, all tenants
    engineer_a: UUID  # Engineer, tenant a: tickets and triage, no cost
    finops_all: UUID  # FinOps, all tenants: cost, no tickets
    cxo_a: UUID  # customer CXO of a: tickets and cost (its price), never staff views
    security_a: UUID  # customer Security officer of a: no tickets
    internal: UUID  # the internal tenant: Getronics' own cost (ADR 0023)
    previous: str  # last month, YYYY-MM
    current: str


def _month(d: datetime) -> str:
    return f"{d:%Y-%m}"


def _incident(cx: psycopg.Connection[Any], tenant: UUID, number: str, priority: str | None,
              opened: datetime, *, breached: bool | None = None, closed: datetime | None = None,
              resolve_seconds: int | None = None, ci: str | None = None,
              workgroup: str | None = None, kind: str = "incident") -> None:
    cx.execute(
        "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
        " closed_at, priority, sla_breached, resolve_time_seconds, configuration_item,"
        " assignment_workgroup, assigned_to, short_description)"
        " VALUES (%s, %s, %s, 'HOME', %s, %s, %s, %s, %s, %s, %s, 'A. Getronics', %s)",
        (tenant, kind, number, opened, closed, priority, breached, resolve_seconds, ci,
         workgroup, f"{number} summary"),
    )


def _populate(s: Seeded, internal: UUID, internal_account: UUID) -> None:
    now = datetime.now(UTC)
    a, b, c = s.a, s.b, s.c
    previous = (now.replace(day=1) - timedelta(days=1))
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        # Tenant a: one incident per SLA shape. The base seed adds INC-alpha (no priority).
        _incident(cx, a.id, "INC-A-P1", "1 - Critical", now - timedelta(hours=3, minutes=30),
                  ci="sql-a-01", workgroup="GTN Azure L2")  # 3.5h of 4h: at risk
        _incident(cx, a.id, "INC-A-P2", "2 - High", now - timedelta(hours=1), breached=True,
                  ci="sql-a-01", workgroup="GTN Azure L2")  # ServiceNow flagged it
        _incident(cx, a.id, "INC-A-P3", "3 - Moderate", now - timedelta(hours=2),
                  ci="sql-a-01", workgroup="GTN Network")  # 2h of 72h: within
        _incident(cx, a.id, "INC-A-P5", "5 - Planning", now - timedelta(days=10),
                  workgroup="GTN Network")  # no target
        _incident(cx, a.id, "INC-A-DONE", "3 - Moderate", now - timedelta(days=2),
                  closed=now - timedelta(days=1), resolve_seconds=7200, ci="web-a-01",
                  workgroup="GTN Azure L2")
        _incident(cx, a.id, "CTASK-A-OPEN", None, now - timedelta(days=2), kind="change_task",
                  ci="sql-a-01", workgroup="GTN Change")
        _incident(cx, a.id, "CTASK-A-DONE", None, now - timedelta(days=3),
                  closed=now - timedelta(days=1), kind="change_task", workgroup="GTN Change")
        # Tenant b: a P1 past its target that ServiceNow has not flagged.
        _incident(cx, b.id, "INC-B-P1", "1", now - timedelta(hours=5), ci="sql-b-01",
                  workgroup="GTN Azure L2")
        # Triage awaiting review: a's least confident, b's ready for planning.
        for tenant, number, route, confidence in ((a.id, "INC-A-P1", "human", 0.55),
                                                  (b.id, "INC-B-P1", "ready_for_planning", 0.91)):
            cx.execute(
                "INSERT INTO cloudops.triage_result (tenant_id, number, route, stage,"
                " classification, cost_currency, workflow_id)"
                " VALUES (%s, %s, %s, 3, %s, 'USD', %s)",
                (tenant, number, route,
                 psycopg.types.json.Jsonb({"intent": "service_down", "confidence": confidence}),
                 f"home-{number}"),
            )
        # Cost: a in EUR (two months) and USD (this month), never summed together.
        cx.execute(
            "INSERT INTO cloudops.cost_fact (tenant_id, account_id, resource_id, usage_date,"
            " invoice_period, service_native, unblended_cost, amortized_cost, currency) VALUES"
            " (%s, %s, %s, %s, %s, 'Virtual Machines', 1000.500000, 990.250000, 'EUR'),"
            " (%s, %s, %s, %s, %s, 'Storage', 12.000000, 11.125000, 'USD')",
            (a.id, a.account_id, a.resource_id, previous.date(), _month(previous),
             a.id, a.account_id, a.resource_id, now.date(), _month(now)),
        )
        # b and c have cost last month too: b unchecked, c checked and failed.
        for t in (b, c):
            cx.execute(
                "INSERT INTO cloudops.cost_fact (tenant_id, account_id, resource_id, usage_date,"
                " invoice_period, service_native, unblended_cost, amortized_cost, currency)"
                " VALUES (%s, %s, %s, %s, %s, 'Virtual Machines', 50, 50, %s)",
                (t.id, t.account_id, t.resource_id, previous.date(), _month(previous),
                 t.currency),
            )
        for tenant, account, kind, status, reason in (
            (a.id, a.account_id, "invoice", "verified", None),
            (c.id, c.account_id, "provider_total", "unverified", "drift"),
        ):
            cx.execute(
                "INSERT INTO cloudops.cost_reconciliation (tenant_id, account_id, invoice_period,"
                " kind, checked_on, ledger_amount, ledger_currency, reference_amount,"
                " reference_currency, drift, status, reason)"
                " VALUES (%s, %s, %s, %s, now()::date, 1, 'EUR', 1, 'EUR', 0, %s, %s)",
                (tenant, account, _month(previous), kind, status, reason),
            )
        # The host's subscription: a subscription-level line, one platform resource
        # and one lab VM (ADR 0031: the platform's cost is its own resources' only).
        cx.execute(
            "INSERT INTO cloudops.cost_fact (tenant_id, account_id, usage_date,"
            " invoice_period, service_native, unblended_cost, amortized_cost, currency)"
            " VALUES (%s, %s, %s, %s, 'Kubernetes', 420.000000, 410.500000, 'EUR')",
            (internal, internal_account, now.date(), _month(now)),
        )
        for name, group, amount in (("aks-cloudops-dev-weu", "MC_rg-cloudops-aks-dev-weu_x",
                                     "100.250000"),
                                    ("vm-lab-01", "rg-lab", "50.000000")):
            (resource,) = cx.execute(
                "INSERT INTO cloudops.resource (tenant_id, account_id, native_id, name,"
                " type_native, type_canonical) VALUES (%s, %s, %s, %s, 'x', 'x') RETURNING id",
                (internal, internal_account,
                 f"/subscriptions/s/resourceGroups/{group}/providers/x/{name}", name),
            ).fetchone() or (None,)
            cx.execute(
                "INSERT INTO cloudops.cost_fact (tenant_id, account_id, resource_id, usage_date,"
                " invoice_period, service_native, unblended_cost, amortized_cost, currency)"
                " VALUES (%s, %s, %s, %s, %s, 'Compute', %s, %s, 'EUR')",
                (internal, internal_account, resource, now.date(), _month(now), amount, amount),
            )
        # Gateway calls this month.
        for tenant, tokens_in, tokens_out, cost, outcome in (
            (a.id, 1000, 500, "0.012500", "succeeded"),
            (a.id, 200, 0, "0.001000", "succeeded"),
            (b.id, 0, 0, "0", "refused"),
        ):
            cx.execute(
                "INSERT INTO cloudops.llm_call (tenant_id, principal, task, tier_requested,"
                " tokens_in, tokens_out, cost, cost_currency, outcome)"
                " VALUES (%s, 'user:home', 'classify_ticket', 'small', %s, %s, %s, 'USD', %s)",
                (tenant, tokens_in, tokens_out, Decimal(cost), outcome),
            )


@pytest.fixture(scope="module")
async def home(admin_server: str) -> AsyncIterator[Home]:
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from cloudops_api.app import create_app
    from cloudops_api.auth import StaticKeys, TokenValidator
    from cloudops_config import AuthConfig
    from cloudops_devdata.tokens import new_signing_key
    from cloudops_domain.assurance.pack import Ed25519Signer
    from cloudops_tenancy import TenantScope

    with _database(admin_server, "home") as provisioned:
        seeded = _seeded(provisioned)
        internal, internal_account = uuid4(), uuid4()
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code, internal,"
                       " hosts_platform) VALUES (%s, 'Getronics platform', 'GTNPLAT', true,"
                       " true)", (internal,))
            cx.execute("INSERT INTO cloudops.cloud_account (id, tenant_id, provider, native_id,"
                       " display_name) VALUES (%s, %s, 'azure', %s, 'platform')",
                       (internal_account, internal, f"sub-platform-{provisioned.suffix}"))
        _populate(seeded, internal, internal_account)
        admin, engineer_a, finops_all, cxo_a, security_a = (uuid4() for _ in range(5))
        db = Database.create(seeded.app_dsn, pool_size=4, max_overflow=0)
        a = seeded.a.id
        async with db.transaction(TenantScope.for_resolution("setup:home")) as conn:
            await _access(conn, "SELECT access.bootstrap_admin(:o, 'Admin')", o=admin)
            for oid, role, tenant, everyone in (
                (engineer_a, "Engineer", a, False),
                (finops_all, "FinOps", None, True),
            ):
                await _access(conn, "SELECT access.set_principal(:a, :o, 'staff', :n, :r, :all)",
                              a=admin, o=oid, n=f"{role} {oid}", r=role, all=everyone)
                if tenant is not None:
                    await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)",
                                  a=admin, o=oid, t=tenant)
            await onboard_portal(conn, admin, a, cxo_a)
            await _access(conn, "SELECT access.set_principal(:a, :o, 'customer', 'Security',"
                          " 'Security officer', false)", a=admin, o=security_a)
            await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)",
                          a=admin, o=security_a, t=a)
        auth = AuthConfig(
            issuer="https://login.example.test/66666666-6666-4666-8666-666666666666/v2.0",
            audience="api://cloudops-home", tenant_id="66666666-6666-4666-8666-666666666666",
            required_scope="access_as_user", jwks_url=None, jwks_secret=None,
        )
        private, public = new_signing_key("home")
        signer = Ed25519Signer(Ed25519PrivateKey.generate())
        app = create_app(db=db, validator=TokenValidator(auth, StaticKeys(public)), signer=signer)
        now = datetime.now(UTC)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://api") as client:
                yield Home(seeded, Api(client, private, auth, signer), admin, engineer_a,
                           finops_all, cxo_a, security_a, internal,
                           _month(now.replace(day=1) - timedelta(days=1)), _month(now))
        finally:
            await db.dispose()


async def _get(home: Home, path: str, oid: UUID, status: int = 200) -> Any:
    response = await home.api.get(path, oid)
    assert response.status_code == status, (path, response.text)
    return response.json()


def _tenants(body: Any) -> set[str]:
    """Every tenant id anywhere in a response."""
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


def _strip(v: Any) -> Any:
    """Without the times that move between two calls."""
    if isinstance(v, dict):
        moving = {"as_of", "query_ref", "elapsed_hours"}
        return {k: _strip(x) for k, x in v.items() if k not in moving}
    if isinstance(v, list):
        return [_strip(x) for x in v]
    return v


# --- operations ----------------------------------------------------------------------


async def test_operations_bands_sla_risk_by_priority(home: Home) -> None:
    body = await _get(home, "/v1/home/operations", home.engineer_a)
    assert _tenants(body) == {str(home.seeded.a.id)}
    # INC-alpha (no priority, from the base seed) and the four open ones above.
    assert body["open_incidents"]["value"] == 5
    assert body["breached_open"]["value"] == 1
    assert body["at_risk"] == {**body["at_risk"], "value": 1, "is_modelled": True,
                               "source_system": "cloudops_policy"}
    lines = {p["priority"]: p for p in body["by_priority"]}
    assert list(lines) == ["1", "2", "3", "4", "5", "none"]
    assert (lines["1"]["open"]["value"], lines["1"]["at_risk"]["value"]) == (1, 1)
    assert lines["1"]["target_hours"]["value"] == 4
    assert lines["1"]["target_hours"]["is_modelled"] is True
    assert lines["2"]["breached"]["value"] == 1
    assert lines["2"]["at_risk"]["value"] == 0  # breached is not also at risk
    assert lines["3"]["at_risk"]["value"] == 0
    assert lines["4"]["open"]["value"] == 0  # a priority with nothing open is still listed
    # No target: never at risk, and the response says there is none rather than zero.
    assert lines["5"]["at_risk"] is None
    assert lines["5"]["target_hours"] is None
    assert lines["none"]["open"]["value"] == 1
    closest = [(i["number"], i["risk"]) for i in body["closest"]]
    assert closest[:3] == [("INC-A-P2", "breached"), ("INC-A-P1", "at_risk"),
                           ("INC-A-P3", "within")]
    assert ("INC-A-P5", "no_target") in closest


async def test_operations_changes_noise_and_backlog(home: Home) -> None:
    body = await _get(home, "/v1/home/operations", home.engineer_a)
    changes = body["changes"]
    assert (changes["opened"]["value"], changes["closed"]["value"],
            changes["open_now"]["value"]) == (2, 1, 1)
    assert {c["number"] for c in changes["recent"]} == {"CTASK-A-OPEN", "CTASK-A-DONE"}
    noisy = body["noisy_cis"][0]
    assert (noisy["configuration_item"], noisy["incidents"]["value"]) == ("sql-a-01", 3)
    [customer] = body["by_customer"]
    assert customer["open_items"]["value"] == 6  # five incidents and one change
    assert customer["resolved_30d"]["value"] == 1
    assert Decimal(customer["mttr_hours"]["value"]) == Decimal(2)
    groups = {g["workgroup"]: g for g in body["by_workgroup"]}
    assert groups["GTN Azure L2"]["open_items"]["value"] == 2
    assert groups["GTN Azure L2"]["breached_open"]["value"] == 1
    assert Decimal(groups["GTN Azure L2"]["mttr_hours"]["value"]) == Decimal(2)
    assert groups["GTN Network"]["mttr_hours"] is None  # nothing resolved: absent, not zero


async def test_operations_follows_scope_across_tenants(home: Home) -> None:
    mine = await _get(home, "/v1/home/operations", home.engineer_a)
    everyone = await _get(home, "/v1/home/operations", home.admin)
    assert str(home.seeded.b.id) in _tenants(everyone)
    # b's P1 is five hours into a four-hour target and unflagged: at risk, but only
    # for a scope that includes b.
    assert everyone["at_risk"]["value"] == mine["at_risk"]["value"] + 1
    assert "INC-B-P1" not in {i["number"] for i in mine["closest"]}


async def test_a_customer_sees_its_own_operations_without_workgroups(home: Home) -> None:
    body = await _get(home, "/v1/home/operations", home.cxo_a)
    assert _tenants(body) == {str(home.seeded.a.id)}
    assert body["by_workgroup"] is None
    assert "GTN" not in str(body)  # no workgroup name anywhere
    staff = await _get(home, "/v1/home/operations", home.engineer_a)
    assert _strip({**body, "by_workgroup": None}) == _strip({**staff, "by_workgroup": None})


async def test_operations_needs_tickets_read(home: Home) -> None:
    await _get(home, "/v1/home/operations", home.security_a, 403)
    await _get(home, "/v1/home/operations", home.finops_all, 403)


# --- triage --------------------------------------------------------------------------


async def test_triage_queue_is_unreviewed_open_tickets_least_confident_first(home: Home) -> None:
    body = await _get(home, "/v1/home/triage", home.engineer_a)
    # The base seed's triage is reviewed; only INC-A-P1's is waiting.
    assert body["awaiting"]["value"] == 1
    assert body["low_confidence"]["value"] == 1
    [item] = body["items"]
    assert (item["number"], item["route"], item["below_threshold"]) == ("INC-A-P1", "human", True)
    assert Decimal(item["confidence"]["value"]) == Decimal("0.55")
    assert Decimal(body["threshold"]["value"]) == Decimal("0.7")
    everyone = await _get(home, "/v1/home/triage", home.admin)
    assert [i["number"] for i in everyone["items"]] == ["INC-A-P1", "INC-B-P1"]
    assert everyone["needs_person"]["value"] == 1


async def test_triage_is_staff_with_the_permission_only(home: Home) -> None:
    await _get(home, "/v1/home/triage", home.cxo_a, 403)
    await _get(home, "/v1/home/triage", home.finops_all, 403)


# --- cost ----------------------------------------------------------------------------


async def test_cost_trend_keeps_currencies_apart_and_marks_reconciliation(home: Home) -> None:
    body = await _get(home, "/v1/home/cost", home.finops_all)
    assert len(body["months"]) == 6
    assert body["months"][-1] == home.current
    by = {c["tenant"]["id"]: c for c in body["customers"]}
    a = by[str(home.seeded.a.id)]
    trend = {t["currency"]: {p["period"]: p for p in t["points"]} for t in a["currencies"]}
    assert set(trend) == {"EUR", "USD"}
    # Unrounded decimal strings in the transaction currency, never converted or summed.
    assert trend["EUR"][home.previous]["amortized"]["value"] == "990.250000"
    assert trend["EUR"][home.current]["amortized"]["value"] == "118.000000"
    assert trend["EUR"][home.current]["month_to_date"] is True
    assert trend["USD"][home.current]["amortized"]["value"] == "11.125000"
    assert trend["USD"][home.current]["amortized"]["unit"] == "USD"
    assert trend["USD"][home.previous]["amortized"] is None  # no rows: absent, not zero
    states = {(c["tenant"]["id"], r["period"]): r["state"]
              for c in body["customers"] for r in c["reconciliation"]}
    a_id, b_id, c_id = (str(t.id) for t in (home.seeded.a, home.seeded.b, home.seeded.c))
    assert states[(a_id, home.previous)] == "final"
    assert states[(a_id, home.current)] == "provisional"
    assert states[(b_id, home.previous)] == "unchecked"
    assert states[(c_id, home.previous)] == "unverified"
    assert a["anomalies_30d"]["value"] == 1
    # An internal tenant is reported like a customer: its whole subscription.
    internal = by[str(home.internal)]
    assert (internal["internal"], internal["hosts_platform"]) == (True, True)
    assert (a["internal"], a["hosts_platform"]) == (False, False)
    assert internal["currencies"][0]["points"][-1]["amortized"]["value"] == "560.750000"
    # The platform's own cost: its resources' lines in the host, nothing else.
    platform = body["platform"]
    assert platform["host"]["id"] == str(home.internal)
    assert platform["captured"] is True
    own = platform["currencies"][0]
    assert own["currency"] == "EUR"
    assert own["points"][-1]["amortized"]["value"] == "100.250000"
    assert own["points"][-2]["amortized"] is None


async def test_cost_is_staff_cost_read_only(home: Home) -> None:
    # The CXO holds cost.read, but reads its price (ADR 0021), never Getronics' cost.
    await _get(home, "/v1/home/cost", home.cxo_a, 403)
    await _get(home, "/v1/home/cost", home.engineer_a, 403)
    # Nor margin, which is commercial: the engineer and the customer are refused it.
    await _get(home, "/v1/commercial/margin", home.engineer_a, 403)
    await _get(home, "/v1/commercial/margin", home.cxo_a, 403)


# --- AI gateway usage ----------------------------------------------------------------


async def test_ai_usage_against_quota(home: Home) -> None:
    body = await _get(home, "/v1/home/ai-usage", home.admin)
    by = {t["tenant"]["id"]: t for t in body["tenants"]}
    a = by[str(home.seeded.a.id)]
    assert a["tokens"]["value"] == 1700
    assert a["quota"]["value"] == 5_000_000
    assert Decimal(a["used_share"]["value"]) == Decimal(1700) / Decimal(5_000_000)
    assert [(s["currency"], s["spend"]["value"]) for s in a["spend"]] == [("USD", "0.013500")]
    b = by[str(home.seeded.b.id)]
    assert (b["calls"]["value"], b["refused"]["value"]) == (1, 1)
    assert by[str(home.seeded.c.id)]["tokens"]["value"] == 0


async def test_ai_usage_is_staff_cost_read_only(home: Home) -> None:
    await _get(home, "/v1/home/ai-usage", home.cxo_a, 403)
    await _get(home, "/v1/home/ai-usage", home.engineer_a, 403)
    mine = await _get(home, "/v1/home/ai-usage", home.finops_all)
    assert str(home.seeded.a.id) in _tenants(mine)


async def test_me_names_the_role_class(home: Home) -> None:
    assert (await _get(home, "/v1/me", home.cxo_a))["role_class"] == "customer"
    assert (await _get(home, "/v1/me", home.engineer_a))["role_class"] == "engineer"
    assert (await _get(home, "/v1/me", home.admin))["role_class"] == "platform_admin"
