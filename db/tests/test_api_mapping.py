"""The customer-mapping API against the real schema (step 14b, ADR 0015): only
``mapping.manage`` reaches it, a customer never does, the two-person rule and
scope hold through the API, and refusals are structured codes that name no
tenant."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, TenantScope

from .conftest import Api, Principals, Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

ENDPOINTS = (
    ("GET", "/v1/mapping/quarantine", None),
    ("GET", "/v1/mapping/tenants", None),
    ("GET", "/v1/mapping/proposals", None),
    ("POST", "/v1/mapping/tenants",
     {"canonical_name": "Probe Co", "short_code": "PROBE", "internal": False}),
    ("POST", f"/v1/mapping/tenants/{uuid4()}/kind", {"internal": True}),
    ("POST", "/v1/mapping/platform-host", {"tenant_id": None}),
    ("POST", "/v1/mapping/proposals",
     {"source_system": "servicenow", "alias": "Probe", "tenant_id": str(uuid4())}),
    ("POST", f"/v1/mapping/proposals/{uuid4()}/decision", {"decision": "approve"}),
    ("GET", f"/v1/mapping/proposals/{uuid4()}/reprocess", None),
    ("POST", f"/v1/mapping/proposals/{uuid4()}/reprocess", None),
)


@pytest.fixture(scope="module")
async def mapper_a(seeded: Seeded, principals: Principals) -> UUID:
    """A Customer master assigned tenant a only."""
    oid = uuid4()
    db = Database.create(seeded.app_dsn, pool_size=1, max_overflow=0)
    try:
        async with db.transaction(TenantScope.for_resolution("setup:mapper")) as conn:
            await conn.execute(text(
                "SELECT access.set_principal(:a, :o, 'staff', 'mapper a', 'Customer master',"
                " false)"), {"a": principals.admin, "o": oid})
            await conn.execute(text("SELECT access.grant_tenant(:a, :o, :t)"),
                               {"a": principals.admin, "o": oid, "t": seeded.a.id})
    finally:
        await db.dispose()
    return oid


async def _send(api: Api, method: str, path: str, oid: UUID, body: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {api.token(oid)}"}
    if method == "GET":
        return await api.client.get(path, headers=headers)
    return await api.client.post(path, json=body, headers=headers)


def _quarantine(seeded: Seeded, alias: str, count: int) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO staging.tenant_alias_quarantine (source_system, alias,"
            " occurrence_count, raw_ref) VALUES ('servicenow', %s, %s, 'servicenow/incident/x')",
            (alias, count))


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
async def test_only_mapping_holders_reach_any_of_it(
    api: Api, principals: Principals, method: str, path: str, body: Any
) -> None:
    for oid in (principals.customer_a, principals.customer_c_limited, principals.engineer_a,
                principals.finops_all, principals.tickets_only):
        response = await _send(api, method, path, oid, body)
        assert response.status_code == 403, (oid, path)
        assert response.json()["error"]["code"] == "forbidden"


async def test_the_quarantine_lists_names_and_counts_never_payload_refs(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    alias = f"API Quarantined {uuid4().hex[:6]}"
    _quarantine(seeded, alias, 11)
    response = await _send(api, "GET", "/v1/mapping/quarantine", principals.admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approvers_required"] == 2
    assert "raw_ref" not in response.text
    item = next(i for i in body["items"] if i["alias"] == alias)
    assert item["occurrences"]["value"] == 11
    assert item["occurrences"]["unit"] == "sightings"
    assert item["occurrences"]["source_system"] == "servicenow"
    values = [i["occurrences"]["value"] for i in body["items"]]
    assert values == sorted(values, reverse=True)


async def test_onboard_then_map_then_approve_by_a_second_person(
    api: Api, principals: Principals, seeded: Seeded, mapper_a: UUID
) -> None:
    alias = f"API Alpha {uuid4().hex[:6]}"
    _quarantine(seeded, alias, 4)
    a = str(seeded.a.id)

    code = f"N{uuid4().hex[:6].upper()}"
    onboarded = await _send(api, "POST", "/v1/mapping/tenants", principals.admin,
                            {"canonical_name": f"New Customer {code}", "short_code": code,
                             "internal": False})
    assert onboarded.status_code == 201, onboarded.text
    tenants = (await _send(api, "GET", "/v1/mapping/tenants", principals.admin)).json()
    assert onboarded.json()["tenant"]["id"] in {t["tenant"]["id"] for t in tenants}
    again = await _send(api, "POST", "/v1/mapping/tenants", principals.admin,
                        {"canonical_name": "Other", "short_code": code, "internal": False})
    assert again.status_code == 409

    proposed = await _send(api, "POST", "/v1/mapping/proposals", principals.admin,
                           {"source_system": "servicenow", "alias": alias, "tenant_id": a})
    assert proposed.status_code == 201, proposed.text
    proposal = proposed.json()
    assert proposal["proposed_by_me"] is True
    assert proposal["may_approve"] is False
    path = f"/v1/mapping/proposals/{proposal['id']}/decision"

    self_approved = await _send(api, "POST", path, principals.admin, {"decision": "approve"})
    assert self_approved.status_code == 409
    assert self_approved.json()["error"]["code"] == "second_approver_required"

    pending = (await _send(api, "GET", "/v1/mapping/proposals", mapper_a)).json()
    mine = next(p for p in pending if p["id"] == proposal["id"])
    assert mine["may_approve"] is True

    approved = await _send(api, "POST", path, mapper_a, {"decision": "approve"})
    assert approved.status_code == 201, approved.text
    decision = approved.json()["decision"]
    assert decision["decision"] == "approve"
    assert decision["decided_by"] == str(mapper_a)
    assert decision["held_occurrences"]["value"] == 4

    tenants = (await _send(api, "GET", "/v1/mapping/tenants", mapper_a)).json()
    (alpha,) = [t for t in tenants if t["tenant"]["id"] == a]
    assert alias in {x["alias"] for x in alpha["aliases"]}
    quarantine = (await _send(api, "GET", "/v1/mapping/quarantine", mapper_a)).json()
    assert alias not in {i["alias"] for i in quarantine["items"]}
    assert (await _send(api, "POST", path, mapper_a, {"decision": "reject",
                                                      "reason": "late"})).status_code == 409

    with psycopg.connect(seeded.admin_dsn) as cx:
        audited = cx.execute(
            "SELECT tenant_id, outcome FROM audit.action_log WHERE actor = %s"
            " AND action = 'POST /v1/mapping/proposals/{proposal_id}/decision'",
            (f"user:{mapper_a}",)).fetchall()
    assert (seeded.a.id, "succeeded") in audited


async def test_a_mapping_never_reaches_a_tenant_outside_scope(
    api: Api, principals: Principals, seeded: Seeded, mapper_a: UUID
) -> None:
    b = str(seeded.b.id)
    refused = await _send(api, "POST", "/v1/mapping/proposals", mapper_a,
                          {"source_system": "servicenow", "alias": "Bravo Probe", "tenant_id": b})
    unknown = await _send(api, "POST", "/v1/mapping/proposals", mapper_a,
                          {"source_system": "servicenow", "alias": "Nobody",
                           "tenant_id": str(uuid4())})
    assert refused.status_code == unknown.status_code == 404
    assert refused.json()["error"]["code"] == unknown.json()["error"]["code"] == "not_found"

    theirs = await _send(api, "POST", "/v1/mapping/proposals", principals.admin,
                         {"source_system": "servicenow", "alias": f"Bravo {uuid4().hex[:6]}",
                          "tenant_id": b})
    assert theirs.status_code == 201
    pending = (await _send(api, "GET", "/v1/mapping/proposals", mapper_a)).json()
    assert theirs.json()["id"] not in {p["id"] for p in pending}
    decided = await _send(api, "POST", f"/v1/mapping/proposals/{theirs.json()['id']}/decision",
                          mapper_a, {"decision": "approve"})
    assert decided.status_code == 404
    tenants = (await _send(api, "GET", "/v1/mapping/tenants", mapper_a)).json()
    assert [t["tenant"]["id"] for t in tenants] == [str(seeded.a.id)]


@pytest.mark.parametrize("body", [
    {"decision": "reject"},
    {"decision": "reject", "reason": "   "},
    {"decision": "maybe"},
])
async def test_a_decision_is_well_formed(
    api: Api, principals: Principals, seeded: Seeded, mapper_a: UUID, body: dict[str, str]
) -> None:
    proposed = await _send(api, "POST", "/v1/mapping/proposals", principals.admin,
                           {"source_system": "servicenow", "alias": f"Form {uuid4().hex[:6]}",
                            "tenant_id": str(seeded.a.id)})
    response = await _send(api, "POST",
                           f"/v1/mapping/proposals/{proposed.json()['id']}/decision",
                           mapper_a, body)
    assert response.status_code == 422


@pytest.mark.parametrize("body", [
    {"source_system": "ServiceNow", "alias": "x"},
    {"source_system": "servicenow", "alias": ""},
    {"source_system": "servicenow", "alias": "   "},
    {"source_system": "servicenow", "alias": "x" * 201},
])
async def test_a_proposal_is_well_formed(
    api: Api, principals: Principals, seeded: Seeded, body: dict[str, str]
) -> None:
    response = await _send(api, "POST", "/v1/mapping/proposals", principals.admin,
                           {**body, "tenant_id": str(seeded.a.id)})
    assert response.status_code == 422


async def test_reprocess_is_previewed_then_requested_once_and_never_started_by_the_api(
    api: Api, principals: Principals, seeded: Seeded, mapper_a: UUID
) -> None:
    alias = f"API Held {uuid4().hex[:6]}"
    _quarantine(seeded, alias, 9)
    proposed = (await _send(api, "POST", "/v1/mapping/proposals", principals.admin,
                            {"source_system": "servicenow", "alias": alias,
                             "tenant_id": str(seeded.a.id)})).json()
    path = f"/v1/mapping/proposals/{proposed['id']}/reprocess"
    early = (await _send(api, "GET", path, mapper_a)).json()
    assert (early["available"], early["reason"]) == (False, "not_approved")
    refused = await _send(api, "POST", path, mapper_a)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "not_approved"

    await _send(api, "POST", f"/v1/mapping/proposals/{proposed['id']}/decision", mapper_a,
                {"decision": "approve"})
    preview = (await _send(api, "GET", path, mapper_a)).json()
    assert preview["available"] is True
    assert preview["upper_bound"]["value"] == 9
    assert preview["upper_bound"]["unit"] == "sightings"
    assert preview["window_start"] < preview["window_end"]

    requested = await _send(api, "POST", path, mapper_a)
    assert requested.status_code == 201, requested.text
    reprocess = requested.json()["reprocess"]
    assert reprocess["status"] == "requested"
    assert reprocess["requested_by_me"] is True
    assert reprocess["moved"] is None
    again = await _send(api, "POST", path, principals.admin)
    assert again.status_code == 409
    assert (await _send(api, "GET", path, mapper_a)).json()["reason"] == "already_requested"
    decided = (await _send(api, "GET", "/v1/mapping/proposals?status=decided", mapper_a)).json()
    assert next(p for p in decided if p["id"] == proposed["id"])["reprocess"]["status"] == \
        "requested"


async def test_reprocess_needs_something_held_and_a_proposal_in_scope(
    api: Api, principals: Principals, seeded: Seeded, mapper_a: UUID
) -> None:
    nothing = (await _send(api, "POST", "/v1/mapping/proposals", principals.admin,
                           {"source_system": "servicenow", "alias": f"Never Held {uuid4().hex[:6]}",
                            "tenant_id": str(seeded.a.id)})).json()
    await _send(api, "POST", f"/v1/mapping/proposals/{nothing['id']}/decision", mapper_a,
                {"decision": "approve"})
    response = await _send(api, "POST", f"/v1/mapping/proposals/{nothing['id']}/reprocess",
                           mapper_a)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "nothing_held"

    theirs = (await _send(api, "POST", "/v1/mapping/proposals", principals.admin,
                          {"source_system": "servicenow", "alias": f"Bravo {uuid4().hex[:6]}",
                           "tenant_id": str(seeded.b.id)})).json()
    for method in ("GET", "POST"):
        hidden = await _send(api, method, f"/v1/mapping/proposals/{theirs['id']}/reprocess",
                             mapper_a)
        assert hidden.status_code == 404, method


# --- internal tenants and the platform host (ADR 0031) ------------------------------


async def test_a_platform_administrator_chooses_internal_at_onboarding_and_later(
    api: Api, principals: Principals, seeded: Seeded, mapper_a: UUID
) -> None:
    code = f"I{uuid4().hex[:6].upper()}"
    body = {"canonical_name": f"Getronics Unit {code}", "short_code": code}
    # No default: the choice is made, never assumed.
    assert (await _send(api, "POST", "/v1/mapping/tenants", principals.admin,
                        body)).status_code == 422
    # A customer master may onboard customers, not internal tenants.
    refused = await _send(api, "POST", "/v1/mapping/tenants", mapper_a,
                          {**body, "internal": True})
    assert refused.status_code == 403
    made = await _send(api, "POST", "/v1/mapping/tenants", principals.admin,
                       {**body, "internal": True})
    assert made.status_code == 201, made.text
    tid = made.json()["tenant"]["id"]
    listed = {t["tenant"]["id"]: t for t in
              (await _send(api, "GET", "/v1/mapping/tenants", principals.admin)).json()}
    assert (listed[tid]["internal"], listed[tid]["hosts_platform"]) == (True, False)
    assert listed[str(seeded.a.id)]["internal"] is False

    # It hosts the platform; a customer cannot; the host cannot stop being internal.
    hosted = await _send(api, "POST", "/v1/mapping/platform-host", principals.admin,
                         {"tenant_id": tid})
    assert hosted.status_code == 200, hosted.text
    assert hosted.json()["hosts_platform"] is True
    for path, change in ((f"/v1/mapping/tenants/{tid}/kind", {"internal": False}),
                         ("/v1/mapping/platform-host", {"tenant_id": str(seeded.a.id)})):
        conflict = await _send(api, "POST", path, principals.admin, change)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "platform_host_rule"
        assert str(seeded.a.id) not in conflict.text
    cleared = await _send(api, "POST", "/v1/mapping/platform-host", principals.admin,
                          {"tenant_id": None})
    assert cleared.status_code == 200
    changed = await _send(api, "POST", f"/v1/mapping/tenants/{tid}/kind", principals.admin,
                          {"internal": False})
    assert changed.status_code == 200
    assert (changed.json()["internal"], changed.json()["hosts_platform"]) == (False, False)
    # A customer master does not change a tenant's kind, even in its scope.
    assert (await _send(api, "POST", f"/v1/mapping/tenants/{seeded.a.id}/kind", mapper_a,
                        {"internal": True})).status_code == 403
    with psycopg.connect(seeded.admin_dsn) as cx:
        logged = [r[0] for r in cx.execute(
            "SELECT action FROM access.change_log WHERE detail->>'tenant_id' = %s"
            " ORDER BY id", (tid,)).fetchall()]
    assert logged == ["onboard_tenant", "set_tenant_internal", "set_platform_host",
                      "set_tenant_internal"]
