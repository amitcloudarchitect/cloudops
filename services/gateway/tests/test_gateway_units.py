"""Gateway pieces that need no database: redaction, chunking, pricing, the
registry's rules, and scope narrowing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from cloudops_gateway.gateway import CallContext, ScopeError
from cloudops_gateway.pricing import LOCAL_DEPLOYMENTS, Deployment
from cloudops_gateway.providers import Tier, ToolCallRequest
from cloudops_gateway.redaction import redact
from cloudops_gateway.registry import (
    RegistrationError,
    Tool,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolResult,
)
from cloudops_gateway.retrieval import chunk_text
from cloudops_gateway.tools import TOOLS
from cloudops_tenancy import Role, TenantScope

# --- redaction -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("mail asha.patel@contoso-retail.co.uk now", "EMAIL"),
        ("call +44 20 7946 0958 please", "PHONE"),
        ("AccountKey=abc123def456ghi789== in DefaultEndpointsProtocol=https;AccountName=x;"
         "AccountKey=abc123def456ghi789==", "CONNECTION_STRING"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N", "JWT"),
        ("Authorization: Bearer abcdefghijklmnop1234567890", "BEARER_TOKEN"),
        ("key AKIAIOSFODNN7EXAMPLE", "AWS_ACCESS_KEY"),
        ("postgres://admin:s3cret@db.example:5432/x", "URL_CREDENTIALS"),
        ("password=Hunter2Hunter2", "SECRET"),
        ("client_secret: q8Z~abcDEF123", "SECRET"),
        ("card 4111 1111 1111 1111", "CARD_NUMBER"),
        ("IBAN GB82 WEST 1234 5698 7654 32 today", "IBAN"),
        ("NI AB123456C", "UK_NI_NUMBER"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "PRIVATE_KEY"),
    ],
)
def test_secrets_and_personal_data_are_redacted(text: str, kind: str) -> None:
    out = redact(text)
    assert out.kinds[kind] >= 1, out.text
    assert f"[REDACTED:{kind}]" in out.text


@pytest.mark.parametrize(
    "text",
    [
        "INC0040123 on 10.0.1.4 since 2026-10-27 08:14:22",  # operational, kept
        "card-like 1234 5678 9012 3456 fails Luhn",
        "GB00WEST12345698765432 fails mod-97",
        "the password policy changed",  # the word, not a value
        "cost 4111.25 EUR",
    ],
)
def test_operational_data_and_near_misses_are_left_alone(text: str) -> None:
    assert redact(text).text == text


def test_known_personal_names_are_redacted_longest_first() -> None:
    out = redact("Asha Patel escalated; Asha closed it. Ashanti is a host.", ["Asha", "Asha Patel"])
    assert out.text == (
        "[REDACTED:PERSON] escalated; [REDACTED:PERSON] closed it. Ashanti is a host."
    )
    assert out.kinds["PERSON"] == 2


# --- chunking and pricing -----------------------------------------------------------------


def test_chunks_respect_the_target_and_overlap() -> None:
    body = "\n\n".join(f"Paragraph {i}. " + "word " * 150 for i in range(12))
    chunks = chunk_text(body, target=1200, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 1200 + 100 + 1 for c in chunks)
    assert chunks[1].startswith(chunks[0][-100:].strip()[:20])
    assert chunk_text("") == []
    assert chunk_text("short") == ["short"]


def test_cost_is_decimal_at_six_places() -> None:
    d = Deployment("p", "a", Decimal("0.002500"), Decimal("0.010000"))
    assert d.cost(1000, 500) == Decimal("0.007500")
    assert d.cost(1, 1).as_tuple().exponent == -6
    assert all(len(LOCAL_DEPLOYMENTS[t]) >= 2 for t in Tier), "every tier has a fallback"


# --- scope narrowing ---------------------------------------------------------------------


def test_a_call_context_is_narrowed_to_one_tenant_from_the_resolved_scope() -> None:
    a, b = uuid4(), uuid4()
    resolved = TenantScope([a, b], Role.FINOPS, "user:x")
    ctx = CallContext.for_tenant(resolved, a, ["cost.read"])
    assert ctx.scope.tenant_ids == frozenset({a})
    assert ctx.scope.role is Role.FINOPS
    with pytest.raises(ScopeError):
        CallContext.for_tenant(resolved, uuid4(), [])


def test_cache_scope_differs_by_role_and_subscription_limit() -> None:
    t = uuid4()
    finops = CallContext.for_tenant(TenantScope([t], Role.FINOPS, "u"), t, [])
    engineer = CallContext.for_tenant(TenantScope([t], Role.ENGINEER, "u"), t, [])
    limited = CallContext.for_tenant(
        TenantScope([t], Role.FINOPS, "u", account_ids=[uuid4()]), t, [])
    assert len({finops.cache_scope_key, engineer.cache_scope_key, limited.cache_scope_key}) == 3


# --- registry rules (no handler ever runs on a refusal) -----------------------------------

CALLS: list[Mapping[str, Any]] = []


async def _record(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    CALLS.append(args)
    return ToolResult({"ok": True, "amount": Decimal("1.500000")})


async def _slow(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    await asyncio.sleep(5)
    return ToolResult({})


async def _broken(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    raise RuntimeError("SELECT * FROM secret_table WHERE password='x'")


SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"q": {"type": "string", "maxLength": 10}}, "required": ["q"]}


def _registry() -> ToolRegistry:
    reg = ToolRegistry(db=None)  # type: ignore[arg-type]  # handlers here never touch it
    reg.register(Tool("echo", "d", SCHEMA, "tickets.read", ToolKind.READ, _record))
    reg.register(Tool("slow", "d", SCHEMA, "tickets.read", ToolKind.READ, _slow,
                      timeout_seconds=0.05))
    reg.register(Tool("broken", "d", SCHEMA, "tickets.read", ToolKind.READ, _broken))
    reg.register(Tool("restart", "d", SCHEMA, "tickets.read", ToolKind.WRITE, _record,
                      idempotency_key=True))
    return reg


def _ctx(*permissions: str) -> CallContext:
    t = uuid4()
    return CallContext.for_tenant(TenantScope([t], Role.ENGINEER, "user:x"), t, permissions)


@pytest.mark.parametrize(
    ("name", "args", "perms", "code"),
    [
        ("nope", '{"q": "x"}', ("tickets.read",), "unknown_tool"),
        ("echo", '{"q": "x"}', ("cost.read",), "forbidden"),
        ("restart", '{"q": "x"}', ("tickets.read",), "write_requires_approval"),
        ("echo", "{not json", ("tickets.read",), "invalid_arguments"),
        ("echo", '{"q": "this is far too long"}', ("tickets.read",), "invalid_arguments"),
        ("echo", '{"q": "x", "tenant_id": "someone-else"}', ("tickets.read",), "invalid_arguments"),
        ("echo", '["q"]', ("tickets.read",), "invalid_arguments"),
        ("echo", "{}", ("tickets.read",), "invalid_arguments"),
    ],
)
async def test_refusals_happen_before_any_handler_runs(
    name: str, args: str, perms: tuple[str, ...], code: str
) -> None:
    CALLS.clear()
    outcome = await _registry().invoke(ToolCallRequest("c1", name, args), _ctx(*perms))
    assert outcome.refusal == code
    assert json.loads(outcome.content) == {"refused": code}
    assert CALLS == []


async def test_timeouts_and_failures_are_refusals_that_reveal_nothing() -> None:
    reg = _registry()
    slow = await reg.invoke(ToolCallRequest("c1", "slow", '{"q": "x"}'), _ctx("tickets.read"))
    assert slow.refusal == "timeout"
    broken = await reg.invoke(ToolCallRequest("c2", "broken", '{"q": "x"}'), _ctx("tickets.read"))
    assert broken.refusal == "tool_failed"
    assert "secret_table" not in broken.content
    assert "password" not in broken.content


async def test_a_valid_call_runs_and_money_stays_a_string() -> None:
    CALLS.clear()
    out = await _registry().invoke(ToolCallRequest("c1", "echo", '{"q": "x"}'),
                                   _ctx("tickets.read"))
    assert out.ok
    assert json.loads(out.content)["result"] == {"ok": True, "amount": "1.500000"}
    assert CALLS == [{"q": "x"}]


def test_models_are_offered_only_permitted_read_tools() -> None:
    names = {s.name for s in _registry().specs({"tickets.read"})}
    assert names == {"echo", "slow", "broken"}
    assert _registry().specs({"cost.read"}) == []


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "additionalProperties": False,
         "properties": {"tenant_id": {"type": "string"}}},
        {"type": "object", "additionalProperties": False,
         "properties": {"filter": {"type": "object", "additionalProperties": False,
                                   "properties": {"customer": {"type": "string"}}}}},
        {"type": "object", "properties": {"q": {"type": "string"}}},  # not closed
        {"type": "array"},
        {"type": "object", "additionalProperties": False, "properties": {"q": {"type": 7}}},
    ],
)
def test_registration_refuses_schemas_that_leak_scope_or_stay_open(
    schema: dict[str, Any],
) -> None:
    reg = ToolRegistry(db=None)  # type: ignore[arg-type]
    with pytest.raises(RegistrationError):
        reg.register(Tool("t", "d", schema, "tickets.read", ToolKind.READ, _record))


def test_write_tools_must_declare_an_idempotency_key() -> None:
    reg = ToolRegistry(db=None)  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="idempotency"):
        reg.register(Tool("w", "d", SCHEMA, "tickets.read", ToolKind.WRITE, _record))


def test_the_three_shipped_tools_register_and_are_read_only() -> None:
    reg = ToolRegistry(db=None)  # type: ignore[arg-type]
    for tool in TOOLS:
        reg.register(tool)
    assert {t.name for t in TOOLS} == {"get_resource_context", "search_tickets", "get_cost_trend"}
    assert all(t.kind is ToolKind.READ for t in TOOLS)
