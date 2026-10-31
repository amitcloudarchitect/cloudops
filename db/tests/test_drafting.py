"""AI drafting against the real schema (step 17, ADR 0019).

A draft reaches only the ``draft_*`` columns: it never sets a control's
result or a pack's text. A draft citing anything it was not given, or a
narrative with a number not in its data, is declined. Each draft runs under
its own tenant's scope, so the model sees one tenant's material only, and the
queue's tenant comes from the request row, never from the model.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from cloudops_agents import drafting
from cloudops_gateway.gateway import Gateway
from cloudops_gateway.pricing import LOCAL_DEPLOYMENTS
from cloudops_gateway.providers import Completion, MockProvider
from cloudops_tenancy import Database

from .conftest import Step17

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


def _reply(payload: dict[str, Any]) -> Completion:
    text = json.dumps(payload)
    return Completion(text, (), 0, len(text) // 4)


@pytest.fixture
async def drafter(step17: Step17) -> Any:
    db = Database.create(step17.seeded.app_dsn, pool_size=2, max_overflow=0)
    provider = MockProvider("mock")
    gateway = Gateway(db, {"mock": provider, "mock-secondary": MockProvider("mock-secondary")},
                      LOCAL_DEPLOYMENTS)
    try:
        yield drafting.DraftingActivities(db, gateway), provider
    finally:
        await db.dispose()


def _control_response(step17: Step17, tenant: UUID, code: str) -> tuple[UUID, UUID]:
    """A narrative control's response for ``tenant``, its draft requested, with one
    piece of evidence collected."""
    with psycopg.connect(step17.seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO cloudops.control (code, framework, domain, title, requirement,"
            " assessment, evidence_source, library_version) VALUES (%s, 'test_drafting',"
            " 'Governance', 'Change policy', 'A documented change policy is followed.',"
            " 'narrative', 'policy_library', 'test-1') ON CONFLICT DO NOTHING", (code,))
        row = cx.execute(
            "INSERT INTO cloudops.assessment (tenant_id, framework, library_version, title,"
            " created_by) VALUES (%s, 'test_drafting', 'test-1', 'Drafting', %s) RETURNING id",
            (tenant, uuid4())).fetchone()
        assert row
        response = cx.execute(
            "INSERT INTO cloudops.control_response (tenant_id, assessment_id, control_code,"
            " draft_state) VALUES (%s, %s, %s, 'requested') RETURNING id",
            (tenant, row[0], code)).fetchone()
        evidence = cx.execute(
            "INSERT INTO cloudops.evidence (tenant_id, assessment_id, control_code,"
            " source_system, query_name, query_ref, query_text, parameters, raw_value,"
            " retrieved_at, retrieved_by, sha256) VALUES (%s, %s, %s, 'servicenow', 'cab',"
            " 'cab.v1:0', 'SELECT ...', '{}', '{\"changes_with_cab\": 12}', now(), 'test',"
            " repeat('a', 64)) RETURNING id", (tenant, row[0], code)).fetchone()
    assert response
    assert evidence
    return response[0], evidence[0]


def _state(step17: Step17, table: str, rid: UUID, cols: str) -> tuple[Any, ...]:
    with psycopg.connect(step17.seeded.admin_dsn) as cx:
        row = cx.execute(f"SELECT {cols} FROM cloudops.{table} WHERE id = %s", (rid,)).fetchone()
    assert row is not None
    return tuple(row)


async def test_a_control_draft_cites_what_it_was_given_and_never_scores(
    step17: Step17, drafter: Any
) -> None:
    acts, provider = drafter
    s = step17.seeded
    response, evidence = _control_response(step17, s.a.id, "TEST-DRF-01")
    provider.queue("draft_control_response", _reply({
        "narrative": "Twelve changes went through the CAB this period.",
        "citations": [f"evidence:{evidence}"], "sufficiency": "partial",
        "gaps": ["the policy document itself"], "confidence": 0.8}))
    out = await acts.draft_control(drafting.DraftItem("control_response", str(response),
                                                      str(s.a.id)))
    assert out.state == "drafted"
    state, narrative, citations, sufficiency, result, approved, human = _state(
        step17, "control_response", response,
        "draft_state, draft_narrative, draft_citations, draft_sufficiency, result,"
        " approved_at, narrative")
    assert (state, sufficiency) == ("drafted", "partial")
    assert narrative.startswith("Twelve changes")
    assert citations[0]["ref"] == f"evidence:{evidence}"
    assert citations[0]["query_name"] == "cab"
    assert (result, approved, human) == (None, None, None)  # a draft is only ever a draft
    # The model saw this tenant's control and evidence, and nothing of another's.
    sent = "\n".join(m.content for _, msgs in provider.sent for m in msgs)
    assert str(evidence) in sent
    assert "bravo" not in sent.lower()
    # Claimed once: a second run finds nothing requested.
    again = await acts.draft_control(drafting.DraftItem("control_response", str(response),
                                                        str(s.a.id)))
    assert again.state == "skipped"


async def test_a_draft_citing_what_it_was_not_given_is_declined(
    step17: Step17, drafter: Any
) -> None:
    acts, provider = drafter
    s = step17.seeded
    response, _ = _control_response(step17, s.a.id, "TEST-DRF-02")
    _, foreign = _control_response(step17, s.b.id, "TEST-DRF-02")
    provider.queue("draft_control_response", _reply({
        "narrative": "Fully compliant, as the attached evidence shows.",
        "citations": [f"evidence:{foreign}"], "sufficiency": "sufficient", "confidence": 0.95}))
    out = await acts.draft_control(drafting.DraftItem("control_response", str(response),
                                                      str(s.a.id)))
    assert (out.state, out.reason) == ("declined", "ungrounded_citations")
    state, narrative = _state(step17, "control_response", response,
                              "draft_state, draft_narrative")
    assert (state, narrative) == ("declined", None)


async def test_the_queue_names_the_tenant_and_scope_holds_it(step17: Step17,
                                                             drafter: Any) -> None:
    """The queue lists requests across tenants, but a draft claimed under the
    wrong tenant's scope finds nothing: the row is invisible to it."""
    acts, _ = drafter
    s = step17.seeded
    response, _ = _control_response(step17, s.b.id, "TEST-DRF-03")
    pending = {(i.id, i.tenant_id) for i in await acts.pending_drafts(500)}
    assert (str(response), str(s.b.id)) in pending
    wrong = await acts.draft_control(drafting.DraftItem("control_response", str(response),
                                                        str(s.a.id)))
    assert wrong.state == "skipped"
    assert _state(step17, "control_response", response, "draft_state") == ("requested",)
    with psycopg.connect(step17.seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.control_response SET draft_state = 'none' WHERE id = %s",
                   (response,))


