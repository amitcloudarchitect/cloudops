"""The agent base: a prompt file, a tier, a tool allowlist, an output schema, a
confidence threshold. That is all an agent is.

    agent = Agent(CLASSIFIER, gateway, registry=None, trace=DbTraceSink(db))
    result = await agent.run(ctx, request="Classify this ticket.", data=[ticket_data(t)])

``run``:

1. Builds the messages: the template, the data sections and the output
   contract (``prompts.build_messages``).
2. Calls the gateway, which owns quotas, redaction, fallback and the per-call
   trace, offering only tools that are both on the allowlist and permitted
   to the caller.
3. **Tool calls.** A tool outside the allowlist is refused without reaching
   the registry; anything else goes through ``registry.invoke`` (permission,
   schema, scope). Results return as structured JSON. On the eleventh round
   the agent records an escalation and raises ``AgentEscalationError``.
4. **Output.** Validated with Pydantic against the schema: JSON in, a model
   out, never regex. One retry with the validation errors fed back; a second
   failure raises ``AgentOutputError``. Nothing is ever partly parsed.
5. **Confidence.** Below the threshold the result is ``declined`` and carries
   no output to act on.
6. **Trace.** One ``agent_run`` record, whatever happened.

Agents do not choose what happens next. They return content; the workflow
that called them decides.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cloudops_agents.prompts import DataItem, PromptTemplate, build_messages
from cloudops_gateway.gateway import CallContext, GatewayRefusedError, GatewayReply
from cloudops_gateway.providers import Message, Tier, ToolCallRequest, ToolSpec
from cloudops_gateway.registry import ToolOutcome

TOOL_ITERATION_CEILING: Final = 10  # LLD: ten tool-call iterations, then escalate
OUTPUT_ATTEMPTS: Final = 2  # the first answer, and one retry with the errors


class AgentOutput(BaseModel):
    """Base for every output schema: closed, immutable, with a confidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)
    confidence: float = Field(ge=0.0, le=1.0)


class AgentError(Exception):
    pass


class AgentOutputError(AgentError):
    """The model's output failed validation twice. Nothing is returned."""


class AgentEscalationError(AgentError):
    """The tool iteration ceiling was reached; a person takes over."""


@dataclass(frozen=True)
class AgentSpec[O: AgentOutput]:
    name: str
    prompt: PromptTemplate
    tier: Tier
    output: type[O]
    confidence_threshold: float
    tools: frozenset[str] = frozenset()
    slots: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.tier is Tier.EMBED:
            raise ValueError("agents complete text; the embed tier is for retrieval")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between 0 and 1")


@dataclass
class AgentResult[O: AgentOutput]:
    run_id: uuid.UUID
    agent: str
    status: Literal["answered", "declined"]
    output: O | None
    confidence: float
    attempts: int
    prompt_version: str
    prompt_hash: str
    prompt: str  # the last prompt exactly as the model received it (redacted)
    response: str  # the last raw response text
    tier: Tier
    tool_calls: list[dict[str, Any]]
    llm_call_ids: list[int]
    tokens_in: int
    tokens_out: int
    cost: Decimal
    currency: str
    latency_ms: int


class GatewayLike(Protocol):
    async def complete(
        self, ctx: CallContext, *, tier: Tier, task: str, messages: Sequence[Message],
        tools: Sequence[ToolSpec] = ..., retrieval: Sequence[Mapping[str, Any]] | None = ...,
        tool_calls: Sequence[Mapping[str, Any]] | None = ...,
    ) -> GatewayReply: ...

    async def record_escalation(self, ctx: CallContext, task: str, reason: str,
                                tool_calls: Sequence[Mapping[str, Any]]) -> int: ...


class RegistryLike(Protocol):
    def specs(self, permissions: frozenset[str]) -> list[ToolSpec]: ...

    async def invoke(self, call: ToolCallRequest, ctx: CallContext) -> ToolOutcome: ...


@dataclass(frozen=True)
class AgentRunRecord:
    run_id: uuid.UUID
    ctx: CallContext
    agent: str
    prompt_version: str
    prompt_hash: str
    tier: Tier
    status: str
    attempts: int
    retrieval: list[str]
    tool_calls: list[dict[str, Any]]
    llm_call_ids: list[int]
    tokens_in: int
    tokens_out: int
    cost: Decimal
    currency: str
    latency_ms: int
    confidence: float | None
    output: dict[str, Any] | None
    error: str | None


class TraceSink(Protocol):
    async def record(self, run: AgentRunRecord) -> None: ...


def _validation_feedback(exc: ValidationError) -> str:
    """Where and why, never the offending input (which may be model-echoed data)."""
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
        for e in exc.errors(include_input=False, include_url=False)
    )
    return ("Your previous reply did not match the required JSON Schema: "
            f"{problems}. Reply again with only the corrected JSON object.")


