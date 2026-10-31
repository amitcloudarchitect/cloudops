"""Where agent runs are recorded: ``cloudops.agent_run`` (migration 0007).

One row per run, written under the conversation's scope through
packages/tenancy, so a run is visible to exactly the people who could see
the tenant it was about. It carries the trace id of the ``agent.run`` span:
the span holds no tenant (ADR 0022), so this row is where the run is
attributed to its tenant and user. ``MemoryTraceSink`` is for tests.
"""

from __future__ import annotations

import json
from typing import Final

from sqlalchemy import text

from cloudops_agents.base import AgentRunRecord
from cloudops_observability import current_trace_id
from cloudops_tenancy import Database

_INSERT: Final = text(
    "INSERT INTO cloudops.agent_run (id, tenant_id, principal, conversation_id, agent,"
    " prompt_version, prompt_hash, tier, status, attempts, retrieval, tool_calls, llm_call_ids,"
    " tokens_in, tokens_out, cost, cost_currency, latency_ms, confidence, output, error,"
    " trace_id)"
    " VALUES (:id, :tenant_id, :principal, :conversation_id, :agent, :prompt_version,"
    " :prompt_hash, :tier, :status, :attempts, CAST(:retrieval AS jsonb),"
    " CAST(:tool_calls AS jsonb), CAST(:llm_call_ids AS bigint[]), :tokens_in, :tokens_out,"
    " :cost, :currency, :latency_ms, :confidence, CAST(:output AS jsonb), :error, :trace_id)"
)


class DbTraceSink:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, run: AgentRunRecord) -> None:
        async with self._db.transaction(run.ctx.scope) as conn:
            await conn.execute(_INSERT, {
                "id": run.run_id, "tenant_id": run.ctx.tenant_id,
                "principal": run.ctx.principal_id, "conversation_id": run.ctx.conversation_id,
                "agent": run.agent, "prompt_version": run.prompt_version,
                "prompt_hash": run.prompt_hash, "tier": run.tier.value, "status": run.status,
                "attempts": run.attempts, "retrieval": json.dumps(run.retrieval),
                "tool_calls": json.dumps(run.tool_calls), "llm_call_ids": run.llm_call_ids,
                "tokens_in": run.tokens_in, "tokens_out": run.tokens_out, "cost": run.cost,
                "currency": run.currency, "latency_ms": run.latency_ms,
                "confidence": run.confidence,
                "output": None if run.output is None else json.dumps(run.output),
                "error": run.error, "trace_id": current_trace_id(),
            })


class MemoryTraceSink:
    def __init__(self) -> None:
        self.runs: list[AgentRunRecord] = []

    async def record(self, run: AgentRunRecord) -> None:
        self.runs.append(run)