def _narrative(step17: Step17, tenant: UUID) -> UUID:
    sections = {
        "volumetrics": {
            "key": "volumetrics", "title": "Volumetrics", "captured": True, "gap": None,
            "figures": [{"key": "incidents_opened", "label": "Incidents opened", "value": "42",
                         "kind": "count", "unit": "incidents", "source_system": "servicenow",
                         "query_ref": "q", "note": None},
                        {"key": "incident_sla_attainment", "label": "Within SLA",
                         "value": "91.6666667", "kind": "percent", "unit": "percent",
                         "source_system": "servicenow", "query_ref": "q", "note": None}],
            "tables": []},
    }
    with psycopg.connect(step17.seeded.admin_dsn, autocommit=True) as cx:
        pack = cx.execute(
            "INSERT INTO cloudops.report_pack (tenant_id, period, version, contract_currency,"
            " sections, data_as_of, generated_by) SELECT %s, '2026-01-01',"
            " coalesce(max(version), 0) + 1, 'EUR', %s, now(), %s FROM cloudops.report_pack"
            " WHERE tenant_id = %s AND period = '2026-01-01' RETURNING id",
            (tenant, json.dumps(sections), uuid4(), tenant)).fetchone()
        assert pack
        row = cx.execute("INSERT INTO cloudops.report_narrative (tenant_id, pack_id, section)"
                         " VALUES (%s, %s, 'volumetrics') RETURNING id",
                         (tenant, pack[0])).fetchone()
    assert row
    return UUID(str(row[0]))


@pytest.mark.parametrize(("text", "state"), [
    ("42 incidents were opened, and 91.67% were resolved within SLA.", "drafted"),
    ("42 incidents were opened, 8 fewer than last month.", "declined"),  # 8 is computed
])
async def test_a_narrative_uses_only_numbers_in_its_data(
    step17: Step17, drafter: Any, text: str, state: str
) -> None:
    acts, provider = drafter
    narrative = _narrative(step17, step17.seeded.a.id)
    provider.queue("draft_report_narrative", _reply({
        "text": text, "figures_cited": ["incidents_opened"], "confidence": 0.9}))
    out = await acts.draft_narrative(drafting.DraftItem("report_narrative", str(narrative),
                                                        str(step17.seeded.a.id)))
    assert out.state == state
    draft_state, draft_text, human = _state(step17, "report_narrative", narrative,
                                            "draft_state, draft_text, text")
    assert draft_state == state
    assert (draft_text == text) is (state == "drafted")
    assert human is None  # a draft is never a person's text


async def test_the_sweep_drafts_everything_requested(step17: Step17, drafter: Any) -> None:
    acts, provider = drafter
    narrative = _narrative(step17, step17.seeded.c.id)
    provider.queue("draft_report_narrative", _reply({
        "text": "42 incidents were opened.", "figures_cited": ["incidents_opened"],
        "confidence": 0.9}))
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=drafting.TASK_QUEUE, workflows=drafting.WORKFLOWS,
               activities=drafting.activities(acts)),
    ):
        outcomes = await env.client.execute_workflow(
            drafting.DraftSweepWorkflow.run, 50, id=f"sweep-{uuid4().hex[:8]}",
            task_queue=drafting.TASK_QUEUE)
        indexed = await env.client.execute_workflow(
            drafting.KnowledgeIndexWorkflow.run, id=f"index-{uuid4().hex[:8]}",
            task_queue=drafting.TASK_QUEUE)
    assert any(o.id == str(narrative) and o.state == "drafted" for o in outcomes)
    assert set(indexed) == {str(t.id) for t in step17.seeded.tenants}
    assert _state(step17, "report_narrative", narrative, "draft_state") == ("drafted",)
    assert datetime.now(UTC)  # the sweep ran to completion within the test server
