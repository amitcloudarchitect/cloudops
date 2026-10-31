"""AI drafting for assurance and reporting (step 17, ADR 0020).

Two narrow agents, no tools:

- ``draft_control_response`` (large tier, prompt ``control_drafter/v1``): the
  written response to one narrative control, from the evidence the platform
  collected and passages retrieved from the customer's documents, with
  citations. Its output has **no result field**: it cannot mark a control
  compliant, or anything else. A person approves every result, and the
  database refuses an approval from anyone but staff holding
  ``assurance.approve`` (migration 0013).
- ``draft_report_narrative`` (large tier, prompt ``report_narrator/v1``): two
  to four sentences on one captured section of a monthly pack, from its
  figures and tables only. A person edits or accepts it; nothing is published
  without them.

Each draft is checked deterministically, before and after the model:

- material with instruction-like content (the triage heuristic, plus "mark
  this control compliant" and the like) is not drafted at all: a person reads
  it (``instruction_like``);
- a control draft's citations must all be refs it was given (``cited_refs``),
  and its narrative must not assert a result (``claims_result``);
- every number in a narrative must appear in the section's data, as given or
  rounded (``numbers_grounded``).

A draft that fails is stored as ``declined`` with nothing in it and the reason
in ``draft_note``, so the person writes from the evidence instead. Either way,
the draft only ever reaches the ``draft_*`` columns.

**Who it runs as, and on which tenant.** ``service:drafting``, engineer role
class, ``knowledge.read`` only. The tenant comes from the request row
(``cloudops.pending_drafts``), never from a model, and each draft runs under
that one tenant's scope.

**Temporal.** ``DraftSweepWorkflow`` runs on a schedule: it lists requested
drafts and drafts each one as an activity (model calls are never workflow
code). ``KnowledgeIndexWorkflow`` does the same for the knowledge indexer:
close notes into chunks, then embeddings, tenant by tenant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

from pydantic import Field
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from cloudops_agents.base import AgentOutput, AgentSpec
from cloudops_agents.prompts import PromptTemplate
from cloudops_gateway.providers import Tier

with workflow.unsafe.imports_passed_through():
    import json
    from datetime import UTC, datetime
    from uuid import UUID

    from sqlalchemy import text

    from cloudops_agents import triage
    from cloudops_agents.base import Agent, AgentOutputError
    from cloudops_agents.prompts import DataItem
    from cloudops_agents.temporal import AGENT_RETRY, AGENT_SCHEDULE_TO_CLOSE, AGENT_START_TO_CLOSE
    from cloudops_agents.trace import DbTraceSink
    from cloudops_domain.reporting.sections import PackContent, narrative_input
    from cloudops_gateway.gateway import CallContext, Gateway, GatewayRefusedError
    from cloudops_gateway.retrieval import KnowledgeIndex, KnowledgeIndexer
    from cloudops_tenancy import Database, Role, TenantScope

TASK_QUEUE: Final = "drafting"
PRINCIPAL: Final = "service:drafting"
INDEXER_PRINCIPAL: Final = "service:knowledge-indexer"
PERMISSIONS: Final = frozenset({"knowledge.read"})
KNOWLEDGE_KINDS: Final = ["sharepoint", "close_note"]
MAX_EVIDENCE: Final = 10
MAX_PASSAGES: Final = 6
SWEEP_LIMIT: Final = 50
DB_RETRY: Final = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5)
DB_TIMEOUT: Final = timedelta(seconds=30)
INDEX_TIMEOUT: Final = timedelta(minutes=10)

Ref = str


class ControlDraft(AgentOutput):
    narrative: str = Field(min_length=1, max_length=3000)
    citations: list[str] = Field(default_factory=list, max_length=12)
    sufficiency: Literal["sufficient", "partial", "insufficient"]
    gaps: list[str] = Field(default_factory=list, max_length=5)


class SectionNarrative(AgentOutput):
    text: str = Field(min_length=1, max_length=1500)
    figures_cited: list[str] = Field(default_factory=list, max_length=12)


CONTROL_DRAFTER: Final = AgentSpec(
    name="draft_control_response",
    prompt=PromptTemplate.load("control_drafter", "v1"),
    tier=Tier.LARGE,
    output=ControlDraft,
    confidence_threshold=0.5,
)
REPORT_NARRATOR: Final = AgentSpec(
    name="draft_report_narrative",
    prompt=PromptTemplate.load("report_narrator", "v1"),
    tier=Tier.LARGE,
    output=SectionNarrative,
    confidence_threshold=0.5,
)


# --- deterministic checks -------------------------------------------------------------

# A draft describes; a person decides. Any assertion of a result is declined.
_CLAIMS_RESULT: Final = re.compile(
    r"\b(is|are|was|were|be|been|being|deemed|remains?|fully|partially|largely)\s+"
    r"(non[- ]?)?compliant\b|\bcontrol\s+(is|has\s+been|was)\s+(met|satisfied|passed)\b",
    re.IGNORECASE)
# Beyond triage's patterns: asking the drafter to record a verdict.
_VERDICT_REQUEST: Final = re.compile(
    r"\b(mark|set|record|treat|report|state|write)\b[^.\n]{0,40}\b(as\s+)?"
    r"(fully\s+)?(compliant|sufficient|passed)\b", re.IGNORECASE)


def claims_result(narrative: str) -> bool:
    return bool(_CLAIMS_RESULT.search(narrative))


def instruction_like(material: Iterable[Any]) -> bool:
    """The triage heuristic over the untrusted material a drafter would see,
    never over model output. Broad on purpose: a false positive costs a person
    a draft they write themselves."""
    for item in material:
        value = item if isinstance(item, str) else json.dumps(item, default=str)
        if triage.injection_signals({"material": value}) or _VERDICT_REQUEST.search(value):
            return True
    return False



def cited_refs(citations: Iterable[str], given: Iterable[str]) -> tuple[bool, list[str]]:
    """Every citation must be a ref the drafter was given. Returns (ok, unknown)."""
    allowed = set(given)
    unknown = sorted({c for c in citations if c not in allowed})
    return not unknown, unknown


_NUMBER: Final = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _numbers(value: str) -> list[Decimal]:
    out: list[Decimal] = []
    for match in _NUMBER.findall(value):
        try:
            out.append(Decimal(match.replace(",", "")))
        except InvalidOperation:
            continue
    return out


def numbers_grounded(narrative: str, data: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Every number in the narrative appears in the data, exactly or rounded to
    0, 1 or 2 places. A computed number (a difference, a new total) fails."""
    known: set[Decimal] = set()
    for n in _numbers(json.dumps(data, default=str)):
        known.add(n)
        for places in (0, 1, 2):
            known.add(n.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
    unknown = sorted({str(n) for n in _numbers(narrative) if n not in known})
    return not unknown, unknown


# --- activities ---------------------------------------------------------------------


@dataclass
class DraftItem:
    kind: str  # control_response | report_narrative
    id: str
    tenant_id: str  # from the request row, server-side


@dataclass
class DraftOutcome:
    kind: str
    id: str
    state: str  # drafted | declined | failed | skipped | retry_later
    reason: str | None = None
    run_id: str | None = None


def _ctx(tenant: UUID, principal: str = PRINCIPAL) -> CallContext:
    return CallContext.for_tenant(TenantScope([tenant], Role.ENGINEER, principal), tenant,
                                  PERMISSIONS)


_CLAIM_CONTROL: Final = text(
    "UPDATE cloudops.control_response SET draft_state = 'drafting'"
    " WHERE id = :id AND draft_state = 'requested' RETURNING assessment_id, control_code"
)
_CONTROL: Final = text(
    "SELECT code, framework, domain, title, requirement FROM cloudops.control WHERE code = :code"
)
_EVIDENCE: Final = text(
    "SELECT id, source_system, query_name, query_text, raw_value, retrieved_at"
    " FROM cloudops.evidence WHERE assessment_id = :a AND control_code = :code"
    " ORDER BY retrieved_at DESC LIMIT :limit"
)
_STORE_CONTROL: Final = text(
    "UPDATE cloudops.control_response SET draft_state = :state, draft_narrative = :narrative,"
    " draft_citations = CAST(:citations AS jsonb), draft_sufficiency = :sufficiency,"
    " draft_run_id = :run, drafted_at = now(), draft_note = :note"
    " WHERE id = :id AND draft_state = 'drafting'"
)
_CLAIM_NARRATIVE: Final = text(
    "UPDATE cloudops.report_narrative n SET draft_state = 'drafting' FROM cloudops.report_pack p"
    " WHERE n.id = :id AND n.draft_state = 'requested' AND p.id = n.pack_id"
    " RETURNING n.section, p.sections, p.period"
)
_STORE_NARRATIVE: Final = text(
    "UPDATE cloudops.report_narrative SET draft_state = :state, draft_text = :text,"
    " draft_run_id = :run, drafted_at = now(), draft_note = :note"
    " WHERE id = :id AND draft_state = 'drafting'"
)
# A transient failure (a provider down, a network error) puts the draft back in
# the queue before Temporal retries, so a claim never strands it.
_RELEASE: Final = {
    "control_response": text("UPDATE cloudops.control_response SET draft_state = 'requested'"
                             " WHERE id = :id AND draft_state = 'drafting'"),
    "report_narrative": text("UPDATE cloudops.report_narrative SET draft_state = 'requested'"
                             " WHERE id = :id AND draft_state = 'drafting'"),
}
_PENDING: Final = text("SELECT kind, id, tenant_id FROM cloudops.pending_drafts(:limit)")
_TENANTS: Final = text("SELECT tenant_id FROM cloudops.knowledge_tenants()")


def control_data(control: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]],
                 passages: Sequence[Mapping[str, Any]]) -> tuple[list[DataItem], list[Ref]]:
    """The drafter's data sections, the control first (it keys the case), and
    the refs it may cite."""
    items = [DataItem("control_library", dict(control), ref=str(control["code"]))]
    refs: list[Ref] = []
    for e in evidence:
        ref = f"evidence:{e['id']}"
        refs.append(ref)
        items.append(DataItem("evidence_store", {k: v for k, v in e.items() if k != "id"},
                              ref=ref))
    for p in passages:
        ref = f"chunk:{p['chunk_id']}"
        refs.append(ref)
        items.append(DataItem("knowledge", {"citation": p["label"], "text": p["text"]}, ref=ref))
    return items, refs


