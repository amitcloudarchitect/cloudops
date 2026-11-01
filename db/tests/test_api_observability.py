"""Platform observability (/v1/observability/*, migration 0032) against the real schema.

Its own database, seeded like ``seeded`` (each tenant: one triage routed to a person
and accepted, one action-log row by a non-user actor) and then given gateway calls
for every agent, triage in both routes, reviews of both kinds, action-log requests
by known principals, a connector, and the platform host's cost.

Checked: who reaches it (``platform.observe``, staff only; a customer is refused,
and no customer role can even be granted it), the figures, and narrowing: one
tenant of the caller's scope gives exactly that tenant's figures, and a tenant
outside it is not found.
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
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_tenancy import Database, TenantScope

from .conftest import Api, Seeded, _access, _database, _seeded, onboard_portal

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

SECTIONS = ("usage", "ai", "triage", "connectors", "platform-cost")


@dataclass(frozen=True)
class Obs:
    seeded: Seeded
    api: Api
    admin: UUID  # platform administrator, all tenants
    observer_a: UUID  # staff role holding platform.observe only, tenant a
    engineer_a: UUID  # Engineer: no platform.observe
    cxo_a: UUID  # customer
    internal: UUID  # hosts the platform
    source: str  # this suite's connector


def _populate(s: Seeded, internal: UUID, account: UUID, admin: UUID, observer: UUID,
              cxo: UUID, source: str) -> None:
    now = datetime.now(UTC)
    a, b = s.a.id, s.b.id
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        for tenant, task, tin, tout, cost, outcome in (
            (a, "classify_ticket", 1000, 500, "0.010000", "succeeded"),
            (a, "cache:enrich_ticket", 100, 0, "0.001000", "succeeded"),
            (a, "assistant.answer", 300, 200, "0.020000", "succeeded"),
            (a, "draft_report_narrative", 0, 0, "0", "failed"),
            (b, "classify_ticket", 40, 10, "0.000500", "refused"),
            (b, "index", 60, 0, "0.000100", "succeeded"),
        ):
            cx.execute(
                "INSERT INTO cloudops.llm_call (tenant_id, principal, task, tier_requested,"
                " tokens_in, tokens_out, cost, cost_currency, outcome)"
                " VALUES (%s, 'user:obs', %s, 'small', %s, %s, %s, 'USD', %s)",
                (tenant, task, tin, tout, Decimal(cost), outcome),
            )
        # b: one ready for planning, overridden.
        (ready,) = cx.execute(
            "INSERT INTO cloudops.triage_result (tenant_id, number, route, stage,"
            " cost_currency, workflow_id) VALUES (%s, 'INC-OBS-B', 'ready_for_planning', 3,"
            " 'USD', 'obs-b') RETURNING id", (b,)).fetchone() or (None,)
        cx.execute(
            "INSERT INTO cloudops.triage_review (tenant_id, triage_result_id, decision,"
            " overrides, reason, reviewer_oid) VALUES (%s, %s, 'override', '{\"intent\": \"x\"}',"
            " 'wrong intent', gen_random_uuid())", (b, ready))
        # Requests: the observer and the CXO in a; the administrator's one request
        # in a scope of three tenants is logged three times, at one time.
        at = now - timedelta(hours=1)
        for tenant, oid, action in ((a, observer, "GET /v1/me"), (a, observer, "GET /v1/x"),
                                    (a, cxo, "GET /v1/me")):
            cx.execute("INSERT INTO audit.action_log (tenant_id, actor, action, outcome,"
                       " created_at) VALUES (%s, %s, %s, 'succeeded', %s)",
                       (tenant, f"user:{oid}", action, at))
        for tenant in (a, b, s.c.id):
            cx.execute("INSERT INTO audit.action_log (tenant_id, actor, action, outcome,"
                       " created_at) VALUES (%s, %s, 'GET /v1/obs-dedupe', 'succeeded', %s)",
                       (tenant, f"user:{admin}", at))
        # A connector of this suite's own: two runs today, one failed.
        cx.execute("INSERT INTO staging.connector_schedule (source_system, entity, every)"
                   " VALUES (%s, 'thing', interval '1 hour')", (source,))
        for status, ago in (("succeeded", "10 minutes"), ("failed", "40 minutes")):
            cx.execute(
                "INSERT INTO staging.ingest_run (id, source_system, entity, mode, started_at,"
                " finished_at, status, records, written, rejected, quarantined, lag_seconds)"
                " VALUES (%s, %s, 'thing', 'incremental', now() - %s::interval,"
                " now() - %s::interval + interval '5 seconds', %s, 5, 5, 0, 0, 1)",
                (uuid4(), source, ago, ago, status))
        # The host: a platform resource's line and a lab VM's.
        for name, group, amount in (("aks-cloudops-dev-weu", "rg-cloudops-aks-dev-weu",
                                     "75.500000"), ("vm-lab", "rg-lab", "9.000000")):
            (resource,) = cx.execute(
                "INSERT INTO cloudops.resource (tenant_id, account_id, native_id, name,"
                " type_native, type_canonical) VALUES (%s, %s, %s, %s, 'x', 'x') RETURNING id",
                (internal, account, f"/subscriptions/s/resourceGroups/{group}/providers/x/{name}",
                 name)).fetchone() or (None,)
            cx.execute(
                "INSERT INTO cloudops.cost_fact (tenant_id, account_id, resource_id, usage_date,"
                " invoice_period, service_native, unblended_cost, amortized_cost, currency)"
                " VALUES (%s, %s, %s, %s, %s, 'Compute', %s, %s, 'EUR')",
                (internal, account, resource, now.date(), f"{now:%Y-%m}", amount, amount))


@pytest.fixture(scope="module")
async def obs(admin_server: str) -> AsyncIterator[Obs]:
    import httpx

    from cloudops_api.app import create_app
    from cloudops_api.auth import StaticKeys, TokenValidator
    from cloudops_config import AuthConfig
    from cloudops_devdata.tokens import new_signing_key

    with _database(admin_server, "obs") as provisioned:
        seeded = _seeded(provisioned)
        internal, account = uuid4(), uuid4()
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code, internal,"
                       " hosts_platform) VALUES (%s, 'Getronics platform', 'GTNPLAT', true,"
                       " true)", (internal,))
            cx.execute("INSERT INTO cloudops.cloud_account (id, tenant_id, provider, native_id,"
                       " display_name) VALUES (%s, %s, 'azure', %s, 'platform')",
                       (account, internal, f"sub-platform-{provisioned.suffix}"))
        admin, observer_a, engineer_a, cxo_a = (uuid4() for _ in range(4))
        source = f"t_{uuid4().hex[:10]}"
        _populate(seeded, internal, account, admin, observer_a, cxo_a, source)
        db = Database.create(seeded.app_dsn, pool_size=4, max_overflow=0)
        a = seeded.a.id
        async with db.transaction(TenantScope.for_resolution("setup:obs")) as conn:
            await _access(conn, "SELECT access.bootstrap_admin(:o, 'Admin')", o=admin)
            await _access(conn, "SELECT access.define_role(:a, 'Observer', 'engineer', 'obs')",
                          a=admin)
            await _access(conn, "SELECT access.grant_permission(:a, 'Observer',"
                          " 'platform.observe')", a=admin)
            for oid, role in ((observer_a, "Observer"), (engineer_a, "Engineer")):
                await _access(conn, "SELECT access.set_principal(:a, :o, 'staff', :n, :r, false)",
                              a=admin, o=oid, n=f"{role} {oid}", r=role)
                await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)",
                              a=admin, o=oid, t=a)
            await onboard_portal(conn, admin, a, cxo_a)
        auth = AuthConfig(
            issuer="https://login.example.test/77777777-7777-4777-8777-777777777777/v2.0",
            audience="api://cloudops-obs", tenant_id="77777777-7777-4777-8777-777777777777",
            required_scope="access_as_user", jwks_url=None, jwks_secret=None,
        )
        private, public = new_signing_key("obs")
        app = create_app(db=db, validator=TokenValidator(auth, StaticKeys(public)))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://api") as client:
                yield Obs(seeded, Api(client, private, auth), admin, observer_a, engineer_a,
                          cxo_a, internal, source)
        finally:
            await db.dispose()


async def _get(obs: Obs, section: str, oid: UUID, status: int = 200,
               **params: str) -> Any:
    response = await obs.api.get(f"/v1/observability/{section}", oid, params=params or None)
    assert response.status_code == status, (section, response.text)
    return response.json()


# --- who reaches it ------------------------------------------------------------------


@pytest.mark.parametrize("section", SECTIONS)
async def test_roles_without_the_permission_and_customers_are_refused(
    obs: Obs, section: str,
) -> None:
    for oid in (obs.engineer_a, obs.cxo_a):
        body = await _get(obs, section, oid, 403)
        assert body["error"]["code"] == "forbidden"


async def test_no_customer_role_can_be_granted_it(obs: Obs) -> None:
    db = Database.create(obs.seeded.app_dsn, pool_size=1, max_overflow=0)

    async def grant() -> None:
        async with db.transaction(TenantScope.for_resolution("setup:obs-probe")) as conn:
            await _access(conn, "SELECT access.define_role(:a, 'Probe', 'customer', 'p')",
                          a=obs.admin)
            await _access(conn, "SELECT access.grant_permission(:a, 'Probe',"
                          " 'platform.observe')", a=obs.admin)

    try:
        with pytest.raises(DBAPIError, match="cannot be granted to a customer role"):
            await grant()
    finally:
        await db.dispose()


async def test_the_role_lookup_refuses_without_the_permission(obs: Obs) -> None:
    db = Database.create(obs.seeded.app_dsn, pool_size=1, max_overflow=0)
    try:
        for oid in (obs.engineer_a, obs.cxo_a):
            with pytest.raises(DBAPIError, match="not permitted to observe"):
                async with db.transaction(TenantScope.for_resolution("probe")) as conn:
                    await conn.execute(text("SELECT * FROM access.observed_roles(:a, :o)"),
                                       {"a": oid, "o": [obs.admin]})
    finally:
        await db.dispose()


async def test_a_tenant_outside_the_scope_is_not_found(obs: Obs) -> None:
    for section in ("usage", "ai", "triage", "platform-cost"):
        body = await _get(obs, section, obs.observer_a, 404, tenant_id=str(obs.seeded.b.id))
        assert body["error"]["code"] == "not_found"
        assert str(obs.seeded.b.id) not in str(body)
        await _get(obs, section, obs.observer_a, 404, tenant_id=str(uuid4()))


# --- usage ---------------------------------------------------------------------------


async def test_usage_counts_users_by_role_and_requests_once(obs: Obs) -> None:
    body = await _get(obs, "usage", obs.admin, days="7")
    assert len(body["daily"]) == 7
    assert body["daily"][-1]["day"] == body["window"]["until"]
    roles = {r["role"]: r["users"]["value"] for r in body["by_role"]}
    assert roles["Observer"] >= 1
    assert roles["CXO"] >= 1
    assert roles["Platform administrator"] >= 1
    assert body["customer_users"]["value"] == 1
    endpoints = {e["action"]: e for e in body["top_endpoints"]}
    # Logged once per tenant in the administrator's scope; one request all the same.
    assert endpoints["GET /v1/obs-dedupe"]["requests"]["value"] == 1
    assert body["sessions"]["is_modelled"] is True
    assert body["sessions"]["value"] >= 2
    assert body["active_users"]["source_system"] == "audit_log"
    assert body["weekly"][-1]["active_users"]["value"] >= 3


async def test_usage_narrows_to_one_tenant(obs: Obs) -> None:
    b = await _get(obs, "usage", obs.admin, tenant_id=str(obs.seeded.b.id))
    assert b["window"]["tenant"]["id"] == str(obs.seeded.b.id)
    roles = {r["role"] for r in b["by_role"]}
    # The CXO and the observer act in a only.
    assert "CXO" not in roles
    assert "Observer" not in roles
    a = await _get(obs, "usage", obs.observer_a)
    assert {"CXO", "Observer"} <= {r["role"] for r in a["by_role"]}
    assert "GET /v1/obs-dedupe" in {e["action"] for e in a["top_endpoints"]}


# --- AI ------------------------------------------------------------------------------


async def test_ai_by_agent_customer_and_sub_budget(obs: Obs) -> None:
    body = await _get(obs, "ai", obs.admin)
    assert body["spend_visible"] is True
    agents = {x["agent"]: x for x in body["by_agent"]}
    assert list(agents) == ["classifier", "enricher", "drafter", "assistant", "other"]
    assert agents["classifier"]["tokens"]["value"] == 1550
    assert (agents["classifier"]["calls"]["value"], agents["classifier"]["refused"]["value"]) \
        == (2, 1)
    assert agents["enricher"]["tokens"]["value"] == 100  # the cache's lookup counts
    assert agents["drafter"]["failed"]["value"] == 1
    assert agents["assistant"]["tokens"]["value"] == 500
    assert agents["other"]["tokens"]["value"] == 60
    assert [(s["currency"], s["spend"]["value"]) for s in agents["assistant"]["spend"]] \
        == [("USD", "0.020000")]
    customers = {c["tenant"]["id"]: c for c in body["by_customer"]}
    a = customers[str(obs.seeded.a.id)]
    assert a["month_tokens"]["value"] == 2100
    assert a["assistant_month_tokens"]["value"] == 500
    assert a["assistant_budget"]["value"] == 2_000_000
    assert a["assistant_budget"]["is_modelled"] is False
    assert Decimal(a["assistant_budget_used"]["value"]) == Decimal(500) / Decimal(2_000_000)
    assert customers[str(obs.seeded.c.id)]["calls"]["value"] == 0  # listed, at zero
    assert body["totals"]["tokens"]["value"] == 2210
    assert sum(d["tokens"]["value"] for d in body["daily"]) == 2210


async def test_ai_narrows_and_hides_spend_without_cost_read(obs: Obs) -> None:
    everyone = await _get(obs, "ai", obs.admin)
    narrowed = await _get(obs, "ai", obs.admin, tenant_id=str(obs.seeded.a.id))
    assert [c["tenant"]["id"] for c in narrowed["by_customer"]] == [str(obs.seeded.a.id)]
    [a] = [c for c in everyone["by_customer"] if c["tenant"]["id"] == str(obs.seeded.a.id)]
    assert narrowed["totals"]["tokens"]["value"] == a["tokens"]["value"] == 2100
    # The observer holds platform.observe but not cost.read: tokens yes, spend absent.
    mine = await _get(obs, "ai", obs.observer_a)
    assert mine["spend_visible"] is False
    assert mine["totals"]["spend"] is None
    assert all(x["spend"] is None for x in mine["by_agent"])
    assert mine["totals"]["tokens"]["value"] == 2100


# --- triage --------------------------------------------------------------------------


async def test_triage_throughput_route_split_and_reviews(obs: Obs) -> None:
    body = await _get(obs, "triage", obs.admin)
    by = {c["tenant"]["id"]: c for c in body["by_customer"]}
    b = by[str(obs.seeded.b.id)]
    # The base seed: one routed to a person and accepted, per tenant.
    assert (b["triaged"]["value"], b["ready_for_planning"]["value"], b["human"]["value"]) \
        == (2, 1, 1)
    assert (b["accepted"]["value"], b["overridden"]["value"]) == (1, 1)
    assert Decimal(b["override_rate"]["value"]) == Decimal("0.5")
    assert b["accepted"]["source_system"] == "triage_review"
    totals = body["totals"]
    assert totals["triaged"]["value"] == sum(c["triaged"]["value"] for c in by.values())
    narrowed = await _get(obs, "triage", obs.admin, tenant_id=str(obs.seeded.b.id))
    assert narrowed["totals"]["overridden"]["value"] == 1
    assert narrowed["totals"]["triaged"]["value"] == 2
    assert [c["tenant"]["id"] for c in narrowed["by_customer"]] == [str(obs.seeded.b.id)]


# --- connectors and platform cost ----------------------------------------------------


async def test_connectors_per_source(obs: Obs) -> None:
    body = await _get(obs, "connectors", obs.admin)
    assert body["visible"] is True
    [mine] = [s for s in body["sources"] if s["source_system"] == obs.source]
    assert (mine["runs_24h"]["value"], mine["failed_runs_24h"]["value"]) == (2, 1)
    assert Decimal(mine["failed_run_rate_24h"]["value"]) == Decimal("0.5")
    assert mine["band"] == "at-risk"  # half the runs failed (cloudops_api.connectors)
    # The observer lacks connectors.read: told so, not shown an empty list as healthy.
    observer = await _get(obs, "connectors", obs.observer_a)
    assert (observer["visible"], observer["sources"]) == (False, [])


async def test_platform_cost_is_the_hosts_platform_resources(obs: Obs) -> None:
    body = await _get(obs, "platform-cost", obs.admin)
    assert body["host"]["id"] == str(obs.internal)
    assert body["captured"] is True
    [eur] = body["currencies"]
    assert eur["points"][-1]["amortized"]["value"] == "75.500000"
    assert eur["points"][-1]["amortized"]["unit"] == "EUR"
    # Narrowed to a customer: the host is outside the scope, so there is none.
    narrowed = await _get(obs, "platform-cost", obs.admin, tenant_id=str(obs.seeded.a.id))
    assert narrowed["host"] is None
    observer = await _get(obs, "platform-cost", obs.observer_a)
    assert (observer["visible"], observer["host"]) == (False, None)
