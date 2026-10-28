"""The enricher: stage 3 of the remediation pipeline (HLD, Pipeline).

Large tier, three read-only tools: ``get_resource_context``, ``search_tickets``,
and ``search_knowledge`` (step 06's hybrid retrieval, under the conversation's
scope). It assembles a structured context bundle; it plans nothing and can
act on nothing. No write tool is on its allowlist, and the registry refuses
write tools anyway.

Every reference the bundle cites must appear in a tool result it saw.
``triage.after_enrichment`` checks that deterministically, and anything
ungrounded goes to a human.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final, Literal

from pydantic import Field

from cloudops_agents.base import AgentOutput, AgentSpec
from cloudops_agents.prompts import PromptTemplate
from cloudops_gateway.gateway import CallContext
from cloudops_gateway.providers import Tier
from cloudops_gateway.registry import Source, Tool, ToolContext, ToolKind, ToolResult
from cloudops_gateway.retrieval import KnowledgeIndex

Missing = Literal["resource", "recent_changes", "telemetry", "prior_resolutions", "runbook"]
KNOWLEDGE_KINDS: Final = ["close_note", "runbook", "knowledge"]


class EnrichmentBundle(AgentOutput):
    resource_found: bool
    resource_summary: str | None = Field(default=None, max_length=600)
    related_tickets: list[str] = Field(default_factory=list, max_length=10)
    prior_resolutions: list[str] = Field(default_factory=list, max_length=10)
    runbook_candidates: list[str] = Field(default_factory=list, max_length=5)
    missing_context: list[Missing] = Field(default_factory=list, max_length=5)
    summary: str = Field(min_length=1, max_length=800)


ENRICHER: Final = AgentSpec(
    name="enrich_ticket",
    prompt=PromptTemplate.load("ticket_enricher", "v1"),
    tier=Tier.LARGE,
    output=EnrichmentBundle,
    confidence_threshold=0.6,
    tools=frozenset({"get_resource_context", "search_tickets", "search_knowledge"}),
)


def knowledge_tool(index: KnowledgeIndex) -> Tool:
    """``search_knowledge`` as a registry tool. It runs under the tool context's
    scope, which the registry built from the conversation, so the tenant
    predicate in the retrieval SQL is the conversation's, whatever the query says."""

    async def handler(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        (tenant,) = ctx.scope.tenant_ids
        call = CallContext(ctx.scope, tenant, ctx.permissions)
        hits = await index.search(call, str(args["query"]), kinds=KNOWLEDGE_KINDS,
                                  limit=int(args.get("limit", 5)))
        return ToolResult(
            {"results": [{"ref": h.source_ref, "kind": h.source_kind, "title": h.title,
                          "excerpt": h.body[:500], "origin": h.origin} for h in hits]},
            tuple(Source("knowledge", f"chunk:{h.chunk_id}", datetime.now(UTC)) for h in hits),
        )

    return Tool(
        name="search_knowledge",
        description="Search the current customer's knowledge (prior resolutions, runbooks) "
                    "and Getronics-wide runbooks.",
        parameters={
            "type": "object", "additionalProperties": False, "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 2, "maxLength": 300},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
        },
        permission="knowledge.read", kind=ToolKind.READ, handler=handler, timeout_seconds=15,
    )
