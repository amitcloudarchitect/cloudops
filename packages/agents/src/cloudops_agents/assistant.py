"""AMARA, the read-only assistant (HLD, "How a question is answered"; ADR 0029).

    engine = Assistant(db, gateway, registry, trace)
    outcome = await engine.answer(question, on_stage=save_stage)

One turn, in a fixed order. The code decides every step; the models fill in
content at three of them (CLAUDE.md rule 3):

1. **Guards, deterministic.** A question naming another customer the caller can
   see, or asking to compare customers, is refused before any model runs. So is a
   question asking for more than one tenant: a conversation is bound to exactly one
   (``CallContext.for_tenant``), so no tool could reach a second anyway.
2. **Intent, small tier** (``assistant_classifier``, no tools): account question,
   platform help, comparison, action or other. Comparison and action are refused;
   other is declined. A help-only conversation (no customer chosen) asking about an
   account is told to choose one.
3. **Retrieval.** The question is embedded and matched against knowledge chunks,
   hybrid (pgvector and full-text, fused by reciprocal rank) with the tenant
   predicate inside the SQL (``KnowledgeIndex.search``), then re-ranked by the small
   tier (``assistant_reranker``). Help pages are always searched; a customer scope
   reads only the customer help pages (the table's ``audience`` policy). Customer
   documents and close notes only for staff holding ``knowledge.read``.
4. **Context.** The screen the question was asked from is a hint: its record is
   looked up through the same read-only tools, under the conversation's scope. A
   ticket or resource of another tenant is "not found" and is simply left out.
5. **Answer.** Platform help: the small tier, no tools, cacheable
   (``assistant_help``). Account questions: the large tier with the read-only tool
   allowlist, cut further to the caller's permissions by the registry
   (``assistant_answerer``). Both return claims, each citing the refs it rests on.
6. **Check, deterministic.** Every claim cites only refs it was given or a tool
   returned; every number in the answer appears in that evidence. Otherwise the
   answer becomes "I don't know". The answer carries its sources with their as-of
   times, and marks modelled values.

Never a write tool, never a credential, never a raw tenant id in a prompt. Ticket
text, documents, tags and history travel as delimited data sections.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloudops_agents.base import (
    Agent,
    AgentError,
    AgentEscalationError,
    AgentOutput,
    AgentResult,
    AgentSpec,
    GatewayLike,
    RegistryLike,
    TraceSink,
)
from cloudops_agents.prompts import DataItem, PromptTemplate
from cloudops_gateway.gateway import AllProvidersFailedError, CallContext, GatewayRefusedError
from cloudops_gateway.providers import Tier, ToolCallRequest, estimate_tokens
from cloudops_gateway.retrieval import Hit, KnowledgeIndex

# --- agents ---------------------------------------------------------------------------

Intent = Literal["account", "platform_help", "comparison", "action", "other"]


class IntentOutput(AgentOutput):
    intent: Intent
    other_customer: bool


class RerankOutput(AgentOutput):
    order: list[int] = Field(max_length=20)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=500)
    sources: list[str] = Field(min_length=1, max_length=6)


class AnswerOutput(AgentOutput):
    status: Literal["answered", "dont_know", "refused"]
    answer: str = Field(min_length=1, max_length=1500)
    claims: list[Claim] = Field(default_factory=list, max_length=10)


# The tools an account answer may use: all read-only. The registry offers only
# those whose permission the caller holds, so a customer never sees get_margin.
ANSWER_TOOLS: Final = frozenset({
    "get_ticket", "search_tickets", "get_sla_summary", "get_resource_context",
    "get_cost_trend", "get_open_vulnerabilities", "get_recent_changes", "get_margin",
})

CLASSIFIER: Final = AgentSpec(
    name="assistant.classify", prompt=PromptTemplate.load("assistant_classifier", "v1"),
    tier=Tier.SMALL, output=IntentOutput, confidence_threshold=0.5,
)
RERANKER: Final = AgentSpec(
    name="assistant.rerank", prompt=PromptTemplate.load("assistant_reranker", "v1"),
    tier=Tier.SMALL, output=RerankOutput, confidence_threshold=0.0,
)
ANSWERER: Final = AgentSpec(
    name="assistant.answer", prompt=PromptTemplate.load("assistant_answerer", "v1"),
    tier=Tier.LARGE, output=AnswerOutput, confidence_threshold=0.0, tools=ANSWER_TOOLS,
)
HELPER: Final = AgentSpec(
    name="assistant.help", prompt=PromptTemplate.load("assistant_help", "v1"),
    tier=Tier.SMALL, output=AnswerOutput, confidence_threshold=0.0,
)
AGENTS: Final = (CLASSIFIER, RERANKER, ANSWERER, HELPER)

# --- limits ------------------------------------------------------------------------------

CANDIDATES: Final = 12  # fused hits handed to the re-ranker
PASSAGES: Final = 5  # passages kept for the answer
PASSAGE_CHARS: Final = 2400
HISTORY_TURNS: Final = 4
HISTORY_CHARS: Final = 600
CONTEXT_TOKENS: Final = 6000  # data sections, before tools: the context budget
HELP_KINDS_ALL: Final = ("platform_help",)
HELP_KINDS_STAFF: Final = ("platform_help", "platform_help_staff")
TENANT_KINDS: Final = ("sharepoint", "close_note")

Stage = Literal["understanding", "retrieving", "looking_up", "composing", "checking"]
Status = Literal["answered", "dont_know", "refused", "failed"]

# Deterministic refusals: what the person reads, by reason code.
MESSAGES: Final[Mapping[str, str]] = {
    "cross_customer": "I can only answer about one customer account at a time, so I can't "
                      "compare customers or answer about another customer. Ask me about this "
                      "account on its own.",
    "read_only": "I'm read-only: I can't change anything, raise or update tickets, or run "
                 "fixes. I can tell you what the platform shows about it; for changes, use "
                 "the service desk or ask your Getronics contact.",
    "needs_customer": "That's a question about a customer account. Open one of your "
                      "customers, or choose one in this panel, and ask again.",
    "out_of_scope": "I can help with this account's services and with how the platform "
                    "works. I can't help with that question.",
    "ungrounded": "I don't know: I couldn't back an answer with the platform's data, so I "
                  "won't guess.",
    "tool_ceiling": "I don't know: answering that needed more look-ups than I'm allowed. Try "
                    "a narrower question.",
    "quota_exceeded": "You've reached the assistant's usage limit for now. Try again later.",
    "spend_circuit_open": "The assistant is paused for a short while. Try again later.",
    "unavailable": "The assistant couldn't answer just now. Try again in a moment.",
}

_COMPARE: Final = re.compile(
    r"\b(compar\w*|versus|vs\.?|benchmark\w*|rank\w*|league table|"
    r"other customers?|another customer|all (?:of )?(?:the |our |my )?customers|"
    r"every customer|each customer|across customers|which customer)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScreenHint:
    """Where the question was asked: a screen and, maybe, a record on it. A hint
    only; the record is looked up under the conversation's scope."""

    screen: str
    record_type: Literal["ticket", "resource"] | None = None
    record_id: str | None = None


