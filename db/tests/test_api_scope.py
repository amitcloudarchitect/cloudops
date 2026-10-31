"""The API end to end against the real schema: token -> scope -> matrix -> RLS ->
provenance -> audit (step 04; ADR 0002).

Three cases the step requires by name, plus the decisions in ADR 0002:

- a tampered organisation claim is ignored;
- a customer principal cannot reach another tenant;
- a request with no valid scope fails closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from .conftest import Api, Principals, Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


def _ids(body: dict[str, Any]) -> set[str]:
    return {item["id"] for item in body["items"]}


# --- Required case: a tampered organisation claim is ignored -------------------------


async def test_tampered_organisation_claim_header_and_query_are_ignored(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    """Every place a client could try to name a tenant names tenant b. The
    customer of tenant a still sees exactly tenant a."""
    b = str(seeded.b.id)
    tampered_claims = {
        "tenant_id": b,
        "organisation": "bravo",
        "org_id": b,
        "extension_TenantId": b,
        "roles": ["platform_admin", "finops"],
        "groups": [b],
        "wids": ["62e90394-69f5-4237-9190-012177145e10"],  # Global Administrator
    }
    tampered_headers = {"X-Tenant-Id": b, "X-Organisation": "bravo", "X-Role": "platform_admin"}
    me = await api.get(
        "/v1/me", principals.customer_a,
        claims=tampered_claims, headers=tampered_headers, params={"tenant_id": b},
    )
    assert me.status_code == 200
    assert [t["id"] for t in me.json()["tenants"]] == [str(seeded.a.id)]
    assert me.json()["role"] == "Customer viewer"
    assert me.json()["kind"] == "customer"

    resources = await api.get(
        "/v1/resources", principals.customer_a,
        claims=tampered_claims, headers=tampered_headers, params={"tenant_id": b},
    )
    assert _ids(resources.json()) == {str(seeded.a.resource_id)}

    sows = await api.get("/v1/commercial/sows", principals.customer_a, claims=tampered_claims)
    assert sows.status_code == 403, "a roles claim cannot grant the commercial permission"


# --- Required case: a customer principal cannot reach another tenant -----------------


async def test_customer_cannot_reach_another_tenants_resource(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    foreign = await api.get(f"/v1/resources/{seeded.b.resource_id}", principals.customer_a)
    missing = await api.get(f"/v1/resources/{uuid4()}", principals.customer_a)
    own = await api.get(f"/v1/resources/{seeded.a.resource_id}", principals.customer_a)
    # 404, not 403, and indistinguishable from a resource that never existed.
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"
    assert own.status_code == 200
    assert own.json()["id"] == str(seeded.a.resource_id)


async def test_customer_lists_and_costs_contain_only_their_tenant(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    resources = await api.get("/v1/resources", principals.customer_a)
    assert _ids(resources.json()) == {str(seeded.a.resource_id)}
    # Closed months only (ADR 0021): which lines exist depends on other suites'
    # rows; whatever exists is this tenant's.
    cost = await api.get("/v1/cost/summary", principals.customer_a, params={"months": "12"})
    assert cost.status_code == 200
    assert {line["currency"] for line in cost.json()["lines"]} <= {seeded.a.currency}


async def test_customer_sees_the_customer_view_and_staff_the_staff_view(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    path = f"/v1/resources/{seeded.a.resource_id}"
    customer = (await api.get(path, principals.customer_a)).json()
    staff = (await api.get(path, principals.engineer_a)).json()
    assert set(customer) == {"id", "name", "type", "region", "state"}
    assert {"native_id", "tenant_id", "account_id", "tags"} <= set(staff)


# --- Required case: a request with no valid scope fails closed ------------------------


@pytest.mark.parametrize(
    ("who", "status", "code"),
    [
        ("unknown", 403, "no_scope"),
        ("staff_no_tenants", 403, "no_scope"),
        ("disabled", 403, "no_scope"),
        ("customer_two_tenants", 500, "scope_defect"),
        ("customer_no_tenant", 500, "scope_defect"),
    ],
)
async def test_no_valid_scope_fails_closed(
    api: Api, principals: Principals, who: str, status: int, code: str
) -> None:
    oid = uuid4() if who == "unknown" else getattr(principals, who)
    for path in ("/v1/me", "/v1/resources", "/v1/cost/summary"):
        response = await api.get(path, oid)
        assert response.status_code == status, path
        assert response.json() == {
            "error": {"code": code, "request_id": response.headers["x-request-id"]}
        }


@pytest.mark.parametrize(
    "header",
    [None, "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "bearer not.a.token", "Token abc"],
)
async def test_requests_without_a_valid_token_are_401(api: Api, header: str | None) -> None:
    headers = {} if header is None else {"Authorization": header}
    response = await api.client.get("/v1/resources", headers=headers)
    assert response.status_code == 401
    assert "items" not in response.json()


async def test_a_token_from_another_directory_is_401_even_for_a_known_oid(
    api: Api, principals: Principals
) -> None:
    response = await api.get(
        "/v1/me", principals.finops_all, claims={"tid": "55555555-5555-4555-8555-555555555555"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "wrong_directory"


# --- ADR 0002 ---------------------------------------------------------------------------


async def test_revoking_an_assignment_takes_effect_on_the_next_request(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    """D4: scope is resolved on every request, so revocation is immediate."""
    from sqlalchemy import text

    from cloudops_tenancy import Database, TenantScope

    assert (await api.get("/v1/resources", principals.revocable)).status_code == 200
    db = Database.create(seeded.app_dsn, pool_size=1)
    try:
        async with db.transaction(TenantScope.for_resolution("test:revoke")) as conn:
            await conn.execute(
                text("SELECT access.revoke_tenant(:a, :o, :t)"),
                {"a": principals.admin, "o": principals.revocable, "t": seeded.a.id},
            )
    finally:
        await db.dispose()
    after = await api.get("/v1/resources", principals.revocable)
    assert after.status_code == 403
    assert after.json()["error"]["code"] == "no_scope"


async def test_the_matrix_refuses_what_a_role_lacks(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    blocked = await api.get("/v1/resources", principals.tickets_only)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "forbidden"
    for who in (principals.engineer_a, principals.customer_a):
        assert (await api.get("/v1/commercial/sows", who)).status_code == 403
    finops = await api.get("/v1/commercial/sows", principals.finops_all)
    assert finops.status_code == 200
    assert {s["currency"] for s in finops.json()} >= {"EUR", "GBP", "USD"}


async def test_all_tenant_staff_see_a_tenant_onboarded_after_assignment(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    """D9: "all tenants" includes customers onboarded later, automatically."""
    late = uuid4()
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO cloudops.tenant (id, canonical_name, short_code) VALUES (%s, %s, %s)",
            (late, f"Late Onboard {late.hex[:6]}", f"LATE{late.hex[:6]}"),
        )
    me = await api.get("/v1/me", principals.finops_all)
    assert str(late) in {t["id"] for t in me.json()["tenants"]}
    engineer = await api.get("/v1/me", principals.engineer_a)
    assert str(late) not in {t["id"] for t in engineer.json()["tenants"]}


async def test_a_subscription_limited_customer_sees_only_that_subscription(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    """D16: enforced by RLS on account_scope, not by filtering in the API."""
    me = await api.get("/v1/me", principals.customer_c_limited)
    assert me.json()["subscription_limited"] is True
    resources = await api.get("/v1/resources", principals.customer_c_limited)
    assert _ids(resources.json()) == {str(seeded.c.resource_id)}
    hidden = await api.get(f"/v1/resources/{seeded.c_resource2}", principals.customer_c_limited)
    assert hidden.status_code == 404
    cost = await api.get("/v1/cost/summary", principals.customer_c_limited, params={"months": "12"})
    assert cost.status_code == 200
    assert {line["provider"] for line in cost.json()["lines"]} <= {"azure"}
    # The same tenant, unrestricted, sees both subscriptions.
    staff = await api.get("/v1/resources", principals.finops_all)
    assert {str(seeded.c.resource_id), str(seeded.c_resource2)} <= _ids(staff.json())


# --- Provenance and audit -----------------------------------------------------------------


async def test_every_metric_carries_provenance_and_money_is_a_string(
    api: Api, principals: Principals
) -> None:
    cost = (await api.get("/v1/cost/summary", principals.finops_all, params={"days": "7"})).json()
    assert cost["lines"]
    for line in cost["lines"]:
        metric = line["amortized"]
        assert set(metric) == {
            "value", "unit", "source_system", "as_of", "query_ref", "is_modelled"
        }
        assert isinstance(metric["value"], str)
        assert metric["unit"] == line["currency"]
        assert datetime.fromisoformat(metric["as_of"]).tzinfo is not None
    count = (await api.get("/v1/resources", principals.finops_all)).json()["count"]
    assert count["unit"] == "resources"
    assert count["is_modelled"] is False


async def test_every_access_is_written_to_the_audit_log(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    started = datetime.now(UTC)
    await api.get(f"/v1/resources/{seeded.a.resource_id}", principals.customer_a)
    await api.get(f"/v1/resources/{seeded.b.resource_id}", principals.customer_a)
    await api.get("/v1/resources", principals.tickets_only)
    with psycopg.connect(seeded.admin_dsn) as cx:
        rows = cx.execute(
            "SELECT actor, action, target_ref, outcome, tenant_id FROM audit.action_log"
            " WHERE created_at >= %s AND actor = ANY(%s) ORDER BY id",
            (started, [f"user:{principals.customer_a}", f"user:{principals.tickets_only}"]),
        ).fetchall()
    actor_a = f"user:{principals.customer_a}"
    assert (
        actor_a, "GET /v1/resources/{resource_id}",
        f"resource_id={seeded.a.resource_id}", "succeeded", seeded.a.id,
    ) in rows
    assert (
        actor_a, "GET /v1/resources/{resource_id}",
        f"resource_id={seeded.b.resource_id}", "failed", seeded.a.id,
    ) in rows, "the probe for another tenant's resource is on record, under the caller's tenant"
    assert any(
        r[0] == f"user:{principals.tickets_only}" and r[3] == "blocked" for r in rows
    ), "a matrix refusal is audited as blocked"


# --- Probes (step 10): ready against the real database, and still no data ------------


async def test_readyz_is_ready_against_the_real_database_and_writes_no_audit(
    api: Api, seeded: Seeded
) -> None:
    with psycopg.connect(seeded.admin_dsn) as conn:
        before = conn.execute("SELECT count(*) FROM audit.action_log").fetchone()
    response = await api.client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    with psycopg.connect(seeded.admin_dsn) as conn:
        after = conn.execute("SELECT count(*) FROM audit.action_log").fetchone()
    assert after == before
