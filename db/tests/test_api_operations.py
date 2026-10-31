"""The operations console's API against the real schema (step 14, ADR 0014): the
work queue, a ticket with its triage, a review of that triage, a resource's
context and the customer overview.

Same guarantees as the rest of the API: another tenant's object is a 404 like
one that never existed, triage is staff-only, a figure the caller has no
permission for is absent rather than zero, and a review is written only by a
holder of ``triage.review`` against a triage it can see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from .conftest import Api, Principals, Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


@dataclass(frozen=True)
class Triaged:
    a: UUID  # tenant a's INC-a triage
    b: UUID  # tenant b's INC-b triage


@pytest.fixture(scope="module")
def triaged(seeded: Seeded) -> Triaged:
    ids: dict[str, UUID] = {}
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for t in (seeded.a, seeded.b):
            rid = uuid4()
            cx.execute(
                "INSERT INTO cloudops.triage_result (id, tenant_id, number, resource_id, route,"
                " stage, reasons, classification, injection_signals, cost, cost_currency,"
                " workflow_id) VALUES (%s, %s, %s, %s, 'human', 2, ARRAY['low confidence'],"
                " '{\"intent\": \"disk_space\", \"risk\": \"low\", \"action_class\": \"B\"}',"
                " '[]', 0.001250, 'USD', %s)",
                (rid, t.id, f"INC-{t.name}", t.resource_id, f"triage-ops-{rid}"),
            )
            ids[t.name] = rid
    return Triaged(ids[seeded.a.name], ids[seeded.b.name])


async def _post(api: Api, path: str, oid: UUID, body: dict[str, Any]) -> Any:
    token = api.token(oid)
    return await api.client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


# --- Work queue -------------------------------------------------------------------


async def test_the_queue_holds_only_the_callers_tenants(
    api: Api, principals: Principals, seeded: Seeded, triaged: Triaged
) -> None:
    customer = (await api.get("/v1/work-items", principals.customer_a)).json()
    engineer = (await api.get("/v1/work-items", principals.engineer_a)).json()
    assert {i["number"] for i in customer["items"]} == {f"INC-{seeded.a.name}"}
    assert {i["number"] for i in engineer["items"]} == {f"INC-{seeded.a.name}"}
    assert customer["count"]["value"] == 1
    assert customer["count"]["source_system"] == "servicenow"
    # The agents' route is staff-only.
    assert customer["items"][0]["triage_route"] is None
    assert engineer["items"][0]["triage_route"] == "human"


async def test_the_queue_filters_by_kind_and_search(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    changes = (await api.get("/v1/work-items", principals.engineer_a,
                             params={"kind": "change_task"})).json()
    assert changes["items"] == []
    found = (await api.get("/v1/work-items", principals.engineer_a,
                           params={"q": seeded.a.name, "status": "all"})).json()
    assert [i["number"] for i in found["items"]] == [f"INC-{seeded.a.name}"]
    # Wildcards in the search are text, not patterns.
    wild = (await api.get("/v1/work-items", principals.engineer_a, params={"q": "%"})).json()
    assert len(wild["items"]) == 1


async def test_the_queue_needs_tickets_read(api: Api, principals: Principals) -> None:
    response = await api.get("/v1/work-items", principals.finops_all)
    assert response.status_code == 403


# --- Ticket detail and triage -------------------------------------------------------


async def test_another_tenants_ticket_is_not_found(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    foreign = await api.get(f"/v1/work-items/INC-{seeded.b.name}", principals.customer_a)
    missing = await api.get("/v1/work-items/INC0000000", principals.customer_a)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"


async def test_triage_is_shown_to_staff_and_never_to_customers(
    api: Api, principals: Principals, seeded: Seeded, triaged: Triaged
) -> None:
    path = f"/v1/work-items/INC-{seeded.a.name}"
    customer = (await api.get(path, principals.customer_a)).json()
    staff = (await api.get(path, principals.engineer_a)).json()
    assert customer["triage"] is None
    assert customer["assigned_to"] is None
    triage = staff["triage"]
    assert triage["id"] == str(triaged.a)
    assert triage["classification"]["intent"] == "disk_space"
    assert triage["cost"] | {"as_of": None, "query_ref": None} == {
        "value": "0.001250", "unit": "USD", "source_system": "cloudops_ai_gateway",
        "as_of": None, "query_ref": None, "is_modelled": False,
    }
    assert triage["plan"] is None


async def test_an_engineer_accepts_a_triage_and_the_review_is_shown(
    api: Api, principals: Principals, seeded: Seeded, triaged: Triaged
) -> None:
    number = f"INC-{seeded.a.name}"
    saved = await _post(api, f"/v1/work-items/{number}/triage/{triaged.a}/reviews",
                        principals.engineer_a, {"decision": "accept"})
    assert saved.status_code == 201, saved.text
    assert saved.json()["reviewer_oid"] == str(principals.engineer_a)
    detail = (await api.get(f"/v1/work-items/{number}", principals.engineer_a)).json()
    assert saved.json()["id"] in {r["id"] for r in detail["triage"]["reviews"]}


async def test_an_override_names_valid_values_and_a_reason(
    api: Api, principals: Principals, seeded: Seeded, triaged: Triaged
) -> None:
    path = f"/v1/work-items/INC-{seeded.a.name}/triage/{triaged.a}/reviews"
    good = await _post(api, path, principals.engineer_a, {
        "decision": "override", "overrides": {"intent": "backup_failure", "risk": "medium"},
        "reason": "The disk is the backup target; the backup is what failed.",
    })
    assert good.status_code == 201, good.text
    assert good.json()["overrides"] == {"intent": "backup_failure", "risk": "medium"}
    for body in (
        {"decision": "override", "overrides": {"intent": "made_up"}, "reason": "x"},
        {"decision": "override", "overrides": {"route": "ready_for_planning"}, "reason": "x"},
        {"decision": "override", "overrides": {"risk": "low"}},
        {"decision": "override", "overrides": {}, "reason": "x"},
        {"decision": "accept", "overrides": {"risk": "low"}},
        {"decision": "approve"},
    ):
        bad = await _post(api, path, principals.engineer_a, body)
        assert bad.status_code == 422, body


async def test_a_review_needs_the_permission_and_a_visible_triage(
    api: Api, principals: Principals, seeded: Seeded, triaged: Triaged
) -> None:
    a_number, b_number = f"INC-{seeded.a.name}", f"INC-{seeded.b.name}"
    accept = {"decision": "accept"}
    # A role without triage.review, and a customer, are refused before any lookup.
    for oid in (principals.tickets_only, principals.customer_a):
        refused = await _post(api, f"/v1/work-items/{a_number}/triage/{triaged.a}/reviews",
                              oid, accept)
        assert refused.status_code == 403
    # Another tenant's triage, a triage under the wrong ticket and an unknown one: 404.
    for number, result in ((b_number, triaged.b), (b_number, triaged.a), (a_number, uuid4())):
        missing = await _post(api, f"/v1/work-items/{number}/triage/{result}/reviews",
                              principals.engineer_a, accept)
        assert missing.status_code == 404, (number, result)
    with psycopg.connect(seeded.admin_dsn) as cx:
        on_b = cx.execute("SELECT count(*) FROM cloudops.triage_review"
                          " WHERE triage_result_id = %s", (triaged.b,)).fetchone()
    assert on_b == (0,)


# --- Resource context ---------------------------------------------------------------


async def test_resource_context_is_scoped_and_permission_shaped(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    foreign = await api.get(f"/v1/resources/{seeded.b.resource_id}/context",
                            principals.customer_a)
    assert foreign.status_code == 404
    own = (await api.get(f"/v1/resources/{seeded.a.resource_id}/context",
                         principals.customer_a)).json()
    assert own["resource"]["id"] == str(seeded.a.resource_id)
    assert own["metrics"]["available"] is False
    assert [i["number"] for i in own["open_incidents"]] == [f"INC-{seeded.a.name}"]
    assert own["recent_changes"] == []
    # FinOps reads inventory but neither tickets nor vulnerabilities: absent, not empty.
    finops = (await api.get(f"/v1/resources/{seeded.b.resource_id}/context",
                            principals.finops_all)).json()
    assert finops["open_incidents"] is None
    assert finops["vulnerabilities"] is None
    assert finops["open_vulnerabilities"] is None


# --- Customer overview --------------------------------------------------------------


async def test_customer_overview_is_scoped_and_absent_is_not_zero(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    customer = (await api.get("/v1/customers", principals.customer_a)).json()
    assert [c["tenant"]["id"] for c in customer] == [str(seeded.a.id)]
    assert customer[0]["open_incidents"]["value"] == 1
    # A customer sees the price, never the cost (ADR 0021); no uplift here.
    assert customer[0]["cost_30d"] is None
    assert [(p["currency"], p["price"], p["not_captured"]) for p in customer[0]["price_30d"]] \
        == [(seeded.a.currency, None, "no_uplift")]

    finops = (await api.get("/v1/customers", principals.finops_all)).json()
    assert {c["tenant"]["id"] for c in finops} >= {str(t.id) for t in seeded.tenants}
    for row in finops:
        assert row["open_incidents"] is None
        assert row["critical_vulnerabilities"] is None
        assert row["resources"] is not None