@dataclass(frozen=True)
class Question:
    ctx: CallContext  # narrowed to the conversation's one tenant
    mode: Literal["account", "help"]
    text: str
    audience: Literal["staff", "customer"]
    subject: str  # the account's display name; "the platform" in help mode
    other_tenant_names: Sequence[str] = ()  # names the caller can see, besides this one
    hint: ScreenHint | None = None
    history: Sequence[tuple[str, str]] = ()  # (question, answer), oldest first


@dataclass(frozen=True)
class AnswerSource:
    ref: str
    label: str
    source_system: str
    as_of: datetime | None
    is_modelled: bool = False
    url: str | None = None


@dataclass
class Outcome:
    status: Status
    text: str
    reason: str | None = None
    claims: list[dict[str, Any]] = field(default_factory=list)
    sources: list[AnswerSource] = field(default_factory=list)
    agent_run_ids: list[str] = field(default_factory=list)
    retrieval_ids: list[str] = field(default_factory=list)

    @classmethod
    def refusal(cls, reason: str, status: Status = "refused", **kw: Any) -> Outcome:
        return cls(status, MESSAGES[reason], reason, **kw)


StageSink = Callable[[Stage], Awaitable[None]]


async def _no_stage(_: Stage) -> None:
    return None


# --- deterministic checks -------------------------------------------------------------


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def names_other_customer(question: str, others: Sequence[str]) -> bool:
    """A whole-word match of any other customer the caller can see."""
    q = f" {_key(question)} "
    return any(k and f" {k} " in q for k in (_key(n) for n in others) if len(k) >= 3)


