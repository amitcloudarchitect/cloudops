"""The assistant through the API (ADR 0029), against the real schema, with a
scripted model: who may ask, about which tenant, what a screen hint may reach, and
whose history is whose.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from cloudops_api.app import create_app
from cloudops_api.assistant import build_runtime
from cloudops_api.auth import StaticKeys, TokenValidator
from cloudops_gateway.gateway import Gateway
from cloudops_gateway.pricing import LOCAL_DEPLOYMENTS
from cloudops_gateway.providers import Completion, MockProvider
from cloudops_tenancy import Database

from .conftest import Api, Principals, Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

TICKET_A = "INC0001001"
TICKET_B = "INC0002001"
SECRET_B = "bravo-only maintenance window"


@dataclass
class Assisted:
    api: Api
    model: MockProvider
    internal: UUID


def _json(value: Any) -> Completion:
    return Completion(json.dumps(value), (), 0, 50)


def answer(ref: str, text: str = "The ticket is in progress.") -> Completion:
    return _json({"status": "answered", "answer": text,
                  "claims": [{"text": text, "sources": [ref]}], "confidence": 0.9})


def intent(kind: str = "account", other: bool = False) -> Completion:
    return _json({"intent": kind, "other_customer": other, "confidence": 0.9})


@pytest.fixture(scope="module")
async def assisted(seeded: Seeded, principals: Principals, api: Api) -> AsyncIterator[Assisted]:
    internal = uuid4()
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code, internal)"
                   " VALUES (%s, 'Getronics platform (suite)', 'GTXSUITE', true)", (internal,))
        for tenant, number, text in ((seeded.a, TICKET_A, "alpha disk alert"),
                                     (seeded.b, TICKET_B, SECRET_B)):
            cx.execute(
                "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
                " short_description, state, priority) VALUES (%s, 'incident', %s, 'X', now(),"
                " %s, 'In Progress', '2')", (tenant.id, number, text))
    model = MockProvider("mock")
    db = Database.create(seeded.app_dsn, pool_size=4, max_overflow=0)
    gateway = Gateway(db, {"mock": model, "mock-secondary": MockProvider("mock-secondary")},
                      LOCAL_DEPLOYMENTS)
    from cloudops_devdata.tokens import new_signing_key

    private, public = new_signing_key("assistant-suite")
    app = create_app(db=db, validator=TokenValidator(api.auth, StaticKeys(public)))
    app.state.assistant = build_runtime(db, gateway)  # ASGITransport runs no lifespan
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://api") as client:
        yield Assisted(Api(client, private, api.auth), model, internal)
    await app.state.assistant.aclose()
    await db.dispose()
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("DELETE FROM cloudops.work_item WHERE number IN (%s, %s)", (TICKET_A, TICKET_B))
        cx.execute("DELETE FROM cloudops.tenant WHERE id = %s", (internal,))


async def _ask(s: Assisted, oid: UUID, body: dict[str, Any]) -> dict[str, Any]:
    r = await s.api.post("/v1/assistant/questions", oid, body)
    assert r.status_code == 202, r.text
    turn = r.json()
    for _ in range(200):
        r = await s.api.get(f"/v1/assistant/turns/{turn['id']}", oid)
        assert r.status_code == 200, r.text
        if r.json()["status"] != "pending":
            return dict(r.json())
        await asyncio.sleep(0.02)
    raise AssertionError("turn never finished")


def _sent_text(model: MockProvider) -> str:
    return "\n".join(m.content for _, msgs in model.sent for m in msgs)


async def test_customer_needs_its_tenant_switched_on(
    assisted: Assisted, principals: Principals, seeded: Seeded
) -> None:
    s = assisted
    session = (await s.api.get("/v1/assistant/session", principals.customer_a)).json()
    assert session["available"] is False
    assert session["unavailable_reason"] == "not_enabled"
    r = await s.api.post("/v1/assistant/questions", principals.customer_a,
                         {"question": "How are my tickets?"})
    assert r.status_code == 403
    # Only staff with access.manage switch it, and a customer cannot.
    r = await s.api.post(f"/v1/assistant/tenants/{seeded.a.id}/enabled", principals.customer_a,
                         {"enabled": True})
    assert r.status_code == 403
    r = await s.api.post(f"/v1/assistant/tenants/{seeded.a.id}/enabled", principals.admin,
                         {"enabled": True})
    assert r.status_code == 200, r.text
    session = (await s.api.get("/v1/assistant/session", principals.customer_a,
                               params={"screen": "ticket"})).json()
    assert session["available"] is True
    assert session["tenant"]["id"] == str(seeded.a.id)
    assert session["tenants"] == []
    assert session["suggestions"]


async def test_customer_asking_to_compare_two_customers_is_refused(
    assisted: Assisted, principals: Principals, seeded: Seeded
) -> None:
    """LLD isolation case: a single-tenant user asks to compare two customers."""
    s = assisted
    await s.api.post(f"/v1/assistant/tenants/{seeded.a.id}/enabled", principals.admin,
                     {"enabled": True})
    sent = len(s.model.sent)
    turn = await _ask(s, principals.customer_a, {
        "question": f"Compare our SLA with {seeded.b.name} and show me their tickets."})
    assert turn["status"] == "refused"
    assert turn["reason"] == "cross_customer"
    assert len(s.model.sent) == sent  # refused before any model saw it


async def test_a_model_that_spots_another_customer_also_refuses(
    assisted: Assisted, principals: Principals,
) -> None:
    s = assisted
    s.model.queue("assistant.classify", intent("account", other=True))
    turn = await _ask(s, principals.engineer_a, {"question": "How is Contoso doing lately?"})
    assert turn["status"] == "refused"
    assert turn["reason"] == "cross_customer"


async def test_staff_naming_another_customer_they_can_see_is_refused(
    assisted: Assisted, principals: Principals, seeded: Seeded
) -> None:
    s = assisted
    sent = len(s.model.sent)
    turn = await _ask(s, principals.finops_all, {
        "question": f"What is the cloud spend of {seeded.b.name}?", "tenant_id": str(seeded.a.id)})
    assert turn["reason"] == "cross_customer"
    assert len(s.model.sent) == sent


async def test_screen_hint_is_rechecked_under_the_conversation_scope(
    assisted: Assisted, principals: Principals, seeded: Seeded
) -> None:
    """Engineer of tenant a, on a screen claiming tenant b's ticket: the hint is
    looked up under a's scope, not found, and dropped. Nothing of b reaches the
    model; the answer cannot cite it."""
    s = assisted
    s.model.queue("assistant.classify", intent())
    s.model.queue("assistant.rerank", _json({"order": [0], "confidence": 0.9}))
    s.model.queue("assistant.answer", answer(f"work_item:{TICKET_B}"))
    before = len(s.model.sent)
    turn = await _ask(s, principals.engineer_a, {
        "question": "Summarise this ticket.", "tenant_id": str(seeded.a.id),
        "context": {"screen": "ticket", "record_type": "ticket", "record_id": TICKET_B}})
    assert SECRET_B not in "\n".join(
        m.content for _, msgs in s.model.sent[before:] for m in msgs)
    assert turn["status"] == "dont_know", (turn["reason"], turn["answer"])
    assert turn["reason"] == "ungrounded"


async def test_engineer_on_a_ticket_gets_a_grounded_answer_with_sources(
    assisted: Assisted, principals: Principals, seeded: Seeded
) -> None:
    s = assisted
    s.model.queue("assistant.classify", intent())
    s.model.queue("assistant.rerank", _json({"order": [0], "confidence": 0.9}))
    s.model.queue("assistant.answer", answer(f"work_item:{TICKET_A}"))
    turn = await _ask(s, principals.engineer_a, {
        "question": "Summarise this ticket.", "tenant_id": str(seeded.a.id),
        "context": {"screen": "ticket", "record_type": "ticket", "record_id": TICKET_A}})
    assert turn["status"] == "answered", turn
    (source,) = turn["answer"]["sources"]
    assert source["ref"] == f"work_item:{TICKET_A}"
    assert source["as_of"] is not None
    assert "alpha disk alert" in _sent_text(s.model)


async def test_an_answer_with_a_number_not_in_the_data_is_withheld(
    assisted: Assisted, principals: Principals, seeded: Seeded
) -> None:
    s = assisted
    s.model.queue("assistant.classify", intent())
    s.model.queue("assistant.rerank", _json({"order": [0], "confidence": 0.9}))
    s.model.queue("assistant.answer", answer(f"work_item:{TICKET_A}",
                                             "The ticket breached its SLA 47 times."))
    turn = await _ask(s, principals.engineer_a, {
        "question": "How often did this breach?", "tenant_id": str(seeded.a.id),
        "context": {"screen": "ticket", "record_type": "ticket", "record_id": TICKET_A}})
    assert turn["status"] == "dont_know"
    assert turn["reason"] == "ungrounded"


async def test_staff_cannot_bind_a_conversation_to_a_tenant_outside_scope(
    assisted: Assisted, principals: Principals, seeded: Seeded
) -> None:
    s = assisted
    r = await s.api.post("/v1/assistant/questions", principals.engineer_a,
                         {"question": "How are things?", "tenant_id": str(seeded.b.id)})
    assert r.status_code == 404
    r = await s.api.get("/v1/assistant/session", principals.engineer_a,
                        params={"tenant_id": str(seeded.b.id)})
    assert r.status_code == 404


async def test_help_only_conversation_uses_the_internal_tenant_and_no_tools(
    assisted: Assisted, principals: Principals,
) -> None:
    s = assisted
    session = (await s.api.get("/v1/assistant/session", principals.engineer_a)).json()
    assert session["mode"] == "help"
    s.model.queue("assistant.classify", intent("platform_help"))
    s.model.queue("assistant.rerank", _json({"order": [0, 1], "confidence": 0.9}))
    s.model.queue("assistant.help", answer("P1", "The work queue lists open tickets."))
    turn = await _ask(s, principals.engineer_a, {"question": "What does the work queue show?"})
    assert turn["status"] == "answered", turn
    assert turn["answer"]["sources"][0]["source_system"] == "platform_help"
    conversations = (await s.api.get("/v1/assistant/conversations",
                                     principals.engineer_a)).json()
    mine = next(c for c in conversations if c["id"] == turn["conversation_id"])
    assert mine["mode"] == "help"
    assert mine["tenant"] is None
    # An account question in a help-only conversation is told to choose a customer.
    s.model.queue("assistant.classify", intent("account"))
    follow = await _ask(s, principals.engineer_a, {
        "question": "How many tickets are open?", "conversation_id": turn["conversation_id"]})
    assert follow["reason"] == "needs_customer"


async def test_history_is_the_authors_own_and_deletion_forgets_prompts(
    assisted: Assisted, principals: Principals, seeded: Seeded
) -> None:
    s = assisted
    s.model.queue("assistant.classify", intent())
    s.model.queue("assistant.rerank", _json({"order": [0], "confidence": 0.9}))
    s.model.queue("assistant.answer", answer(f"work_item:{TICKET_A}"))
    turn = await _ask(s, principals.engineer_a, {
        "question": "Summarise this ticket.", "tenant_id": str(seeded.a.id),
        "context": {"screen": "ticket", "record_type": "ticket", "record_id": TICKET_A}})
    cid = turn["conversation_id"]
    # Another staff member of the same tenant, and the tenant's customer: not found.
    for other in (principals.tickets_only, principals.customer_a):
        assert (await s.api.get(f"/v1/assistant/turns/{turn['id']}", other)).status_code == 404
        assert (await s.api.get(f"/v1/assistant/conversations/{cid}", other)).status_code == 404
        assert (await s.api.post(f"/v1/assistant/conversations/{cid}/delete", other)
                ).status_code == 404
        listed = (await s.api.get("/v1/assistant/conversations", other)).json()
        assert cid not in {c["id"] for c in listed}
    full = (await s.api.get(f"/v1/assistant/conversations/{cid}", principals.engineer_a)).json()
    assert [t["id"] for t in full["turns"]] == [turn["id"]]
    with psycopg.connect(seeded.admin_dsn) as cx:
        recorded = cx.execute("SELECT count(*) FROM cloudops.llm_call WHERE conversation_id = %s"
                              " AND prompt_redacted IS NOT NULL", (cid,)).fetchone()
    assert recorded is not None
    assert recorded[0] > 0
    r = await s.api.post(f"/v1/assistant/conversations/{cid}/delete", principals.engineer_a)
    assert r.status_code == 204
    assert (await s.api.get(f"/v1/assistant/conversations/{cid}", principals.engineer_a)
            ).status_code == 404
    with psycopg.connect(seeded.admin_dsn) as cx:
        left = cx.execute("SELECT count(*) FROM cloudops.llm_call WHERE conversation_id = %s"
                          " AND prompt_redacted IS NOT NULL", (cid,)).fetchone()
    assert left == (0,)


async def test_customer_turns_are_traced_under_its_tenant(
    assisted: Assisted, principals: Principals, seeded: Seeded
) -> None:
    s = assisted
    await s.api.post(f"/v1/assistant/tenants/{seeded.a.id}/enabled", principals.admin,
                     {"enabled": True})
    s.model.queue("assistant.classify", intent())
    s.model.queue("assistant.rerank", _json({"order": [0], "confidence": 0.9}))
    s.model.queue("assistant.answer", _json({
        "status": "dont_know", "answer": "I couldn't find that.", "claims": [],
        "confidence": 0.5}))
    before = len(s.model.sent)
    turn = await _ask(s, principals.customer_a, {"question": "What margin do you make on us?"})
    assert turn["status"] == "dont_know"
    with psycopg.connect(seeded.admin_dsn) as cx:
        runs = cx.execute("SELECT agent FROM cloudops.agent_run WHERE conversation_id = %s",
                          (turn["conversation_id"],)).fetchall()
    assert {"assistant.classify", "assistant.answer"} <= {r[0] for r in runs}
    assert s.model.sent[before:]