class DraftingActivities:
    def __init__(self, db: Database, gateway: Gateway) -> None:
        self._db = db
        trace = DbTraceSink(db)
        self._index = KnowledgeIndex(db, gateway)
        self._indexer = KnowledgeIndexer(db, gateway)
        self._control = Agent(CONTROL_DRAFTER, gateway, None, trace)
        self._narrator = Agent(REPORT_NARRATOR, gateway, None, trace)

    @activity.defn
    async def pending_drafts(self, limit: int) -> list[DraftItem]:
        async with self._db.transaction(TenantScope.for_resolution(PRINCIPAL)) as conn:
            rows = [tuple(r) for r in await conn.execute(_PENDING, {"limit": limit})]
        return [DraftItem(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    @activity.defn
    async def draft_control(self, item: DraftItem) -> DraftOutcome:
        ctx = _ctx(UUID(item.tenant_id))
        async with self._db.transaction(ctx.scope) as conn:
            claimed = (await conn.execute(_CLAIM_CONTROL, {"id": item.id})).one_or_none()
            if claimed is None:
                return DraftOutcome(item.kind, item.id, "skipped", "not requested")
            assessment, code = tuple(claimed)
            control = dict((await conn.execute(_CONTROL, {"code": code})).mappings().one())
            evidence = [dict(r) for r in (await conn.execute(_EVIDENCE, {
                "a": assessment, "code": code, "limit": MAX_EVIDENCE})).mappings()]
        try:
            hits = await self._index.search(ctx, str(control["requirement"])[:300],
                                            kinds=KNOWLEDGE_KINDS, limit=MAX_PASSAGES)
            passages = [{"chunk_id": h.chunk_id, "label": h.citation()["label"],
                         "text": h.body[:1200], "citation": h.citation()}
                        for h in hits if h.origin == "tenant"]
            data, refs = control_data(control, evidence, passages)
            if instruction_like([*(e["raw_value"] for e in evidence),
                                 *(p["text"] for p in passages)]):
                return await self._store_control(ctx, item, "declined",
                                                 "instruction_like_content")
            result = await self._control.run(
                ctx, request=f"Draft the response to control {code}.", data=data,
                retrieval_ids=[f"chunk:{p['chunk_id']}" for p in passages])
        except (AgentOutputError, GatewayRefusedError) as exc:
            return await self._store_control(ctx, item, "failed", type(exc).__name__)
        except Exception:
            await self._release(ctx, item)
            raise
        if result.output is None:
            return await self._store_control(ctx, item, "declined", "low_confidence",
                                             run=str(result.run_id))
        ok, _ = cited_refs(result.output.citations, refs)
        if not ok:
            return await self._store_control(ctx, item, "declined", "ungrounded_citations",
                                             run=str(result.run_id))
        if claims_result(result.output.narrative):
            return await self._store_control(ctx, item, "declined", "claims_result",
                                             run=str(result.run_id))
        by_ref: dict[str, dict[str, Any]] = {f"chunk:{p['chunk_id']}": p["citation"]
                                             for p in passages}
        by_ref.update({f"evidence:{e['id']}": {
            "evidence_id": str(e["id"]), "query_name": e["query_name"],
            "source_system": e["source_system"], "retrieved_at": e["retrieved_at"].isoformat(),
            "label": f"{e['query_name']} at {e['retrieved_at']:%Y-%m-%d %H:%M} UTC"}
            for e in evidence})
        citations = [{"ref": c, **by_ref[c]} for c in result.output.citations]
        return await self._store_control(
            ctx, item, "drafted", None, run=str(result.run_id),
            narrative=result.output.narrative, citations=citations,
            sufficiency=result.output.sufficiency)

    async def _store_control(self, ctx: CallContext, item: DraftItem, state: str,
                             reason: str | None, *, run: str | None = None,
                             narrative: str | None = None,
                             citations: list[dict[str, Any]] | None = None,
                             sufficiency: str | None = None) -> DraftOutcome:
        async with self._db.transaction(ctx.scope) as conn:
            await conn.execute(_STORE_CONTROL, {
                "id": item.id, "state": state, "narrative": narrative,
                "citations": None if citations is None else json.dumps(citations),
                "sufficiency": sufficiency, "run": run, "note": reason})
        return DraftOutcome(item.kind, item.id, state, reason, run)

    @activity.defn
    async def draft_narrative(self, item: DraftItem) -> DraftOutcome:
        ctx = _ctx(UUID(item.tenant_id))
        async with self._db.transaction(ctx.scope) as conn:
            claimed = (await conn.execute(_CLAIM_NARRATIVE, {"id": item.id})).one_or_none()
        if claimed is None:
            return DraftOutcome(item.kind, item.id, "skipped", "not requested")
        key, sections, period = tuple(claimed)
        content = PackContent.from_json(ctx.tenant_id, {
            "tenant_name": "", "period": period.isoformat(), "contract_currency": "XXX",
            "data_as_of": datetime.now(UTC).isoformat(), "sections": sections})
        data = {"period": f"{period:%B %Y}", **narrative_input(content.section(key))}
        if instruction_like([data["tables"]]):
            return await self._store_narrative(ctx, item, "declined", "instruction_like_content")
        try:
            result = await self._narrator.run(
                ctx, request=f"Draft the commentary for the {key.replace('_', ' ')} section.",
                data=[DataItem("report_section", data, ref=f"{key}:{period.isoformat()}")])
        except (AgentOutputError, GatewayRefusedError) as exc:
            return await self._store_narrative(ctx, item, "failed", type(exc).__name__)
        except Exception:
            await self._release(ctx, item)
            raise
        if result.output is None:
            return await self._store_narrative(ctx, item, "declined", "low_confidence",
                                               run=str(result.run_id))
        ok, _ = numbers_grounded(result.output.text, data)
        keys = {f["key"] for f in data["figures"]}
        if not ok or not set(result.output.figures_cited) <= keys:
            return await self._store_narrative(ctx, item, "declined", "ungrounded_numbers",
                                               run=str(result.run_id))
        return await self._store_narrative(ctx, item, "drafted", None, run=str(result.run_id),
                                           draft=result.output.text)

    async def _store_narrative(self, ctx: CallContext, item: DraftItem, state: str,
                               reason: str | None, *, run: str | None = None,
                               draft: str | None = None) -> DraftOutcome:
        async with self._db.transaction(ctx.scope) as conn:
            await conn.execute(_STORE_NARRATIVE, {"id": item.id, "state": state, "text": draft,
                                                  "run": run, "note": reason})
        return DraftOutcome(item.kind, item.id, state, reason, run)

    async def _release(self, ctx: CallContext, item: DraftItem) -> None:
        async with self._db.transaction(ctx.scope) as conn:
            await conn.execute(_RELEASE[item.kind], {"id": item.id})

    @activity.defn
    async def knowledge_tenants(self) -> list[str]:
        async with self._db.transaction(TenantScope.for_resolution(INDEXER_PRINCIPAL)) as conn:
            return [str(r[0]) for r in await conn.execute(_TENANTS)]

    @activity.defn
    async def index_tenant(self, tenant_id: str) -> int:
        """Close notes into chunks, then embeddings, under this tenant's scope."""
        ctx = _ctx(UUID(tenant_id), INDEXER_PRINCIPAL)
        await self._indexer.index_close_notes(ctx)
        return await self._indexer.embed_pending(ctx)


def activities(acts: DraftingActivities) -> list[Any]:
    return [acts.pending_drafts, acts.draft_control, acts.draft_narrative,
            acts.knowledge_tenants, acts.index_tenant]


# --- workflows -------------------------------------------------------------------------


@workflow.defn
class DraftSweepWorkflow:
    """Draft everything requested, one activity per draft. Scheduled; safe to
    overlap, since a draft is claimed before it is drafted."""

    @workflow.run
    async def run(self, limit: int = SWEEP_LIMIT) -> list[DraftOutcome]:
        items = await workflow.execute_activity_method(
            DraftingActivities.pending_drafts, limit, start_to_close_timeout=DB_TIMEOUT,
            retry_policy=DB_RETRY)
        outcomes: list[DraftOutcome] = []
        for item in items:
            method = (DraftingActivities.draft_control if item.kind == "control_response"
                      else DraftingActivities.draft_narrative)
            try:
                outcomes.append(await workflow.execute_activity_method(
                    method, item, start_to_close_timeout=AGENT_START_TO_CLOSE,
                    schedule_to_close_timeout=AGENT_SCHEDULE_TO_CLOSE, retry_policy=AGENT_RETRY))
            except ActivityError:
                # Retries exhausted (a provider down): the draft was released back
                # to the queue, and the next sweep tries again. One draft never
                # blocks the others.
                outcomes.append(DraftOutcome(item.kind, item.id, "retry_later"))
        return outcomes


@workflow.defn
class KnowledgeIndexWorkflow:
    """Index every tenant's pending knowledge, one tenant per activity."""

    @workflow.run
    async def run(self) -> dict[str, int]:
        tenants = await workflow.execute_activity_method(
            DraftingActivities.knowledge_tenants, start_to_close_timeout=DB_TIMEOUT,
            retry_policy=DB_RETRY)
        done: dict[str, int] = {}
        for tenant in tenants:
            done[tenant] = await workflow.execute_activity_method(
                DraftingActivities.index_tenant, tenant, start_to_close_timeout=INDEX_TIMEOUT,
                retry_policy=AGENT_RETRY)
        return done


WORKFLOWS: Final = [DraftSweepWorkflow, KnowledgeIndexWorkflow]
SCHEDULES: Final = (
    ("drafting-sweep", DraftSweepWorkflow, timedelta(minutes=1)),
    ("knowledge-index", KnowledgeIndexWorkflow, timedelta(minutes=15)),
)
