"""Stages 1 to 3 of the remediation pipeline as a Temporal workflow (HLD, Pipeline).

    TicketTriageWorkflow: intake -> classify -> enrich -> ready_for_planning | human

The workflow holds only the sequence and the decisions (``triage.py``, pure
and deterministic). Everything that reads the database or calls a model is
an activity, so replays never call a model (CLAUDE.md rule 3). Stage 4
(plan) and everything after it are not built: they need the security review
first.

**Who it runs as.** A service principal, ``service:triage``, with the
read-only permissions the stages need, scoped to the one tenant the ticket was
ingested for. The tenant comes from the request (the connector resolved it
server-side), never from a model. The classifier's "customer" is only
cross-checked against it.

**Failures.** Invalid agent output or a gateway refusal (quota, breaker) is
returned as a status and routed to a human; it is not retried. Transient
faults (a provider down, a network error) are retried by Temporal under the
agent retry policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from sqlalchemy import text

    from cloudops_agents import triage
    from cloudops_agents.base import Agent, AgentOutputError
    from cloudops_agents.classifier import CLASSIFIER, TicketTriage, ticket_data
    from cloudops_agents.enricher import ENRICHER, EnrichmentBundle
    from cloudops_agents.prompts import DataItem
    from cloudops_agents.temporal import AGENT_RETRY, AGENT_SCHEDULE_TO_CLOSE, AGENT_START_TO_CLOSE
    from cloudops_agents.trace import DbTraceSink
    from cloudops_gateway.gateway import CallContext, Gateway, GatewayRefusedError
    from cloudops_gateway.registry import ToolRegistry
    from cloudops_tenancy import Database, Role, TenantScope

TASK_QUEUE: Final = "remediation-triage"
PRINCIPAL: Final = "service:triage"
PERMISSIONS: Final = frozenset({"inventory.read", "tickets.read", "knowledge.read"})
DB_RETRY: Final = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5)
DB_TIMEOUT: Final = timedelta(seconds=30)


@dataclass
class TriageRequest:
    tenant_id: str  # resolved server-side when the ticket was ingested
    number: str


@dataclass
class AgentStep:
    status: str  # answered | declined | invalid_output | refused
    output: dict[str, Any] | None = None
    run_id: str | None = None
    cost: str = "0"
    evidence: list[str] = field(default_factory=list)


@dataclass
class ClassifyRequest:
    request: TriageRequest
    ticket: dict[str, Any]


@dataclass
class CiRequest:
    tenant_id: str
    ci: str


@dataclass
class EnrichRequest:
    request: TriageRequest
    ticket: dict[str, Any]
    classification: dict[str, Any]


@dataclass
class TriageOutcome:
    number: str
    route: str  # ready_for_planning | human
    stage: int  # the last stage that ran: 1, 2 or 3
    reasons: list[str]
    classification: dict[str, Any] | None = None
    resource_id: str | None = None
    enrichment: dict[str, Any] | None = None
    injection_signals: list[str] = field(default_factory=list)
    agent_runs: list[str] = field(default_factory=list)
    cost: str = "0"
    evidence: list[str] = field(default_factory=list)


def _ctx(request: TriageRequest) -> CallContext:
    tenant = UUID(request.tenant_id)
    return CallContext.for_tenant(TenantScope([tenant], Role.ENGINEER, PRINCIPAL), tenant,
                                  PERMISSIONS)


class TriageActivities:
    def __init__(self, db: Database, gateway: Gateway, registry: ToolRegistry) -> None:
        self._db = db
        trace = DbTraceSink(db)
        self._classifier = Agent(CLASSIFIER, gateway, None, trace)
        self._enricher = Agent(ENRICHER, gateway, registry, trace)

    @activity.defn
    async def load_ticket(self, req: TriageRequest) -> dict[str, Any] | None:
        ctx = _ctx(req)
        async with self._db.transaction(ctx.scope) as conn:
            row = (await conn.execute(text(
                "SELECT number, kind, state, company_raw, short_description, configuration_item,"
                " priority, close_notes FROM cloudops.work_item WHERE number = :n"
                " ORDER BY opened_at DESC LIMIT 1"), {"n": req.number})).one_or_none()
        if row is None:
            return None
        keys = ("number", "kind", "state", "company", "short_description", "configuration_item",
                "priority", "close_notes")
        return dict(zip(keys, tuple(row), strict=True))

    @activity.defn
    async def tenant_names(self, req: TriageRequest) -> list[str]:
        ctx = _ctx(req)
        async with self._db.transaction(ctx.scope) as conn:
            names = [str(r[0]) for r in await conn.execute(text(
                "SELECT canonical_name FROM cloudops.tenant UNION"
                " SELECT alias FROM cloudops.tenant_alias"))]
        return names

    @activity.defn
    async def resolve_ci(self, req: CiRequest) -> str | None:
        """The platform, not the model, decides which resource a CI is: one
        match by name within the tenant, or none."""
        ci = req.ci
        ctx = _ctx(TriageRequest(req.tenant_id, ""))
        async with self._db.transaction(ctx.scope) as conn:
            ids = [str(r[0]) for r in await conn.execute(text(
                "SELECT id FROM cloudops.resource WHERE deleted_at IS NULL"
                " AND name_norm = lower(split_part(:ci, '.', 1)) LIMIT 2"), {"ci": ci})]
        return ids[0] if len(ids) == 1 else None

    @activity.defn
    async def classify(self, req: ClassifyRequest) -> AgentStep:
        try:
            result = await self._classifier.run(
                _ctx(req.request), request="Triage this ticket.", data=[ticket_data(req.ticket)])
        except AgentOutputError:
            return AgentStep("invalid_output")
        except GatewayRefusedError:
            return AgentStep("refused")
        output: TicketTriage | None = result.output
        return AgentStep(result.status, None if output is None else output.model_dump(mode="json"),
                         str(result.run_id), str(result.cost))

    @activity.defn
    async def enrich(self, req: EnrichRequest) -> AgentStep:
        try:
            result = await self._enricher.run(
                _ctx(req.request),
                request="Assemble the context bundle for this ticket.",
                data=[ticket_data(req.ticket),
                      DataItem("classifier", req.classification, "classification")],
            )
        except AgentOutputError:
            return AgentStep("invalid_output")
        except GatewayRefusedError:
            return AgentStep("refused")
        output: EnrichmentBundle | None = result.output
        return AgentStep(result.status, None if output is None else output.model_dump(mode="json"),
                         str(result.run_id), str(result.cost), result.evidence)


def activities(acts: TriageActivities) -> list[Any]:
    return [acts.load_ticket, acts.tenant_names, acts.resolve_ci, acts.classify, acts.enrich]


@workflow.defn
class TicketTriageWorkflow:
    @workflow.run
    async def run(self, req: TriageRequest) -> TriageOutcome:
        acts = TriageActivities

        # Stage 1: intake.
        ticket = await workflow.execute_activity_method(
            acts.load_ticket, req, start_to_close_timeout=DB_TIMEOUT, retry_policy=DB_RETRY)
        if ticket is None:
            return TriageOutcome(req.number, "human", 1, ["ticket_not_found"])
        signals = triage.injection_signals(
            {k: ticket.get(k) for k in ("short_description", "configuration_item", "close_notes")})
        decision = triage.intake(ticket)
        if decision.route == "human":
            return TriageOutcome(req.number, "human", 1, list(decision.reasons),
                                 injection_signals=signals)

        # Stage 2: classify.
        names = await workflow.execute_activity_method(
            acts.tenant_names, req, start_to_close_timeout=DB_TIMEOUT, retry_policy=DB_RETRY)
        cls = await workflow.execute_activity_method(
            acts.classify, ClassifyRequest(req, ticket),
            start_to_close_timeout=AGENT_START_TO_CLOSE,
            schedule_to_close_timeout=AGENT_SCHEDULE_TO_CLOSE, retry_policy=AGENT_RETRY)
        runs = [r for r in (cls.run_id,) if r]
        decision = triage.after_classification(ticket, status=cls.status, output=cls.output,
                                               tenant_names=names)
        if decision.route == "human" or cls.output is None:
            return TriageOutcome(req.number, "human", 2, list(decision.reasons), cls.output,
                                 injection_signals=signals, agent_runs=runs, cost=cls.cost)
        resource = None
        if cls.output.get("affected_ci"):
            resource = await workflow.execute_activity_method(
                acts.resolve_ci, CiRequest(req.tenant_id, str(cls.output["affected_ci"])),
                start_to_close_timeout=DB_TIMEOUT, retry_policy=DB_RETRY)

        # Stage 3: enrich.
        enr = await workflow.execute_activity_method(
            acts.enrich, EnrichRequest(req, ticket, cls.output),
            start_to_close_timeout=AGENT_START_TO_CLOSE,
            schedule_to_close_timeout=AGENT_SCHEDULE_TO_CLOSE, retry_policy=AGENT_RETRY)
        runs += [r for r in (enr.run_id,) if r]
        cost = str(Decimal(cls.cost) + Decimal(enr.cost))
        decision = triage.after_enrichment(status=enr.status, bundle=enr.output,
                                           evidence=enr.evidence)
        return TriageOutcome(req.number, decision.route, 3, list(decision.reasons), cls.output,
                             resource, enr.output, signals, runs, cost, enr.evidence)
