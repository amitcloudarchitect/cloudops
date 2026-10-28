"""Agents against the real schema: the classifier through the real gateway,
and the agent_run trace under RLS (step 07; migration 0007)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from uuid import uuid4

import psycopg
import pytest

from cloudops_agents.base import Agent
from cloudops_agents.classifier import CLASSIFIER, ticket_data
from cloudops_agents.trace import DbTraceSink
from cloudops_gateway.gateway import CallContext, Gateway
from cloudops_gateway.pricing import LOCAL_DEPLOYMENTS
from cloudops_gateway.providers import MockProvider
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Seeded

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def app(seeded: Seeded) -> AsyncIterator[Database]:
    db = Database.create(seeded.app_dsn, pool_size=2, max_overflow=0)
    yield db
    await db.dispose()


async def test_the_classifier_runs_through_the_gateway_and_is_traced(
    app: Database, seeded: Seeded
) -> None:
    provider = MockProvider(name="mock")
    secondary = MockProvider(name="mock-secondary")
    gateway = Gateway(app, {"mock": provider, "mock-secondary": secondary}, LOCAL_DEPLOYMENTS)
    principal = f"user:{uuid4()}"
    ctx = CallContext.for_tenant(TenantScope([seeded.a.id], Role.ENGINEER, principal),
                                 seeded.a.id, ["tickets.read"])
    result = await Agent(CLASSIFIER, gateway, None, DbTraceSink(app)).run(
        ctx, request="Classify this ticket.",
        data=[ticket_data({"number": "INC-alpha",
                           "short_description": "Service down on vm-alpha-01"})],
    )
    assert result.status == "answered"
    assert result.output is not None
    (_, sent), = provider.sent
    assert "Service down on vm-alpha-01" in sent[1].content
    assert str(seeded.a.id) not in sent[0].content + sent[1].content

    with psycopg.connect(seeded.admin_dsn) as cx:
        run = cx.execute(
            "SELECT tenant_id, agent, prompt_version, prompt_hash, tier, status, attempts,"
            " llm_call_ids, cost, cost_currency, output->>'category' FROM cloudops.agent_run"
            " WHERE principal = %s", (principal,)).fetchone()
        calls = cx.execute(
            "SELECT id, tenant_id, task FROM cloudops.llm_call WHERE principal = %s",
            (principal,)).fetchall()
    assert run is not None
    assert run[:7] == (seeded.a.id, "classify_ticket", CLASSIFIER.prompt.version,
                       hashlib.sha256(result.prompt.encode()).hexdigest(), "small", "answered", 1)
    assert run[7] == [c[0] for c in calls], "the run links to the gateway calls it was made of"
    assert {c[1] for c in calls} == {seeded.a.id}
    assert (run[9], run[10]) == ("USD", "compute.vm")


def test_agent_runs_are_tenant_isolated(seeded: Seeded) -> None:
    from .support import raw_scope

    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO cloudops.agent_run (id, tenant_id, principal, agent, prompt_version,"
            " prompt_hash, tier, status, attempts, cost_currency) VALUES (%s, %s, 'user:x',"
            " 'classify_ticket', 'v1@x', %s, 'small', 'answered', 1, 'USD')",
            (uuid4(), seeded.b.id, "0" * 64),
        )
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "platform_admin") as cx:
        rows = cx.execute("SELECT DISTINCT tenant_id FROM cloudops.agent_run").fetchall()
    assert seeded.b.id not in {r[0] for r in rows}
