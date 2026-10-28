"""The Temporal pattern for agents: agent calls are activities, never workflow code.

A model call is non-deterministic, so it must not run during a workflow
replay: it runs in an activity, whose result Temporal records. That
recorded result carries the full audit trail:
- the prompt as the model received it, the prompt version and hash;
- the tier, the response and the validated output;
- tokens and cost, and the trace ids.

So the workflow history alone explains what the model was asked and what it
said.

Timeouts and retries suit LLM latency:
- two minutes per attempt, ten minutes in all;
- three attempts with backoff, for transient provider or network failures;
- invalid output, an escalation, a quota or breaker refusal, or a scope error
  is not transient, so it is not retried.

The activity input carries the scope resolved server-side when the request
arrived (ADR 0002). It is never something a model produced, and the activity
narrows it to the conversation's tenant again (``CallContext.for_tenant``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from cloudops_agents.base import Agent
    from cloudops_agents.prompts import DataItem
    from cloudops_gateway.gateway import CallContext
    from cloudops_tenancy import Role, TenantScope

AGENT_START_TO_CLOSE: Final = timedelta(minutes=2)
AGENT_SCHEDULE_TO_CLOSE: Final = timedelta(minutes=10)
AGENT_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=3,
    non_retryable_error_types=[
        "AgentOutputError", "AgentEscalationError", "QuotaExceededError", "CircuitOpenError",
        "ConversationTooLongError", "ScopeError", "ValidationError", "KeyError",
    ],
)


@dataclass
class AgentCall:
    agent: str
    tenant_id: str
    principal_id: str
    role: str
    resolved_tenant_ids: list[str]
    permissions: list[str]
    request: str
    data: list[dict[str, Any]] = field(default_factory=list)
    account_ids: list[str] | None = None
    conversation_id: str | None = None


@dataclass
class AgentActivityResult:
    run_id: str
    agent: str
    status: str
    output: dict[str, Any] | None
    confidence: float
    prompt_version: str
    prompt_hash: str
    prompt: str
    tier: str
    response: str
    tokens_in: int
    tokens_out: int
    cost: str  # Decimal as text: money never becomes a float, even in history
    currency: str
    llm_call_ids: list[int]
    tool_calls: list[dict[str, Any]]


class AgentActivities:
    def __init__(self, agents: dict[str, Agent[Any]]) -> None:
        self._agents = agents

    @activity.defn
    async def run_agent(self, call: AgentCall) -> AgentActivityResult:
        agent = self._agents[call.agent]
        resolved = TenantScope(
            [UUID(t) for t in call.resolved_tenant_ids], Role(call.role), call.principal_id,
            account_ids=None if call.account_ids is None else [UUID(a) for a in call.account_ids],
        )
        ctx = CallContext.for_tenant(
            resolved, UUID(call.tenant_id), call.permissions,
            conversation_id=UUID(call.conversation_id) if call.conversation_id else None,
        )
        data = [DataItem(d["source"], d["content"], d.get("ref")) for d in call.data]
        result = await agent.run(ctx, request=call.request, data=data)
        return AgentActivityResult(
            str(result.run_id), result.agent, result.status,
            None if result.output is None else result.output.model_dump(mode="json"),
            result.confidence, result.prompt_version, result.prompt_hash, result.prompt,
            result.tier.value, result.response, result.tokens_in, result.tokens_out,
            str(result.cost), result.currency, result.llm_call_ids, result.tool_calls,
        )


@workflow.defn
class RunAgentWorkflow:
    """The minimal pattern: one agent call as an activity. Real workflows put
    agent activities between deterministic steps they own."""

    @workflow.run
    async def run(self, call: AgentCall) -> AgentActivityResult:
        return await workflow.execute_activity_method(
            AgentActivities.run_agent, call,
            start_to_close_timeout=AGENT_START_TO_CLOSE,
            schedule_to_close_timeout=AGENT_SCHEDULE_TO_CLOSE,
            retry_policy=AGENT_RETRY,
        )
