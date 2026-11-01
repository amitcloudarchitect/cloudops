"""The AI gateway (LLD, Gateway controls; ADR 0005).

    ctx = CallContext.for_tenant(access_scope, tenant_id, permissions)
    reply = await gateway.complete(ctx, tier=Tier.SMALL, task="classify_ticket", messages=[...])

One call, in this order. Each refusal is traced before it is raised:

1. **Turn ceiling:** at most 20 turns per conversation.
2. **Quotas:** tokens per user per day and per tenant per month, read from the
   ledger through ``cloudops.llm_usage`` (totals only, so a customer scope can
   ask too). A task family may have its own sub-budget inside them (the
   assistant: ADR 0029). Refused at 100%, with a warning flag from 80%.
3. **Spend breaker:** this tenant's spend over the last 15 minutes against
   its trailing 24-hour rate (a verdict from ``cloudops.llm_usage``), and the
   platform's (through ``cloudops.llm_platform_spend``). Open above five times
   the trailing rate, once past a floor.
4. **Redaction:** of every message, before anything leaves the process.
5. **Semantic cache:** only when the caller asks for it. Scoped by tenant
   (RLS), role class and subscription scope.
6. **Providers:** the tier's deployments in order. Throttling or an outage moves
   to the next one; ``large`` falls back to ``small`` (LLD).
7. **Trace:** one ``cloudops.llm_call`` row with the redacted prompt, retrieval
   set, tool calls, tokens, latency and cost. The ledger and the trace are the
   same row. It carries the OpenTelemetry trace id; the span (``llm.complete``,
   ``llm.embed``) and the metrics carry task, tier, tokens, cost and outcome but
   never the tenant, the user or the prompt (ADR 0022).

Every database access runs under ``ctx.scope``: the conversation's single
tenant, checked server-side against the caller's resolved scope. Nothing a
model sends can change it.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from opentelemetry.trace import SpanKind, get_current_span
from sqlalchemy import text

from cloudops_gateway.pricing import SPEND_BREAKER_FLOOR, Deployment
from cloudops_gateway.providers import (
    Completion,
    Message,
    Provider,
    ProviderError,
    Tier,
    ToolCallRequest,
    ToolSpec,
    estimate_tokens,
)
from cloudops_gateway.redaction import redact
from cloudops_observability import current_trace_id, record_llm_call, tracer
from cloudops_tenancy import Database, TenantScope

CacheClass = Literal["live", "static"]
CACHE_TTL: Final = {"live": timedelta(hours=1), "static": timedelta(hours=24)}
CACHE_SIMILARITY: Final = 0.97
MAX_TOKENS: Final = 2048


class GatewayRefusedError(Exception):
    """The gateway refused the call. ``code`` is safe to show a caller."""

    code = "refused"


class ScopeError(GatewayRefusedError):
    code = "tenant_not_in_scope"


class ConversationTooLongError(GatewayRefusedError):
    code = "conversation_turn_ceiling"


class QuotaExceededError(GatewayRefusedError):
    code = "quota_exceeded"


class CircuitOpenError(GatewayRefusedError):
    code = "spend_circuit_open"


class AllProvidersFailedError(Exception):
    """Every deployment for the tier (and its fallback) was throttled or down."""


@dataclass(frozen=True)
class SubBudget:
    """A task family's share of the tenant's budget, and its own per-user cap,
    so one family (the assistant) can never starve another (triage)."""

    user_daily_tokens: int
    tenant_monthly_fraction: float


@dataclass(frozen=True)
class Limits:
    user_daily_tokens: int = 200_000
    tenant_monthly_tokens: int = 5_000_000
    alert_fraction: float = 0.8
    conversation_turns: int = 20
    tool_iterations: int = 10
    breaker_recent: timedelta = timedelta(minutes=15)
    breaker_trailing: timedelta = timedelta(hours=24)
    breaker_multiple: Decimal = Decimal(5)  # not money: a ratio of spend rates
    breaker_floor: Decimal = SPEND_BREAKER_FLOOR  # recent spend below this never trips
    # Task prefix -> sub-budget. ADR 0029: the assistant may use 40% of the
    # tenant's month, and 100k tokens per user per day.
    sub_budgets: Mapping[str, SubBudget] = field(default_factory=lambda: {
        "assistant.": SubBudget(user_daily_tokens=100_000, tenant_monthly_fraction=0.4)})


@dataclass(frozen=True)
class CallContext:
    """Who is calling, about which tenant, with which permissions. Built from
    the server-side resolved scope; there is no constructor that takes a tenant
    from anywhere else."""

    scope: TenantScope  # narrowed to exactly one tenant
    tenant_id: UUID
    permissions: frozenset[str]
    conversation_id: UUID | None = None
    personal_terms: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def for_tenant(
        cls,
        resolved: TenantScope,
        tenant_id: UUID,
        permissions: Iterable[str],
        *,
        conversation_id: UUID | None = None,
        personal_terms: Iterable[str] = (),
    ) -> CallContext:
        if tenant_id not in resolved.tenant_ids:
            raise ScopeError("tenant outside the caller's scope")
        narrowed = TenantScope(
            [tenant_id], resolved.role, resolved.principal_id, account_ids=resolved.account_ids
        )
        return cls(narrowed, tenant_id, frozenset(permissions), conversation_id,
                   frozenset(personal_terms))

    @property
    def principal_id(self) -> str:
        return self.scope.principal_id

    @property
    def cache_scope_key(self) -> str:
        """Role class and subscription scope: a cached answer is only ever
        served to callers who could have received exactly that answer."""
        accounts = "all" if self.scope.account_ids is None else self.scope.account_ids_setting
        return hashlib.sha256(f"{self.scope.role.value}|{accounts}".encode()).hexdigest()


@dataclass(frozen=True)
class GatewayReply:
    text: str
    tool_calls: tuple[ToolCallRequest, ...]
    tier_used: Tier
    provider: str
    deployment: str
    tokens_in: int
    tokens_out: int
    cost: Decimal
    currency: str
    cache_hit: bool
    budget_warning: bool
    call_id: int
    # Exactly what the provider received (redacted), and how long it took.
    prompt_redacted: str = ""
    latency_ms: int = 0


# The id comes from the sequence, not RETURNING: a customer scope may insert a
# ledger row but never read one back (migration 0023).
_NEXT_CALL_ID: Final = text("SELECT nextval('cloudops.llm_call_id_seq')")
_INSERT_CALL: Final = text(
    "INSERT INTO cloudops.llm_call (id, tenant_id, principal, conversation_id, task,"
    " tier_requested, tier_used, provider, deployment, prompt_redacted, redactions, retrieval,"
    " tool_calls,"
    " tokens_in, tokens_out, cost, cost_currency, latency_ms, cache_hit, outcome, reason,"
    " trace_id)"
    " VALUES (:id, :tenant_id, :principal, :conversation_id, :task, :tier_requested, :tier_used,"
    " :provider, :deployment, :prompt, :redactions, CAST(:retrieval AS jsonb),"
    " CAST(:tool_calls AS jsonb), :tokens_in, :tokens_out, :cost, :currency, :latency_ms,"
    " :cache_hit, :outcome, :reason, :trace_id)"
)
# Totals for this principal and tenant (the scope's), and the tenant breaker's
# verdict: never a cost or a row, so a customer scope may ask (migration 0023).
_USAGE: Final = text(
    "SELECT user_day_tokens, tenant_month_tokens, task_user_day_tokens,"
    " task_tenant_month_tokens, tenant_breaker_open FROM cloudops.llm_usage(:prefix,"
    " CAST(:recent AS interval), CAST(:trailing AS interval), :multiple, :floor)"
)
_PLATFORM_SPEND: Final = text(
    "SELECT recent_spend, trailing_spend FROM cloudops.llm_platform_spend("
    "CAST(:recent AS interval), CAST(:trailing AS interval))"
)
_CACHE_LOOKUP: Final = text(
    "SELECT response, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity"
    " FROM cloudops.llm_cache"
    " WHERE tenant_id = ANY(cloudops.current_tenant_ids())"
    "   AND scope_key = :scope_key AND tier = :tier AND task = :task AND expires_at > now()"
    " ORDER BY embedding <=> CAST(:embedding AS vector) LIMIT 1"
)
_CACHE_STORE: Final = text(
    "INSERT INTO cloudops.llm_cache (tenant_id, scope_key, tier, task, prompt_hash, embedding,"
    " response, expires_at) VALUES (:tenant_id, :scope_key, :tier, :task, :prompt_hash,"
    " CAST(:embedding AS vector), :response, now() + CAST(:ttl AS interval))"
)


def _interval(delta: timedelta) -> str:
    return f"{int(delta.total_seconds())} seconds"


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def _prompt(messages: Sequence[Message]) -> str:
    return "\n".join(f"[{m.role}{':' + m.name if m.name else ''}] {m.content}" for m in messages)


class Gateway:
    def __init__(
        self,
        db: Database,
        providers: Mapping[str, Provider],
        deployments: Mapping[Tier, Sequence[Deployment]],
        limits: Limits | None = None,
    ) -> None:
        self._db = db
        self._providers = dict(providers)
        self._deployments = {t: tuple(d) for t, d in deployments.items()}
        self.limits = limits or Limits()

    # --- public ----------------------------------------------------------------

    async def complete(
        self,
        ctx: CallContext,
        *,
        tier: Tier,
        task: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        cache: CacheClass | None = None,
        retrieval: Sequence[Mapping[str, Any]] | None = None,
        tool_calls: Sequence[Mapping[str, Any]] | None = None,
        turn: int = 1,
    ) -> GatewayReply:
        with tracer().start_as_current_span("llm.complete", kind=SpanKind.CLIENT, attributes={
                "gen_ai.operation.name": "chat", "cloudops.llm.task": task,
                "cloudops.llm.tier": tier.value, "cloudops.llm.turn": turn,
                "cloudops.llm.retrieved": len(retrieval or ()),
                "cloudops.llm.tool_calls": len(tool_calls or ()),
                "cloudops.llm.tools_offered": len(tools)}):
            return await self._complete(ctx, tier=tier, task=task, messages=messages,
                                        tools=tools, cache=cache, retrieval=retrieval,
                                        tool_calls=tool_calls, turn=turn)

    async def _complete(
        self,
        ctx: CallContext,
        *,
        tier: Tier,
        task: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        cache: CacheClass | None,
        retrieval: Sequence[Mapping[str, Any]] | None,
        tool_calls: Sequence[Mapping[str, Any]] | None,
        turn: int,
    ) -> GatewayReply:
        if tier is Tier.EMBED:
            raise ValueError("complete() takes the small or large tier")
        clean = [self._redacted(m, ctx) for m in messages]
        prompt = _prompt([c for c, _ in clean])
        redactions = sum(n for _, n in clean)
        trace = {"ctx": ctx, "task": task, "tier": tier, "prompt": prompt,
                 "redactions": redactions, "retrieval": retrieval, "tool_calls": tool_calls}
        if turn > self.limits.conversation_turns:
            await self._refuse(trace, ConversationTooLongError("turn ceiling reached"))
        warning = await self._check_quota_and_breaker(trace)

        cache_vector: list[float] | None = None
        if cache is not None and not tools:
            cache_vector = (await self._embed_raw(ctx, [prompt], task=f"cache:{task}"))[0]
            hit = await self._cache_lookup(ctx, tier, task, cache_vector)
            if hit is not None:
                call_id = await self._record(trace, tier_used=tier, provider="cache",
                                             deployment="cache", tokens_in=0, tokens_out=0,
                                             cost=Decimal(0), currency="USD", latency_ms=0,
                                             cache_hit=True, outcome="succeeded")
                return GatewayReply(hit, (), tier, "cache", "cache", 0, 0, Decimal(0), "USD",
                                    True, warning, call_id, prompt, 0)

        started = time.monotonic()
        completion, deployment, tier_used = await self._call(
            trace, tier, [c for c, _ in clean], tools, task
        )
        latency = int((time.monotonic() - started) * 1000)
        cost = deployment.cost(completion.tokens_in, completion.tokens_out)
        call_id = await self._record(
            trace, tier_used=tier_used, provider=deployment.provider,
            deployment=deployment.alias, tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out, cost=cost, currency=deployment.currency,
            latency_ms=latency, cache_hit=False, outcome="succeeded",
        )
        if cache_vector is not None and cache is not None and not completion.tool_calls:
            await self._cache_store(ctx, tier, task, prompt, cache_vector, completion.text, cache)
        return GatewayReply(
            completion.text, completion.tool_calls, tier_used, deployment.provider,
            deployment.alias, completion.tokens_in, completion.tokens_out, cost,
            deployment.currency, False, warning, call_id, prompt, latency,
        )

    async def embed(self, ctx: CallContext, texts: Sequence[str], *, task: str = "embed"
                    ) -> list[list[float]]:
        """Embeddings of redacted text. Counted against quotas and traced."""
        with tracer().start_as_current_span("llm.embed", kind=SpanKind.CLIENT, attributes={
                "gen_ai.operation.name": "embeddings", "cloudops.llm.task": task,
                "cloudops.llm.texts": len(texts)}):
            return await self._embed(ctx, texts, task=task)

    async def _embed(self, ctx: CallContext, texts: Sequence[str], *, task: str
                     ) -> list[list[float]]:
        trace = {"ctx": ctx, "task": task, "tier": Tier.EMBED,
                 "prompt": f"{len(texts)} text(s)", "redactions": 0,
                 "retrieval": None, "tool_calls": None}
        await self._check_quota_and_breaker(trace)
        return await self._embed_raw(ctx, texts, task=task)

    async def record_escalation(self, ctx: CallContext, task: str, reason: str,
                                tool_calls: Sequence[Mapping[str, Any]]) -> int:
        trace = {"ctx": ctx, "task": task, "tier": Tier.LARGE, "prompt": None,
                 "redactions": 0, "retrieval": None, "tool_calls": tool_calls}
        return await self._record(trace, tier_used=None, provider=None, deployment=None,
                                  tokens_in=0, tokens_out=0, cost=Decimal(0), currency="USD",
                                  latency_ms=None, cache_hit=False, outcome="escalated",
                                  reason=reason)

    # --- steps -----------------------------------------------------------------

    def _redacted(self, m: Message, ctx: CallContext) -> tuple[Message, int]:
        r = redact(m.content, ctx.personal_terms)
        return Message(m.role, r.text, m.tool_call_id, m.name, m.tool_calls), r.count

    async def _check_quota_and_breaker(self, trace: dict[str, Any]) -> bool:
        ctx: CallContext = trace["ctx"]
        lim = self.limits
        recent, trailing = _interval(lim.breaker_recent), _interval(lim.breaker_trailing)
        task = str(trace["task"])
        prefix, sub = next(((p, b) for p, b in lim.sub_budgets.items() if task.startswith(p)),
                           ("", None))
        async with self._db.transaction(ctx.scope) as conn:
            usage: tuple[Any, ...] = tuple((await conn.execute(_USAGE, {
                "prefix": prefix or task, "recent": recent, "trailing": trailing,
                "multiple": lim.breaker_multiple, "floor": lim.breaker_floor,
            })).one())
            platform_spend: tuple[Any, ...] = tuple((await conn.execute(
                _PLATFORM_SPEND, {"recent": recent, "trailing": trailing}
            )).one())
        user_used, tenant_used = int(usage[0]), int(usage[1])
        # (used, limit, refusal) for every budget this call counts against.
        budgets: list[tuple[int, float, str]] = [
            (user_used, lim.user_daily_tokens, "user daily token quota reached"),
            (tenant_used, lim.tenant_monthly_tokens, "tenant monthly token quota reached"),
        ]
        if sub is not None:
            budgets += [
                (int(usage[2]), sub.user_daily_tokens, f"{prefix} user daily quota reached"),
                (int(usage[3]), sub.tenant_monthly_fraction * lim.tenant_monthly_tokens,
                 f"{prefix} tenant monthly quota reached"),
            ]
        for used, limit, message in budgets:
            if used >= limit:
                await self._refuse(trace, QuotaExceededError(message))
        if bool(usage[4]):
            await self._refuse(trace, CircuitOpenError("tenant spend rate above threshold"))
        if self._tripped(Decimal(platform_spend[0]), Decimal(platform_spend[1])):
            await self._refuse(trace, CircuitOpenError("platform spend rate above threshold"))
        return any(used >= lim.alert_fraction * limit for used, limit, _ in budgets)

    def _tripped(self, recent: Decimal, trailing: Decimal) -> bool:
        lim = self.limits
        if recent < lim.breaker_floor:
            return False
        ratio = Decimal(lim.breaker_recent.total_seconds()) / Decimal(
            lim.breaker_trailing.total_seconds())
        expected = trailing * ratio  # trailing spend scaled to the recent window
        return recent > lim.breaker_multiple * expected

    async def _call(
        self, trace: dict[str, Any], tier: Tier, messages: Sequence[Message],
        tools: Sequence[ToolSpec], task: str,
    ) -> tuple[Completion, Deployment, Tier]:
        chain: list[tuple[Deployment, Tier]] = [(d, tier) for d in self._deployments[tier]]
        if tier is Tier.LARGE:
            chain += [(d, Tier.SMALL) for d in self._deployments[Tier.SMALL]]
        errors: list[str] = []
        for deployment, tier_used in chain:
            provider = self._providers.get(deployment.provider)
            if provider is None:
                errors.append(f"{deployment.provider}: not configured")
                continue
            try:
                completion = await provider.complete(
                    deployment.alias, messages, tools, task=task, max_tokens=MAX_TOKENS
                )
                return completion, deployment, tier_used
            except ProviderError as exc:
                errors.append(f"{deployment.provider}/{deployment.alias}: {type(exc).__name__}")
        await self._record(trace, tier_used=None, provider=None, deployment=None, tokens_in=0,
                           tokens_out=0, cost=Decimal(0), currency="USD", latency_ms=None,
                           cache_hit=False, outcome="failed", reason="; ".join(errors))
        raise AllProvidersFailedError("; ".join(errors))

    async def _embed_raw(self, ctx: CallContext, texts: Sequence[str], *, task: str
                         ) -> list[list[float]]:
        clean = [redact(t, ctx.personal_terms) for t in texts]
        trace = {"ctx": ctx, "task": task, "tier": Tier.EMBED,
                 "prompt": f"{len(texts)} text(s)", "redactions": sum(c.count for c in clean),
                 "retrieval": None, "tool_calls": None}
        errors: list[str] = []
        for deployment in self._deployments[Tier.EMBED]:
            provider = self._providers.get(deployment.provider)
            if provider is None:
                continue
            try:
                vectors = await provider.embed(deployment.alias, [c.text for c in clean])
            except ProviderError as exc:
                errors.append(f"{deployment.provider}: {type(exc).__name__}")
                continue
            tokens = sum(estimate_tokens(c.text) for c in clean)
            await self._record(trace, tier_used=Tier.EMBED, provider=deployment.provider,
                               deployment=deployment.alias, tokens_in=tokens, tokens_out=0,
                               cost=deployment.cost(tokens, 0), currency=deployment.currency,
                               latency_ms=None, cache_hit=False, outcome="succeeded")
            return vectors
        await self._record(trace, tier_used=None, provider=None, deployment=None, tokens_in=0,
                           tokens_out=0, cost=Decimal(0), currency="USD", latency_ms=None,
                           cache_hit=False, outcome="failed", reason="; ".join(errors))
        raise AllProvidersFailedError("; ".join(errors))

    async def _cache_lookup(self, ctx: CallContext, tier: Tier, task: str,
                            vector: Sequence[float]) -> str | None:
        async with self._db.transaction(ctx.scope) as conn:
            found = (await conn.execute(_CACHE_LOOKUP, {
                "embedding": _vector(vector), "scope_key": ctx.cache_scope_key,
                "tier": tier.value, "task": task,
            })).one_or_none()
        if found is None:
            return None
        row: tuple[Any, ...] = tuple(found)
        return None if float(row[1]) < CACHE_SIMILARITY else str(row[0])

    async def _cache_store(self, ctx: CallContext, tier: Tier, task: str, prompt: str,
                           vector: Sequence[float], response: str, cache: CacheClass) -> None:
        async with self._db.transaction(ctx.scope) as conn:
            await conn.execute(_CACHE_STORE, {
                "tenant_id": ctx.tenant_id, "scope_key": ctx.cache_scope_key,
                "tier": tier.value, "task": task,
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                "embedding": _vector(vector), "response": response,
                "ttl": _interval(CACHE_TTL[cache]),
            })

    async def _refuse(self, trace: dict[str, Any], refusal: GatewayRefusedError) -> None:
        await self._record(trace, tier_used=None, provider=None, deployment=None, tokens_in=0,
                           tokens_out=0, cost=Decimal(0), currency="USD", latency_ms=None,
                           cache_hit=False, outcome="refused", reason=refusal.code)
        raise refusal

    async def _record(
        self, trace: Mapping[str, Any], *, tier_used: Tier | None, provider: str | None,
        deployment: str | None, tokens_in: int, tokens_out: int, cost: Decimal, currency: str,
        latency_ms: int | None, cache_hit: bool, outcome: str, reason: str | None = None,
    ) -> int:
        ctx: CallContext = trace["ctx"]
        async with self._db.transaction(ctx.scope) as conn:
            call_id = await conn.scalar(_NEXT_CALL_ID)
            await conn.execute(_INSERT_CALL, {
                "id": call_id, "tenant_id": ctx.tenant_id, "principal": ctx.principal_id,
                "conversation_id": ctx.conversation_id, "task": trace["task"],
                "tier_requested": Tier(trace["tier"]).value,
                "tier_used": None if tier_used is None else tier_used.value,
                "provider": provider, "deployment": deployment, "prompt": trace["prompt"],
                "redactions": trace["redactions"], "retrieval": _json(trace["retrieval"]),
                "tool_calls": _json(trace["tool_calls"]), "tokens_in": tokens_in,
                "tokens_out": tokens_out, "cost": cost, "currency": currency,
                "latency_ms": latency_ms, "cache_hit": cache_hit, "outcome": outcome,
                "reason": reason, "trace_id": current_trace_id(),
            })
        tier_requested = Tier(trace["tier"]).value
        record_llm_call(task=trace["task"], tier=tier_requested, outcome=outcome,
                        provider=provider, cache_hit=cache_hit, tokens_in=tokens_in,
                        tokens_out=tokens_out, cost=cost, currency=currency,
                        latency_ms=latency_ms, refusal=reason if outcome == "refused" else None)
        span = get_current_span()
        span.set_attributes({
            "cloudops.llm.call_id": int(call_id), "cloudops.llm.outcome": outcome,
            "cloudops.llm.tier_used": "none" if tier_used is None else tier_used.value,
            "cloudops.llm.cache_hit": cache_hit, "cloudops.llm.redactions": trace["redactions"],
            "cloudops.llm.cost": str(cost), "cloudops.llm.currency": currency,
            "gen_ai.usage.input_tokens": tokens_in, "gen_ai.usage.output_tokens": tokens_out,
        })
        if deployment is not None:
            span.set_attribute("gen_ai.request.model", deployment)
        if latency_ms is not None:
            span.set_attribute("cloudops.llm.latency_ms", latency_ms)
        if reason is not None and outcome != "succeeded":
            span.set_attribute("cloudops.llm.reason", reason)
        return int(call_id)

