"""The bounded tool loop (LLD: 10 tool-call iterations, then escalate).

    result = await run_tool_loop(gateway, registry, ctx, tier=Tier.LARGE, task="answer",
                                 messages=[...])

Deterministic control flow, probabilistic content (CLAUDE.md rule 3). The
model may ask for tools from the registry's allowlist; this loop decides
everything else:
- every call goes through ``registry.invoke`` (permission, schema, scope);
- results go back to the model through the gateway, which redacts them;
- after ``tool_iterations`` rounds the loop stops, records an escalation and
  returns ``escalated`` for a human, however much the model wants another
  call.

The model's text is returned as content. It never selects the next stage of a
workflow; whatever calls this does (step 07: Temporal).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from cloudops_gateway.gateway import CallContext, Gateway
from cloudops_gateway.providers import Message, Tier
from cloudops_gateway.registry import ToolRegistry


@dataclass
class LoopResult:
    status: Literal["answered", "escalated"]
    text: str
    iterations: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    call_ids: list[int] = field(default_factory=list)


async def run_tool_loop(
    gateway: Gateway,
    registry: ToolRegistry,
    ctx: CallContext,
    *,
    tier: Tier,
    task: str,
    messages: Sequence[Message],
    first_turn: int = 1,
) -> LoopResult:
    history = list(messages)
    trace: list[dict[str, Any]] = []
    calls: list[int] = []
    tools = registry.specs(ctx.permissions)
    ceiling = gateway.limits.tool_iterations
    for iteration in range(ceiling + 1):
        reply = await gateway.complete(ctx, tier=tier, task=task, messages=history, tools=tools,
                                       tool_calls=trace or None, turn=first_turn)
        calls.append(reply.call_id)
        if not reply.tool_calls:
            return LoopResult("answered", reply.text, iteration, trace, calls)
        if iteration == ceiling:
            break
        history.append(Message("assistant", reply.text, tool_calls=reply.tool_calls))
        for call in reply.tool_calls:
            outcome = await registry.invoke(call, ctx)
            trace.append({"iteration": iteration + 1, **outcome.trace()})
            history.append(Message("tool", outcome.content, tool_call_id=call.id, name=call.name))
    calls.append(await gateway.record_escalation(
        ctx, task, f"tool iteration ceiling ({ceiling}) reached", trace))
    return LoopResult("escalated", "", ceiling, trace, calls)
