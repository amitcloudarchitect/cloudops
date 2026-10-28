"""The AI gateway against the real schema (step 06; ADR 0005): redaction before
any provider sees text, fallback, quotas, breaker, semantic cache isolation,
tracing, the tools under scope, the bounded loop, and retrieval."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from cloudops_gateway.gateway import (
    AllProvidersFailedError,
    CallContext,
    CircuitOpenError,
    ConversationTooLongError,
    Gateway,
    Limits,
    QuotaExceededError,
)
from cloudops_gateway.loop import run_tool_loop
from cloudops_gateway.pricing import LOCAL_DEPLOYMENTS
from cloudops_gateway.providers import (
    Completion,
    Message,
    MockProvider,
    Tier,
    ToolCallRequest,
)
from cloudops_gateway.registry import ToolRegistry
from cloudops_gateway.retrieval import KnowledgeIndex
from cloudops_gateway.tools import TOOLS
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Seeded, Tenant

pytestmark = [pytest.mark.integration]

ALL_READ = ("inventory.read", "tickets.read", "cost.read", "vulnerabilities.read",
            "knowledge.read")


class Providers:
    def __init__(self) -> None:
        self.primary = MockProvider(name="mock")
        self.secondary = MockProvider(name="mock-secondary")

    def mapping(self) -> dict[str, MockProvider]:
        return {"mock": self.primary, "mock-secondary": self.secondary}


@pytest.fixture
def providers() -> Providers:
    return Providers()


def _gateway(db: Database, providers: Providers, limits: Limits | None = None) -> Gateway:
    return Gateway(db, providers.mapping(), LOCAL_DEPLOYMENTS, limits)


@pytest.fixture
async def app(seeded: Seeded) -> AsyncIterator[Database]:
    db = Database.create(seeded.app_dsn, pool_size=2, max_overflow=0)
    yield db
    await db.dispose()


def ctx(t: Tenant, *, role: Role = Role.ENGINEER, perms: tuple[str, ...] = ALL_READ,
        principal: str | None = None, names: tuple[str, ...] = ()) -> CallContext:
    resolved = TenantScope([t.id], role, principal or f"user:{uuid4()}")
    return CallContext.for_tenant(resolved, t.id, perms, personal_terms=names)


def _calls(seeded: Seeded, principal: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.admin_dsn) as cx:
        return list(cx.execute(
            "SELECT outcome, tier_requested, tier_used, provider, prompt_redacted, redactions,"
            " tokens_in, tokens_out, cost, cost_currency, cache_hit, reason, tool_calls"
            " FROM cloudops.llm_call WHERE principal = %s ORDER BY id", (principal,)
        ).fetchall())


# --- redaction and tracing --------------------------------------------------------------


async def test_nothing_unredacted_ever_reaches_a_provider(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    c = ctx(seeded.a, names=("Asha Patel",))
    secret = "AccountKey=abc123def456ghi789=="
    await _gateway(app, providers).complete(c, tier=Tier.SMALL, task="classify_ticket", messages=[
        Message("user", f"Asha Patel (asha@contoso.example) says {secret} fails on vm-alpha-01"),
    ])
    (_deployment, sent), = providers.primary.sent
    body = " ".join(m.content for m in sent)
    for leaked in ("Asha Patel", "asha@contoso.example", "abc123def456ghi789"):
        assert leaked not in body
    assert "vm-alpha-01" in body, "operational identifiers are kept"
    (row,) = _calls(seeded, c.principal_id)
    assert row[0] == "succeeded"
    assert "abc123def456ghi789" not in row[4]
    assert row[5] >= 3
    assert (row[6] > 0, row[7] > 0, row[9]) == (True, True, "USD")
    assert isinstance(row[8], Decimal)
    assert row[8] > 0


# --- fallback --------------------------------------------------------------------------


async def test_throttling_falls_back_to_the_secondary_provider(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    providers.primary.throttled.add("cloudops-small")
    c = ctx(seeded.a)
    reply = await _gateway(app, providers).complete(
        c, tier=Tier.SMALL, task="classify_ticket", messages=[Message("user", "disk full")])
    assert (reply.provider, reply.tier_used) == ("mock-secondary", Tier.SMALL)


async def test_large_unavailable_falls_back_to_small(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    providers.primary.down.add("cloudops-large")
    providers.secondary.down.add("cloudops-large")
    c = ctx(seeded.a)
    reply = await _gateway(app, providers).complete(
        c, tier=Tier.LARGE, task="draft_work_note", messages=[Message("user", "summarise")])
    assert (reply.tier_used, reply.deployment) == (Tier.SMALL, "cloudops-small")
    assert _calls(seeded, c.principal_id)[0][1:3] == ("large", "small")


async def test_everything_down_fails_and_is_traced(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    for p in (providers.primary, providers.secondary):
        p.down.update({"cloudops-small", "cloudops-large"})
    c = ctx(seeded.a)
    with pytest.raises(AllProvidersFailedError):
        await _gateway(app, providers).complete(
            c, tier=Tier.LARGE, task="x", messages=[Message("user", "hi")])
    assert _calls(seeded, c.principal_id)[0][0] == "failed"


# --- quotas, breaker, turns --------------------------------------------------------------


async def test_user_quota_warns_at_80_percent_then_refuses_before_any_provider_call(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    """Quotas are checked against usage before a call: one call may overshoot,
    the next is refused, and no provider sees it."""
    gw = _gateway(app, providers, Limits(user_daily_tokens=120))
    c = ctx(seeded.a)
    first = await gw.complete(c, tier=Tier.SMALL, task="classify_ticket",
                              messages=[Message("user", "x" * 300)])
    assert first.budget_warning is False
    second = await gw.complete(c, tier=Tier.SMALL, task="classify_ticket",
                               messages=[Message("user", "y")])
    assert second.budget_warning is True, "past 80% of the daily quota"
    sent = len(providers.primary.sent)
    with pytest.raises(QuotaExceededError):
        await gw.complete(c, tier=Tier.SMALL, task="classify_ticket",
                          messages=[Message("user", "again")])
    assert len(providers.primary.sent) == sent
    assert _calls(seeded, c.principal_id)[-1][0] == "refused"


async def test_tenant_quota_applies_across_users(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    # Usage by one user of the tenant, under normal limits (whatever ran before).
    await _gateway(app, providers).complete(ctx(seeded.b), tier=Tier.SMALL,
                                            task="classify_ticket",
                                            messages=[Message("user", "hello")])
    gw = _gateway(app, providers, Limits(tenant_monthly_tokens=1))
    with pytest.raises(QuotaExceededError):  # a different user, the same tenant
        await gw.complete(ctx(seeded.b), tier=Tier.SMALL, task="classify_ticket",
                          messages=[Message("user", "hello")])


async def test_spend_breaker_opens_on_a_spike(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    """A burst 20 minutes ago, judged with a one-hour 'recent' window so it does
    not trip the default 15-minute breaker for every other test."""
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO cloudops.llm_call (tenant_id, principal, task, tier_requested, cost,"
            " cost_currency, outcome, created_at) VALUES (%s, 'agent:loop', 'x', 'large',"
            " 900.000000, 'USD', 'succeeded', now() - interval '20 minutes')",
            (seeded.c.id,),
        )
    gw = _gateway(app, providers, Limits(breaker_recent=timedelta(hours=1)))
    with pytest.raises(CircuitOpenError):
        await gw.complete(ctx(seeded.c), tier=Tier.SMALL, task="classify_ticket",
                          messages=[Message("user", "hi")])


async def test_conversation_turn_ceiling(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    with pytest.raises(ConversationTooLongError):
        await _gateway(app, providers).complete(
            ctx(seeded.a), tier=Tier.SMALL, task="classify_ticket",
            messages=[Message("user", "hi")], turn=21)


# --- semantic cache --------------------------------------------------------------------


async def test_cache_serves_only_the_same_tenant_role_and_scope(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    gw = _gateway(app, providers)
    question = [Message("user", f"what is the backup policy {uuid4()}")]
    principal = f"user:{uuid4()}"
    first = await gw.complete(ctx(seeded.a, principal=principal), tier=Tier.SMALL,
                              task="classify_ticket", messages=question, cache="static")
    again = await gw.complete(ctx(seeded.a, principal=principal), tier=Tier.SMALL,
                              task="classify_ticket", messages=question, cache="static")
    assert (first.cache_hit, again.cache_hit) == (False, True)
    other_tenant = await gw.complete(ctx(seeded.b), tier=Tier.SMALL, task="classify_ticket",
                                     messages=question, cache="static")
    other_role = await gw.complete(ctx(seeded.a, role=Role.CFO), tier=Tier.SMALL,
                                   task="classify_ticket", messages=question, cache="static")
    assert other_tenant.cache_hit is False, "never across tenants"
    assert other_role.cache_hit is False, "never across roles"


# --- tools under scope ---------------------------------------------------------------------


def _registry(db: Database) -> ToolRegistry:
    reg = ToolRegistry(db)
    for tool in TOOLS:
        reg.register(tool)
    return reg


async def test_tools_see_only_the_conversation_tenant(app: Database, seeded: Seeded) -> None:
    reg = _registry(app)
    c = ctx(seeded.a)

    async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        out = await reg.invoke(ToolCallRequest("1", name, json.dumps(args)), c)
        assert out.ok, out.refusal
        result: dict[str, Any] = json.loads(out.content)["result"]
        return result

    own = await call("get_resource_context", {"resource": str(seeded.a.resource_id)})
    assert own["found"] is True
    assert own["name"] == "vm-alpha-01"
    assert [t["number"] for t in own["recent_tickets"]] == ["INC-alpha"]
    foreign = await call("get_resource_context", {"resource": str(seeded.b.resource_id)})
    assert foreign == {"found": False, "matches": 0}
    by_name = await call("get_resource_context", {"resource": "vm-bravo-01"})
    assert by_name["found"] is False
    cost = await call("get_cost_trend", {"days": 7})
    assert cost["currencies"] == [seeded.a.currency]
    assert all(isinstance(p["amortized"], str) for p in cost["series"])


async def test_a_model_cannot_pass_scope_as_an_argument(app: Database, seeded: Seeded) -> None:
    out = await _registry(app).invoke(
        ToolCallRequest(
            "1", "get_cost_trend", json.dumps({"days": 7, "tenant_id": str(seeded.b.id)})
        ),
        ctx(seeded.a),
    )
    assert out.refusal == "invalid_arguments"


async def test_sections_need_their_own_permission(app: Database, seeded: Seeded) -> None:
    out = await _registry(app).invoke(
        ToolCallRequest("1", "get_resource_context",
                        json.dumps({"resource": str(seeded.a.resource_id)})),
        ctx(seeded.a, perms=("inventory.read",)),
    )
    result = json.loads(out.content)["result"]
    assert result["found"] is True
    assert "recent_tickets" not in result
    assert "open_vulnerabilities" not in result


# --- the bounded loop --------------------------------------------------------------------------


async def test_the_loop_answers_after_a_tool_call(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    providers.primary.queue(
        "answer",
        Completion("", (ToolCallRequest("t1", "get_cost_trend", '{"days": 7}'),), 0, 5),
        Completion("Spend is flat this week.", (), 0, 6),
    )
    c = ctx(seeded.a)
    result = await run_tool_loop(_gateway(app, providers), _registry(app), c, tier=Tier.LARGE,
                                 task="answer", messages=[Message("user", "how is cost?")])
    assert (result.status, result.iterations) == ("answered", 1)
    assert result.text == "Spend is flat this week."
    assert result.tool_calls[0]["name"] == "get_cost_trend"
    tool_message = providers.primary.sent[-1][1][-1]
    assert tool_message.role == "tool"
    assert seeded.a.currency in tool_message.content


async def test_the_loop_escalates_after_ten_tool_iterations(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    looping = Completion("", (ToolCallRequest("t", "search_tickets", '{"query": "disk"}'),), 0, 3)
    providers.primary.queue("answer", *[looping] * 50)
    c = ctx(seeded.a)
    result = await run_tool_loop(_gateway(app, providers), _registry(app), c, tier=Tier.LARGE,
                                 task="answer", messages=[Message("user", "keep going")])
    assert result.status == "escalated"
    assert len(result.tool_calls) == 10
    assert len(providers.primary.sent) == 11, "ten rounds of tools, one final refusal to continue"
    last = _calls(seeded, c.principal_id)[-1]
    assert (last[0], last[11]) == ("escalated", "tool iteration ceiling (10) reached")


# --- retrieval -----------------------------------------------------------------------------------


async def test_hybrid_retrieval_finds_by_meaning_and_by_word(
    app: Database, providers: Providers, seeded: Seeded
) -> None:
    gw = _gateway(app, providers)
    index = KnowledgeIndex(app, gw)
    c = ctx(seeded.a)
    note = "Restarted the print spooler on vm-alpha-01; queue drained in two minutes."
    await index.add(c, source_kind="hybrid_test", source_ref="INC-h1", title="Spooler",
                    text_body=note)
    await index.add(c, source_kind="hybrid_test", source_ref="INC-h2", title="Other",
                    text_body="Unrelated certificate renewal on the jump host.")
    by_vector = await index.search(c, note, kinds=["hybrid_test"])
    by_word = await index.search(c, "spooler", kinds=["hybrid_test"])
    assert by_vector[0].source_ref == "INC-h1"
    assert by_word[0].source_ref == "INC-h1"
    assert {h.tenant_id for h in by_vector} == {seeded.a.id}


def test_platform_spend_function_returns_totals_to_the_app_only(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        row = cx.execute(
            "SELECT pg_get_userbyid(p.proowner), p.prosecdef,"
            " has_function_privilege('public', p.oid, 'EXECUTE'),"
            " has_function_privilege('cloudops_ingest', p.oid, 'EXECUTE'),"
            " has_function_privilege('cloudops_app', p.oid, 'EXECUTE'),"
            " pg_get_function_result(p.oid)"
            " FROM pg_proc p WHERE p.proname = 'llm_platform_spend'"
        ).fetchone()
    assert row == ("cloudops_resolver", True, False, False, True,
                   "TABLE(recent_spend numeric, trailing_spend numeric)")

