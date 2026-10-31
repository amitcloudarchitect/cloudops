"""Assurance against the real schema (step 17, ADR 0019; HLD M14, M15).

The library loads only through ``access.load_controls``; evidence is routed by
each control's declared query and stored append-only with its query, raw value
and timestamp; a result scores only once a person holding ``assurance.approve``
approves it, which the database enforces as well as the API; and the evidence
pack is signed and verifiable. Isolation: another tenant's assessment, pack or
evidence is a 404, and a subscription-limited scope sees no assessment at all.

The controls here are test controls (``TEST-*``). The real 98-control library is
supplied by Security and is not in the repository.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from cloudops_domain.assurance.pack import verify

from .conftest import Step17

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

LIBRARY: dict[str, Any] = {
    "framework": "test_assurance", "version": "test-1", "expected_count": 4,
    "controls": [
        {"code": "TEST-VUL-01", "domain": "Vulnerability", "title": "Age of open criticals",
         "requirement": "No severity 4 or 5 vulnerability stays open longer than a day.",
         "assessment": "automated", "evidence_source": "qualys",
         "evidence_query": "qualys.open_vulnerabilities_over_age",
         "parameters": {"min_severity": 4, "max_age_days": 1, "compliant_max": 0,
                        "partial_max": 5}},
        {"code": "TEST-IAM-01", "domain": "Identity", "title": "Global administrators",
         "requirement": "At most two accounts hold Global Administrator.",
         "assessment": "automated", "evidence_source": "entra",
         "evidence_query": "entra.role_holders",
         "parameters": {"role_name": "Global Administrator", "compliant_max": 2}},
        {"code": "TEST-GOV-01", "domain": "Governance", "title": "Change policy",
         "requirement": "A documented change management policy exists and is followed.",
         "assessment": "narrative", "evidence_source": "policy_library"},
        {"code": "TEST-PHY-01", "domain": "Physical", "title": "Data centre access",
         "requirement": "Physical access to the data centre is badge controlled.",
         "assessment": "manual", "evidence_source": "human"},
    ],
}


@pytest.fixture(scope="module")
async def assessment(step17: Step17) -> UUID:
    loaded = await step17.api.post("/v1/assurance/library", step17.admin, LIBRARY)
    assert loaded.status_code == 201, loaded.text
    assert loaded.json()["loaded"]["value"] == 4
    opened = await step17.api.post("/v1/assurance/assessments", step17.secops_a, {
        "tenant_id": str(step17.seeded.a.id), "framework": "test_assurance",
        "title": "Alpha test assessment"})
    assert opened.status_code == 201, opened.text
    return UUID(opened.json()["id"])


async def _detail(step17: Step17, assessment: UUID, oid: UUID | None = None) -> dict[str, Any]:
    got = await step17.api.get(f"/v1/assurance/assessments/{assessment}",
                               oid or step17.secops_a)
    assert got.status_code == 200, got.text
    return dict(got.json())


def _response(detail: dict[str, Any], code: str) -> dict[str, Any]:
    return next(r for r in detail["responses"] if r["control"]["code"] == code)


# --- the library ----------------------------------------------------------------------


async def test_only_the_library_permission_loads_controls(step17: Step17) -> None:
    bad = copy.deepcopy(LIBRARY)
    bad["framework"] = "test_other"
    refused = await step17.api.post("/v1/assurance/library", step17.delivery_a, bad)
    assert refused.status_code == 403
    invalid = copy.deepcopy(bad)
    invalid["expected_count"] = 98
    got = await step17.api.post("/v1/assurance/library", step17.admin, invalid)
    assert got.status_code == 422  # validated before anything is written
    with psycopg.connect(step17.seeded.app_dsn) as cx, pytest.raises(
            psycopg.errors.InsufficientPrivilege):
        cx.execute("INSERT INTO cloudops.control (code, framework, domain, title, requirement,"
                   " assessment, evidence_source, library_version) VALUES ('TEST-X-01', 'x',"
                   " 'd', 't', 'r', 'manual', 'human', 'v')")


async def test_frameworks_and_routing_are_listed(step17: Step17, assessment: UUID) -> None:
    frameworks = (await step17.api.get("/v1/assurance/frameworks", step17.auditor_all)).json()
    mine = next(f for f in frameworks if f["framework"] == "test_assurance")
    assert (mine["controls"]["value"], mine["automated"]["value"], mine["narrative"]["value"],
            mine["manual"]["value"]) == (4, 2, 1, 1)
    routing = (await step17.api.get("/v1/assurance/evidence-routing", step17.secops_a)).json()
    assert {"qualys", "entra", "servicenow", "sharepoint", "corestack", "azure", "aws"} == {
        r["source_system"] for r in routing}


# --- assessments and evidence -----------------------------------------------------------


async def test_an_assessment_is_opened_only_within_scope(step17: Step17, assessment: UUID
                                                          ) -> None:
    other = await step17.api.post("/v1/assurance/assessments", step17.secops_a, {
        "tenant_id": str(step17.seeded.b.id), "framework": "test_assurance", "title": "x"})
    assert other.status_code == 404
    limited = await step17.api.post("/v1/assurance/assessments", step17.delivery_limited, {
        "tenant_id": str(step17.seeded.c.id), "framework": "test_assurance", "title": "x"})
    assert limited.status_code == 404  # tenant-wide: not for a subscription-limited scope
    engineer = await step17.api.post("/v1/assurance/assessments", step17.engineer_a, {
        "tenant_id": str(step17.seeded.a.id), "framework": "test_assurance", "title": "x"})
    assert engineer.status_code == 403
    detail = await _detail(step17, assessment)
    assert len(detail["responses"]) == 4
    assert detail["score"]["percent"] is None  # nothing approved: no score, not zero
    assert detail["score"]["pending"]["value"] == 4


async def test_evidence_is_routed_stored_and_proposes_only(step17: Step17, assessment: UUID
                                                           ) -> None:
    got = await step17.api.post(f"/v1/assurance/assessments/{assessment}/collect",
                                step17.secops_a)
    assert got.status_code == 200, got.text
    proposals = {c["control_code"]: c["proposed_result"] for c in got.json()}
    # Alpha: at least one open severity 4+ detection over a day old (at most five,
    # whatever other suites add); one Global Administrator.
    assert proposals == {"TEST-VUL-01": "partially_compliant", "TEST-IAM-01": "compliant"}
    detail = await _detail(step17, assessment)
    vul = _response(detail, "TEST-VUL-01")
    assert vul["result"] is None  # proposed, not approved: it does not score
    (ev,) = vul["evidence"]
    assert ev["source_system"] == "qualys"
    with psycopg.connect(step17.seeded.admin_dsn) as cx:
        expected = cx.execute(
            "SELECT count(*) FROM cloudops.vulnerability WHERE tenant_id = %s"
            " AND remediated_at IS NULL AND severity >= 4"
            " AND first_detected < %s::timestamptz - interval '1 day'",
            (step17.seeded.a.id, ev["retrieved_at"])).fetchone()
    assert expected is not None
    assert ev["raw_value"]["open_over_age"] == expected[0] >= 1  # never another tenant's
    assert "FROM cloudops.vulnerability" in ev["query_text"]
    assert ev["parameters"]["max_age_days"] == 1
    assert ev["retrieved_at"]
    assert len(ev["sha256"]) == 64
    assert detail["score"]["percent"] is None


@pytest.mark.parametrize("login", ["app_dsn", "migrator_dsn"])
def test_evidence_is_append_only_even_for_the_owner(step17: Step17, login: str) -> None:
    s = step17.seeded
    with psycopg.connect(getattr(s, login)) as cx:
        cx.execute("SELECT set_config('app.tenant_ids', %s, true),"
                   " set_config('app.role', 'platform_admin', true),"
                   " set_config('app.account_ids', 'all', true)", (str(s.a.id),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cx.execute("UPDATE cloudops.evidence SET raw_value = '{}'")
    with psycopg.connect(s.admin_dsn) as cx, pytest.raises(
            psycopg.errors.InsufficientPrivilege):
        cx.execute("DELETE FROM cloudops.evidence")  # the trigger holds even for a superuser


# --- approval --------------------------------------------------------------------------


async def test_only_an_approver_can_score_and_never_on_nothing(step17: Step17,
                                                               assessment: UUID) -> None:
    api, base = step17.api, f"/v1/assurance/assessments/{assessment}/responses"
    for oid in (step17.engineer_a, step17.auditor_all, step17.customer_a):
        refused = await api.post(f"{base}/TEST-IAM-01/approve", oid, {"result": "compliant"})
        assert refused.status_code == 403, oid
    # Compliant needs a narrative, and collected evidence or a citation behind it.
    bare = await api.post(f"{base}/TEST-IAM-01/approve", step17.secops_a,
                          {"result": "compliant"})
    assert (bare.status_code, bare.json()["error"]["code"]) == (
        422, "compliant_needs_narrative_and_evidence")
    no_evidence = await api.post(f"{base}/TEST-GOV-01/narrative", step17.secops_a,
                                 {"narrative": "A policy exists, says the customer."})
    assert no_evidence.status_code == 200
    assert (await api.post(f"{base}/TEST-GOV-01/approve", step17.secops_a,
                           {"result": "compliant"})).status_code == 422
    wrong = await api.post(f"{base}/TEST-IAM-01/approve", step17.secops_a, {"result": "manual"})
    assert wrong.json()["error"]["code"] == "result_not_allowed_for_control"
    manual_compliant = await api.post(f"{base}/TEST-PHY-01/approve", step17.secops_a,
                                      {"result": "compliant"})
    assert manual_compliant.json()["error"]["code"] == "result_not_allowed_for_control"

    await api.post(f"{base}/TEST-IAM-01/narrative", step17.secops_a,
                   {"narrative": "One account holds Global Administrator."})
    ok = await api.post(f"{base}/TEST-IAM-01/approve", step17.secops_a,
                        {"result": "compliant", "note": "checked in Entra"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["approved_by"] == str(step17.secops_a)
    await api.post(f"{base}/TEST-VUL-01/narrative", step17.delivery_a,
                   {"narrative": "One severity 4 detection is older than a day."})
    assert (await api.post(f"{base}/TEST-VUL-01/approve", step17.delivery_a,
                           {"result": "partially_compliant"})).status_code == 200
    assert (await api.post(f"{base}/TEST-GOV-01/approve", step17.secops_a,
                           {"result": "non_compliant"})).status_code == 200
    assert (await api.post(f"{base}/TEST-PHY-01/approve", step17.secops_a,
                           {"result": "manual"})).status_code == 200
    score = (await _detail(step17, assessment))["score"]
    assert score["percent"]["value"] == "50.00"  # (100 + 50 + 0) / 3; manual excluded
    assert (score["scorable"]["value"], score["excluded"]["value"],
            score["pending"]["value"]) == (3, 1, 0)

    # Editing an approved narrative withdraws the approval.
    edited = await api.post(f"{base}/TEST-GOV-01/narrative", step17.secops_a,
                            {"narrative": "Revised after the audit."})
    assert edited.json()["result"] is None
    assert edited.json()["approved_at"] is None
    assert (await _detail(step17, assessment))["score"]["pending"]["value"] == 1
    await api.post(f"{base}/TEST-GOV-01/approve", step17.secops_a, {"result": "non_compliant"})


@pytest.mark.parametrize("approver", ["unknown", "engineer", "customer"])
def test_the_database_refuses_an_approval_from_anyone_but_an_approver(
    step17: Step17, assessment: UUID, approver: str
) -> None:
    oid = {"unknown": uuid4(), "engineer": step17.engineer_a,
           "customer": step17.customer_a}[approver]
    s = step17.seeded
    with psycopg.connect(s.app_dsn) as cx:
        cx.execute("SELECT set_config('app.tenant_ids', %s, true),"
                   " set_config('app.role', 'platform_admin', true),"
                   " set_config('app.account_ids', 'all', true)", (str(s.a.id),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match=r"assurance\.approve"):
            cx.execute("UPDATE cloudops.control_response SET result = 'compliant',"
                       " approved_by = %s, approved_at = now()"
                       " WHERE assessment_id = %s AND control_code = 'TEST-GOV-01'",
                       (oid, assessment))


def test_a_result_needs_an_approval(step17: Step17, assessment: UUID) -> None:
    s = step17.seeded
    with psycopg.connect(s.app_dsn) as cx:
        cx.execute("SELECT set_config('app.tenant_ids', %s, true),"
                   " set_config('app.role', 'platform_admin', true),"
                   " set_config('app.account_ids', 'all', true)", (str(s.a.id),))
        with pytest.raises(psycopg.errors.CheckViolation):
            cx.execute("UPDATE cloudops.control_response SET result = 'compliant',"
                       " approved_by = NULL, approved_at = NULL"
                       " WHERE assessment_id = %s AND control_code = 'TEST-GOV-01'",
                       (assessment,))


# --- the evidence pack -----------------------------------------------------------------


async def test_the_evidence_pack_is_signed_and_verifiable(step17: Step17,
                                                          assessment: UUID) -> None:
    api = step17.api
    assert (await api.post(f"/v1/assurance/assessments/{assessment}/evidence-packs",
                           step17.auditor_all)).status_code == 403  # auditors read only
    got = await api.post(f"/v1/assurance/assessments/{assessment}/evidence-packs",
                         step17.secops_a)
    assert got.status_code == 201, got.text
    doc = got.json()
    assert verify(doc, api.evidence_signer.public_key)
    manifest = doc["manifest"]
    assert manifest["format"] == "cloudops.evidence-pack/1"
    assert manifest["score"]["percent"] == "50.00"
    assert {r["control_code"] for r in manifest["responses"]} == {
        "TEST-VUL-01", "TEST-IAM-01", "TEST-GOV-01", "TEST-PHY-01"}
    assert {e["query_name"] for e in manifest["evidence"]} == {
        "qualys.open_vulnerabilities_over_age", "entra.role_holders"}
    assert all(e["retrieved_at"] and e["sha256"] for e in manifest["evidence"])

    packs = (await api.get(f"/v1/assurance/assessments/{assessment}/evidence-packs",
                           step17.auditor_all)).json()
    stored = (await api.get(f"/v1/assurance/evidence-packs/{packs[0]['id']}",
                            step17.auditor_all)).json()
    assert verify(stored, api.evidence_signer.public_key)  # as stored, still verifiable
    tampered = copy.deepcopy(stored)
    tampered["manifest"]["score"]["percent"] = "100.00"
    assert not verify(tampered, api.evidence_signer.public_key)


async def test_another_tenant_sees_none_of_it(step17: Step17, assessment: UUID) -> None:
    s, api = step17.seeded, step17.api
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        b_assessment = cx.execute(
            "INSERT INTO cloudops.assessment (tenant_id, framework, library_version, title,"
            " created_by) VALUES (%s, 'test_assurance', 'test-1', 'Bravo', %s) RETURNING id",
            (s.b.id, uuid4())).fetchone()
        b_pack = cx.execute("SELECT id FROM cloudops.evidence_pack WHERE tenant_id = %s",
                            (s.b.id,)).fetchone()
    assert b_assessment
    assert b_pack
    for path in (f"/v1/assurance/assessments/{b_assessment[0]}",
                 f"/v1/assurance/assessments/{b_assessment[0]}/evidence-packs",
                 f"/v1/assurance/evidence-packs/{b_pack[0]}"):
        assert (await api.get(path, step17.secops_a)).status_code == 404, path
    for path in (f"/v1/assurance/assessments/{b_assessment[0]}/collect",
                 f"/v1/assurance/assessments/{b_assessment[0]}/evidence-packs",
                 f"/v1/assurance/assessments/{b_assessment[0]}/drafts"):
        assert (await api.post(path, step17.secops_a)).status_code == 404, path
    listed = (await api.get("/v1/assurance/assessments", step17.secops_a)).json()
    assert {a["tenant"]["id"] for a in listed} == {str(s.a.id)}
    assert (await api.get("/v1/assurance/assessments", step17.customer_a)).status_code == 403