def asks_to_compare(question: str) -> bool:
    return bool(_COMPARE.search(question))


_NUMBER: Final = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _numbers(value: str) -> list[Decimal]:
    out: list[Decimal] = []
    for match in _NUMBER.findall(value):
        try:
            out.append(Decimal(match.replace(",", "")))
        except InvalidOperation:
            continue
    return out


def ungrounded_numbers(answer: str, evidence: Sequence[str]) -> list[str]:
    """Numbers in the answer that appear nowhere in the evidence: as given,
    rounded to 0-2 places, or (a fraction) as a percentage. A computed number
    (a sum, a difference) is ungrounded."""
    known: set[Decimal] = set()
    for text in evidence:
        for n in _numbers(text):
            for v in (n, n * 100):
                known.add(v.normalize())
                for places in (0, 1, 2):
                    known.add(v.quantize(Decimal(1).scaleb(-places),
                                         rounding=ROUND_HALF_UP).normalize())
    return sorted({str(n) for n in _numbers(answer) if n.normalize() not in known})


def _tool_sources(content: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return []
    sources = parsed.get("sources") if isinstance(parsed, dict) else None
    modelled = '"is_modelled": true' in content
    out: list[dict[str, Any]] = []
    for s in sources or []:
        if isinstance(s, dict) and isinstance(s.get("ref"), str):
            out.append({**s, "is_modelled": modelled})
    return out


def _as_of(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


_SYSTEM_LABELS: Final = {
    "servicenow": "Service desk", "qualys": "Vulnerability scans",
    "platform_inventory": "Inventory", "cost_fact": "Cloud cost",
    "customer_price": "Price charged", "margin_snapshot": "Margin",
}


def _hit_source(ref: str, hit: Hit) -> AnswerSource:
    if hit.source_kind.startswith("platform_help"):
        return AnswerSource(ref, f"Help: {hit.title or 'platform help'}", "platform_help", None)
    c = hit.citation()
    return AnswerSource(ref, str(c["label"]), hit.source_kind, None, url=c["web_url"])


# --- the engine ------------------------------------------------------------------------


class Assistant:
    def __init__(self, gateway: GatewayLike, registry: RegistryLike, index: KnowledgeIndex,
                 trace: TraceSink) -> None:
        self._gateway = gateway
        self._registry = registry
        self._index = index
        self._trace = trace

    async def answer(self, q: Question, on_stage: StageSink = _no_stage) -> Outcome:
        runs: list[str] = []
        try:
            return await self._answer(q, on_stage, runs)
        except GatewayRefusedError as exc:
            reason = exc.code if exc.code in MESSAGES else "unavailable"
            return Outcome.refusal(reason, "failed", agent_run_ids=runs)
        except AgentEscalationError:
            return Outcome.refusal("tool_ceiling", "dont_know", agent_run_ids=runs)
        except (AgentError, AllProvidersFailedError):
            return Outcome.refusal("unavailable", "failed", agent_run_ids=runs)

    async def _answer(self, q: Question, on_stage: StageSink, runs: list[str]) -> Outcome:
        # 1. Guards: before any model sees the question.
        if names_other_customer(q.text, q.other_tenant_names) or asks_to_compare(q.text):
            return Outcome.refusal("cross_customer")

        # 2. Intent.
        await on_stage("understanding")
        slots = {"audience": "Getronics staff" if q.audience == "staff" else "a customer user",
                 "subject": q.subject}
        intent = await self._run(CLASSIFIER, q, runs, slots=slots, request=q.text)
        decided: IntentOutput | None = intent.output
        if decided is not None and (decided.other_customer or decided.intent == "comparison"):
            return Outcome.refusal("cross_customer", agent_run_ids=runs)
        if decided is not None and decided.intent == "action":
            return Outcome.refusal("read_only", agent_run_ids=runs)
        if decided is not None and decided.intent == "other":
            return Outcome.refusal("out_of_scope", agent_run_ids=runs)
        help_only = q.mode == "help" or (decided is not None
                                         and decided.intent == "platform_help")
        if q.mode == "help" and decided is not None and decided.intent == "account":
            return Outcome.refusal("needs_customer", agent_run_ids=runs)

        # 3. Retrieval.
        await on_stage("retrieving")
        kinds: list[str] = list(HELP_KINDS_STAFF if q.audience == "staff" else HELP_KINDS_ALL)
        if (not help_only and q.audience == "staff"
                and "knowledge.read" in q.ctx.permissions):
            kinds += TENANT_KINDS
        hits = await self._index.search(q.ctx, q.text, kinds=kinds, limit=CANDIDATES,
                                        task="assistant.retrieve",
                                        tenant=any(k in TENANT_KINDS for k in kinds))
        hits = await self._rerank(q, hits, runs, slots)
        retrieval_ids = [str(h.chunk_id) for h in hits]

        data: list[DataItem] = []
        sources: dict[str, AnswerSource] = {}
        for i, hit in enumerate(hits, start=1):
            ref = f"P{i}"
            data.append(DataItem("knowledge", {"title": hit.title, "text":
                                               hit.body[:PASSAGE_CHARS]}, ref=ref))
            sources[ref] = _hit_source(ref, hit)

        # 4. Context from the screen, through the tools, under this scope.
        if not help_only and q.hint is not None:
            await on_stage("looking_up")
            for item, found in await self._context(q):
                data.insert(0, item)
                for s in found:
                    sources[s["ref"]] = AnswerSource(
                        s["ref"], _SYSTEM_LABELS.get(s["source_system"], s["source_system"]),
                        s["source_system"], _as_of(s.get("as_of")),
                        bool(s.get("is_modelled")))
        if q.history and not help_only:
            data.append(DataItem("conversation_history", {"earlier_turns": [
                {"question": qq[:HISTORY_CHARS], "answer": aa[:HISTORY_CHARS]}
                for qq, aa in q.history[-HISTORY_TURNS:]]}))
        data = _within_budget(data)

        # 5. Answer.
        await on_stage("composing")
        result = await self._run(HELPER if help_only else ANSWERER, q, runs, slots=slots,
                                 request=q.text, data=data, retrieval_ids=retrieval_ids,
                                 cache="static" if help_only else None)
        output: AnswerOutput | None = result.output

        # 6. Check.
        await on_stage("checking")
        for content in result.evidence:
            for s in _tool_sources(content):
                sources.setdefault(s["ref"], AnswerSource(
                    s["ref"], _SYSTEM_LABELS.get(str(s.get("source_system")),
                                                 str(s.get("source_system"))),
                    str(s.get("source_system")), _as_of(s.get("as_of")),
                    bool(s.get("is_modelled"))))
        done: dict[str, Any] = {"agent_run_ids": runs, "retrieval_ids": retrieval_ids}
        if output is None:
            return Outcome.refusal("ungrounded", "dont_know", **done)
        if output.status == "refused":
            return Outcome("refused", output.answer, "model_refused", agent_run_ids=runs,
                           retrieval_ids=retrieval_ids)
        if output.status == "dont_know":
            return Outcome("dont_know", output.answer, "not_in_data", agent_run_ids=runs,
                           retrieval_ids=retrieval_ids)
        cited = {r for c in output.claims for r in c.sources}
        evidence = [json.dumps(d.content, default=str) if not isinstance(d.content, str)
                    else d.content for d in data] + list(result.evidence)
        if (not output.claims or not cited <= set(sources)
                or ungrounded_numbers(output.answer, evidence)
                or any(ungrounded_numbers(c.text, evidence) for c in output.claims)):
            return Outcome.refusal("ungrounded", "dont_know", **done)
        return Outcome(
            "answered", output.answer, None,
            claims=[{"text": c.text, "sources": c.sources} for c in output.claims],
            sources=[sources[r] for r in sorted(cited, key=list(sources).index)], **done)

    async def _run[O: AgentOutput](
        self, spec: AgentSpec[O], q: Question, runs: list[str], *, slots: Mapping[str, str],
        request: str, data: Sequence[DataItem] = (), retrieval_ids: Sequence[str] = (),
        cache: Literal["static"] | None = None,
    ) -> AgentResult[O]:
        agent = Agent(replace(spec, slots=dict(slots)), self._gateway,
                      self._registry if spec.tools else None, self._trace)
        result = await agent.run(q.ctx, request=request, data=data,
                                 retrieval_ids=retrieval_ids, cache=cache)
        runs.append(str(result.run_id))
        return result

    async def _rerank(self, q: Question, hits: list[Hit], runs: list[str],
                      slots: Mapping[str, str]) -> list[Hit]:
        if len(hits) <= 1:
            return hits[:PASSAGES]
        data = [DataItem("passage", {"title": h.title, "text": h.body[:800]}, ref=str(i))
                for i, h in enumerate(hits)]
        try:
            ranked = await self._run(RERANKER, q, runs, slots=slots, request=q.text,
                                     data=data)
        except (AgentError, AllProvidersFailedError):
            return hits[:PASSAGES]  # fused order stands
        order = ranked.output.order if ranked.output is not None else []
        picked = [hits[i] for i in dict.fromkeys(order) if 0 <= i < len(hits)]
        return picked[:PASSAGES]

    async def _context(self, q: Question) -> list[tuple[DataItem, list[dict[str, Any]]]]:
        hint = q.hint
        if hint is None or hint.record_type is None or not hint.record_id:
            return []
        name, args = (("get_ticket", {"number": hint.record_id}) if hint.record_type == "ticket"
                      else ("get_resource_context", {"resource": hint.record_id}))
        outcome = await self._registry.invoke(
            ToolCallRequest(f"context-{name}", name, json.dumps(args)), q.ctx)
        if not outcome.ok:
            return []
        parsed = json.loads(outcome.content)
        if not parsed.get("result", {}).get("found"):
            return []  # outside scope, or never existed: the hint is dropped
        found = _tool_sources(outcome.content)
        ref = found[0]["ref"] if found else f"context:{name}"
        return [(DataItem(f"screen:{hint.screen}", parsed["result"], ref=ref), found)]


def _within_budget(data: list[DataItem]) -> list[DataItem]:
    """Drop the lowest-ranked passages until the data sections fit the budget.
    Context and history are kept; passages come last in rank order."""
    def size(items: Sequence[DataItem]) -> int:
        return sum(estimate_tokens(i.content if isinstance(i.content, str)
                                   else json.dumps(i.content, default=str)) for i in items)

    kept = list(data)
    while size(kept) > CONTEXT_TOKENS:
        passages = [i for i, d in enumerate(kept) if d.source == "knowledge"]
        if not passages:
            break
        kept.pop(passages[-1])
    return kept
