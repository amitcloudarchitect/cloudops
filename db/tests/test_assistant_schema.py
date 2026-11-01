"""The assistant's tables and its paths into the gateway's ledger (migration 0023).

Conversations are tenant data with a second lock: only the principal who wrote a
row sees it. A customer scope may write the trace of its own calls but never read
one, reads the semantic cache under its own scope key only, and reads only the
customer help pages of the Getronics-wide knowledge.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Seeded, embedding

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


def _as(tenant: Any, principal: str, role: Role = Role.ENGINEER) -> TenantScope:
    return TenantScope([tenant.id], role, principal)


async def _rows(db: Database, s: TenantScope, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with db.transaction(s) as conn:
        return [tuple(r) for r in await conn.execute(text(sql), params)]


async def _conversation(db: Database, s: TenantScope, title: str = "q") -> UUID:
    async with db.transaction(s) as conn:
        cid = await conn.scalar(text(
            "INSERT INTO cloudops.assistant_conversation (tenant_id, principal, mode, title)"
            " VALUES (:t, :p, 'account', :title) RETURNING id"),
            {"t": next(iter(s.tenant_ids)), "p": s.principal_id, "title": title})
        await conn.execute(text(
            "INSERT INTO cloudops.assistant_turn (tenant_id, conversation_id, principal, seq,"
            " question) VALUES (:t, :c, :p, 1, :q)"),
            {"t": next(iter(s.tenant_ids)), "c": cid, "p": s.principal_id, "q": f"{title}?"})
    assert isinstance(cid, UUID)
    return cid


async def test_conversations_are_visible_to_their_author_only(
    app_db: Database, seeded: Seeded
) -> None:
    alice, bob = _as(seeded.a, "user:alice"), _as(seeded.a, "user:bob")
    other_tenant = _as(seeded.b, "user:alice")
    mine = await _conversation(app_db, alice, "alice")
    await _conversation(app_db, bob, "bob")
    sql = "SELECT title FROM cloudops.assistant_conversation ORDER BY 1"
    assert await _rows(app_db, alice, sql) == [("alice",)]
    assert await _rows(app_db, bob, sql) == [("bob",)]
    # The same principal under another tenant sees nothing of tenant A.
    assert await _rows(app_db, other_tenant, sql) == []
    # Bob cannot read or delete Alice's turns by id.
    turn_sql = "SELECT question FROM cloudops.assistant_turn WHERE conversation_id = :c"
    assert await _rows(app_db, bob, turn_sql, c=mine) == []
    async with app_db.transaction(bob) as conn:
        gone = await conn.execute(text(
            "DELETE FROM cloudops.assistant_conversation WHERE id = :c"), {"c": mine})
        assert gone.rowcount == 0
    assert await _rows(app_db, alice, turn_sql, c=mine) == [("alice?",)]


async def test_a_conversation_cannot_be_written_for_someone_else(
    app_db: Database, seeded: Seeded
) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        async with app_db.transaction(_as(seeded.a, "user:alice")) as conn:
            await conn.execute(text(
                "INSERT INTO cloudops.assistant_conversation (tenant_id, principal, mode, title)"
                " VALUES (:t, 'user:bob', 'account', 'forged')"), {"t": seeded.a.id})
    with pytest.raises(DBAPIError, match="row-level security"):
        async with app_db.transaction(_as(seeded.a, "user:alice")) as conn:
            await conn.execute(text(
                "INSERT INTO cloudops.assistant_conversation (tenant_id, principal, mode, title)"
                " VALUES (:t, 'user:alice', 'account', 'other tenant')"), {"t": seeded.b.id})


async def test_customer_writes_its_trace_but_never_reads_one(
    app_db: Database, seeded: Seeded
) -> None:
    customer = _as(seeded.a, "user:customer-a", Role.CUSTOMER)
    run = uuid4()
    async with app_db.transaction(customer) as conn:
        call_id = await conn.scalar(text("SELECT nextval('cloudops.llm_call_id_seq')"))
        await conn.execute(text(
            "INSERT INTO cloudops.llm_call (id, tenant_id, principal, task, tier_requested,"
            " prompt_redacted, tokens_in, tokens_out, cost, cost_currency, outcome)"
            " VALUES (:id, :t, 'user:customer-a', 'assistant.answer', 'large', 'secret prompt',"
            " 100, 50, 0.5, 'USD', 'succeeded')"), {"id": call_id, "t": seeded.a.id})
        await conn.execute(text(
            "INSERT INTO cloudops.agent_run (id, tenant_id, principal, agent, prompt_version,"
            " prompt_hash, tier, status, attempts, cost_currency)"
            " VALUES (:id, :t, 'user:customer-a', 'assistant_answerer', 'v1', :h, 'large',"
            " 'answered', 1, 'USD')"), {"id": run, "t": seeded.a.id, "h": "0" * 64})
    for sql in ("SELECT prompt_redacted FROM cloudops.llm_call",
                "SELECT cost FROM cloudops.llm_call", "SELECT output FROM cloudops.agent_run"):
        with pytest.raises(DBAPIError, match="permission denied"):
            await _rows(app_db, customer, sql)
    # Staff of the tenant see it, as before.
    staff = await _rows(app_db, _as(seeded.a, "user:staff"),
                        "SELECT prompt_redacted FROM cloudops.llm_call WHERE id = :id", id=call_id)
    assert staff == [("secret prompt",)]
    # Totals, through the function, and only the caller's own and its tenant's.
    usage = await _rows(app_db, customer,
                        "SELECT * FROM cloudops.llm_usage('assistant.', interval '15 minutes',"
                        " interval '24 hours', 5, 1000)")
    assert usage[0][0] >= 150
    assert usage[0][2] >= 150
    other = await _rows(app_db, _as(seeded.b, "user:customer-a", Role.CUSTOMER),
                        "SELECT * FROM cloudops.llm_usage('assistant.', interval '15 minutes',"
                        " interval '24 hours', 5, 1000)")
    assert other[0][:4] == (0, 0, 0, 0)


async def test_customer_cannot_write_a_trace_for_another_tenant(
    app_db: Database, seeded: Seeded
) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        async with app_db.transaction(_as(seeded.a, "user:c", Role.CUSTOMER)) as conn:
            await conn.execute(text(
                "INSERT INTO cloudops.llm_call (id, tenant_id, principal, task, tier_requested,"
                " cost_currency, outcome) VALUES (nextval('cloudops.llm_call_id_seq'), :t,"
                " 'user:c', 'assistant.answer', 'small', 'USD', 'succeeded')"),
                {"t": seeded.b.id})


def _scope_key(role: str, accounts: str = "all") -> str:
    return hashlib.sha256(f"{role}|{accounts}".encode()).hexdigest()


async def test_customer_reads_the_cache_under_its_own_scope_key_only(
    app_db: Database, seeded: Seeded
) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for key, response in ((_scope_key("customer"), "customer answer"),
                              (_scope_key("cfo"), "cfo answer")):
            cx.execute(
                "INSERT INTO cloudops.llm_cache (tenant_id, scope_key, tier, task, prompt_hash,"
                " embedding, response, expires_at) VALUES (%s, %s, 'large', 'assistant.help',"
                " 'h', %s, %s, now() + interval '1 hour')",
                (seeded.a.id, key, embedding(1), response))
    customer = _as(seeded.a, "user:c", Role.CUSTOMER)
    seen = await _rows(app_db, customer, "SELECT response FROM cloudops.llm_cache")
    assert seen == [("customer answer",)]
    with pytest.raises(DBAPIError, match="row-level security"):
        async with app_db.transaction(customer) as conn:
            await conn.execute(text(
                "INSERT INTO cloudops.llm_cache (tenant_id, scope_key, tier, task, prompt_hash,"
                " embedding, response, expires_at) VALUES (:t, :k, 'large', 'x', 'h',"
                " CAST(:e AS vector), 'poisoned', now() + interval '1 hour')"),
                {"t": seeded.a.id, "k": _scope_key("cfo"), "e": embedding(2)})
    cfo = await _rows(app_db, _as(seeded.a, "user:cfo", Role.CFO),
                      "SELECT response FROM cloudops.llm_cache ORDER BY 1")
    assert cfo == [("cfo answer",), ("customer answer",)]


async def test_customer_reads_customer_help_and_nothing_else_global(
    app_db: Database, seeded: Seeded
) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for kind in ("platform_help", "platform_help_staff"):
            cx.execute(
                "INSERT INTO cloudops.global_knowledge_chunk (source_kind, source_ref, title, body)"
                " VALUES (%s, %s, %s, 'body')", (kind, f"test#{kind}", kind))
    sql = "SELECT DISTINCT source_kind FROM cloudops.global_knowledge_chunk ORDER BY 1"
    customer = await _rows(app_db, _as(seeded.a, "user:c", Role.CUSTOMER), sql)
    staff = await _rows(app_db, _as(seeded.a, "user:s"), sql)
    assert customer == [("platform_help",)]
    assert {"platform_help", "platform_help_staff", "runbook"} <= {r[0] for r in staff}


async def test_fill_global_embedding_fills_only_an_empty_unchanged_body(
    app_db: Database, seeded: Seeded
) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        row = cx.execute(
            "INSERT INTO cloudops.global_knowledge_chunk (source_kind, source_ref, title, body)"
            " VALUES ('platform_help', 'fill#1', 't', 'help body') RETURNING id, md5(body)"
        ).fetchone()
    assert row is not None
    chunk, digest = row
    s = _as(seeded.a, "service:indexer")
    sql = "SELECT cloudops.fill_global_embedding(:id, :d, :e)"
    assert await _rows(app_db, s, sql, id=chunk, d="stale", e=embedding(3)) == [(False,)]
    assert await _rows(app_db, s, sql, id=chunk, d=digest, e=embedding(3)) == [(True,)]
    # Already filled: never overwritten.
    assert await _rows(app_db, s, sql, id=chunk, d=digest, e=embedding(4)) == [(False,)]
    with pytest.raises(DBAPIError, match="permission denied"):
        await _rows(app_db, s, "UPDATE cloudops.global_knowledge_chunk SET body = 'x'")


async def test_forget_clears_recorded_prompts_of_the_callers_conversation_only(
    app_db: Database, seeded: Seeded
) -> None:
    alice, bob = _as(seeded.a, "user:alice"), _as(seeded.a, "user:bob")
    conversation = uuid4()
    for who in (alice, bob):
        async with app_db.transaction(who) as conn:
            await conn.execute(text(
                "INSERT INTO cloudops.llm_call (id, tenant_id, principal, conversation_id, task,"
                " tier_requested, prompt_redacted, cost_currency, outcome)"
                " VALUES (nextval('cloudops.llm_call_id_seq'), :t, :p, :c, 'assistant.answer',"
                " 'large', 'remember me', 'USD', 'succeeded')"),
                {"t": seeded.a.id, "p": who.principal_id, "c": conversation})
    assert await _rows(app_db, alice, "SELECT cloudops.assistant_forget(:c)", c=conversation
                       ) == [(1,)]
    left = await _rows(app_db, alice,
                       "SELECT principal, prompt_redacted FROM cloudops.llm_call"
                       " WHERE conversation_id = :c ORDER BY 1", c=conversation)
    assert left == [("user:alice", None), ("user:bob", "remember me")]


async def test_purge_removes_only_conversations_older_than_ninety_days(
    app_db: Database, seeded: Seeded
) -> None:
    alice = _as(seeded.a, "user:purge")
    old = await _conversation(app_db, alice, "old")
    new = await _conversation(app_db, alice, "new")
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.assistant_conversation SET updated_at = now() - interval"
                   " '91 days' WHERE id = %s", (old,))
    removed = await _rows(app_db, _as(seeded.c, "service:assistant-retention"),
                          "SELECT cloudops.assistant_purge()")
    assert removed[0][0] >= 1
    ids = {r[0] for r in await _rows(
        app_db, alice, "SELECT conversation_id FROM cloudops.assistant_turn")}
    assert new in ids
    assert old not in ids