class Agent[O: AgentOutput]:
    def __init__(self, spec: AgentSpec[O], gateway: GatewayLike,
                 registry: RegistryLike | None, trace: TraceSink) -> None:
        if spec.tools and registry is None:
            raise ValueError(f"{spec.name} allows tools but has no registry")
        self.spec = spec
        self._gateway = gateway
        self._registry = registry
        self._trace = trace

    async def run(
        self,
        ctx: CallContext,
        *,
        request: str,
        data: Sequence[DataItem] = (),
        retrieval_ids: Sequence[str] = (),
    ) -> AgentResult[O]:
        spec = self.spec
        run_id = uuid.uuid4()
        history: list[Message] = build_messages(
            spec.prompt.render(spec.slots), schema=spec.output.model_json_schema(),
            request=request, data=data,
        )
        offered = ([s for s in self._registry.specs(ctx.permissions) if s.name in spec.tools]
                   if self._registry is not None else [])
        retrieval = [{"id": r} for r in retrieval_ids] or None
        tools_trace: list[dict[str, Any]] = []
        calls: list[int] = []
        tokens_in = tokens_out = latency = 0
        cost = Decimal(0)
        currency = "USD"
        prompt = response = ""
        attempts = rounds = 0
        started = time.monotonic()

        async def finish(status: str, *, output: O | None = None, error: str | None = None
                         ) -> None:
            await self._trace.record(AgentRunRecord(
                run_id, ctx, spec.name, spec.prompt.version,
                hashlib.sha256(prompt.encode()).hexdigest(), spec.tier, status, attempts,
                list(retrieval_ids), tools_trace, calls, tokens_in, tokens_out, cost, currency,
                int((time.monotonic() - started) * 1000),
                None if output is None else output.confidence,
                None if output is None else output.model_dump(mode="json"), error,
            ))

        try:
            while True:
                reply = await self._gateway.complete(
                    ctx, tier=spec.tier, task=spec.name, messages=history, tools=offered,
                    retrieval=retrieval, tool_calls=tools_trace or None,
                )
                calls.append(reply.call_id)
                tokens_in += reply.tokens_in
                tokens_out += reply.tokens_out
                cost += reply.cost
                currency = reply.currency
                latency += reply.latency_ms
                prompt, response = reply.prompt_redacted, reply.text

                if reply.tool_calls:
                    if rounds == TOOL_ITERATION_CEILING:
                        calls.append(await self._gateway.record_escalation(
                            ctx, spec.name,
                            f"tool iteration ceiling ({TOOL_ITERATION_CEILING}) reached",
                            tools_trace))
                        await finish("escalated", error="tool iteration ceiling reached")
                        raise AgentEscalationError(spec.name)
                    rounds += 1
                    history.append(Message("assistant", reply.text, tool_calls=reply.tool_calls))
                    for call in reply.tool_calls:
                        outcome = await self._tool(call, ctx)
                        tools_trace.append({"round": rounds, **outcome.trace()})
                        history.append(Message("tool", outcome.content,
                                               tool_call_id=call.id, name=call.name))
                    continue

                attempts += 1
                try:
                    output = spec.output.model_validate_json(reply.text)
                except ValidationError as exc:
                    if attempts >= OUTPUT_ATTEMPTS:
                        await finish("invalid_output", error=_validation_feedback(exc)[:2000])
                        raise AgentOutputError(f"{spec.name}: output failed validation twice"
                                               ) from None
                    history.append(Message("assistant", reply.text))
                    history.append(Message("user", _validation_feedback(exc)))
                    continue

                declined = output.confidence < spec.confidence_threshold
                status: Literal["answered", "declined"] = "declined" if declined else "answered"
                await finish(status, output=output)
                return AgentResult(
                    run_id, spec.name, status, None if declined else output, output.confidence,
                    attempts, spec.prompt.version, hashlib.sha256(prompt.encode()).hexdigest(),
                    prompt, response, spec.tier, tools_trace, calls, tokens_in, tokens_out,
                    cost, currency, latency,
                )
        except (AgentOutputError, AgentEscalationError):
            raise
        except GatewayRefusedError as exc:
            await finish("refused", error=exc.code)
            raise
        except Exception as exc:
            await finish("failed", error=type(exc).__name__)
            raise

    async def _tool(self, call: ToolCallRequest, ctx: CallContext) -> ToolOutcome:
        if call.name not in self.spec.tools or self._registry is None:
            # Never reaches the registry, let alone a service.
            return ToolOutcome(call.id, call.name, False,
                               json.dumps({"refused": "not_in_allowlist"}), "not_in_allowlist")
        return await self._registry.invoke(call, ctx)
