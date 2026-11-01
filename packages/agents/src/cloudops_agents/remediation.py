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

**Record.** Every run ends by storing its outcome in ``cloudops.triage_result``
(migration 0008), under the run's own single-tenant scope, so the console can
show it and a person can accept or override it (ADR 0014).

**What starts it.** ``TriageSweepWorkflow``, on a schedule (``SCHEDULES``):
it lists tenants, finds each tenant's open incidents with no triage result
under that tenant's own scope, and starts this workflow for each as an
abandoned child, at most ``SWEEP_LIMIT`` per run. The workflow id is
``triage_workflow_id(tenant, number)``, so a ticket is never triaged twice; a
run that failed may be started again. A tenant whose AI budget is near its
alert fraction is skipped until a later run (``Gateway.headroom``). The sweep
only chooses which tickets enter triage, by a fixed query: no model is asked.

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
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    import json

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
    from cloudops_observability import ticket_span, traceparent
    from cloudops_tenancy import Database, Role, TenantScope

TASK_QUEUE: Final = "remediation-triage"
PRINCIPAL: Final = "service:triage"
PERMISSIONS: Final = frozenset({"inventory.read", "tickets.read", "knowledge.read"})
DB_RETRY: Final = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5)
DB_TIMEOUT: Final = timedelta(seconds=30)
COST_CURRENCY: Final = "USD"  # the gateway's rate card (cloudops_gateway.pricing)
SWEEP_PRINCIPAL: Final = "service:triage-sweep"
SWEEP_EVERY: Final = timedelta(minutes=5)
SWEEP_LIMIT: Final = 20  # tickets started per run, across tenants
SWEEP_TENANT_LIMIT: Final = 10  # per tenant per run, so one tenant never takes the batch
SWEEP_WINDOW: Final = timedelta(days=7)  # opened within; older tickets wait for a person


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
    result_id: str | None = None  # the cloudops.triage_result row, once recorded


@dataclass
class RecordRequest:
    tenant_id: str
    workflow_id: str
    outcome: TriageOutcome


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


    @activity.defn
    async def record_outcome(self, req: RecordRequest) -> str:
        """Store the outcome for the console. Idempotent per workflow run: a
        retried activity finds its own row rather than writing a second."""
        o = req.outcome
        ctx = _ctx(TriageRequest(req.tenant_id, o.number))
        async with self._db.transaction(ctx.scope) as conn:
            existing = await conn.scalar(text(
                "SELECT id FROM cloudops.triage_result WHERE workflow_id = :w"),
                {"w": req.workflow_id})
            if existing is not None:
                return str(existing)
            parent = await conn.scalar(text(
                "SELECT trace_parent FROM cloudops.work_item WHERE number = :n"
                " ORDER BY opened_at DESC LIMIT 1"), {"n": o.number})
            # One trace per ticket (ADR 0022): triage joins the trace the
            # connector started at first sighting, or starts it.
            with ticket_span("triaged", parent, {"cloudops.triage.route": o.route,
                                                 "cloudops.triage.stage": o.stage}) as span:
                stored = traceparent(span)
                if parent is None and stored is not None:
                    await conn.execute(text(
                        "UPDATE cloudops.work_item SET trace_parent = :p"
                        " WHERE number = :n AND trace_parent IS NULL"),
                        {"p": stored, "n": o.number})
            new_id = await conn.scalar(text(
                "INSERT INTO cloudops.triage_result (tenant_id, number, resource_id, route, stage,"
                " reasons, classification, enrichment, injection_signals, agent_run_ids, cost,"
                " cost_currency, workflow_id) VALUES (:t, :n, :r, :route, :stage, :reasons,"
                " CAST(:cls AS jsonb), CAST(:enr AS jsonb), CAST(:sig AS jsonb),"
                " CAST(:runs AS uuid[]), :cost, :cur, :w) RETURNING id"),
                {"t": UUID(req.tenant_id), "n": o.number,
                 "r": UUID(o.resource_id) if o.resource_id else None,
                 "route": o.route, "stage": o.stage, "reasons": o.reasons,
                 "cls": None if o.classification is None else json.dumps(o.classification),
                 "enr": None if o.enrichment is None else json.dumps(o.enrichment),
                 "sig": json.dumps(o.injection_signals), "runs": o.agent_runs,
                 "cost": Decimal(o.cost), "cur": COST_CURRENCY, "w": req.workflow_id})
        return str(new_id)


def activities(acts: TriageActivities) -> list[Any]:
    return [acts.load_ticket, acts.tenant_names, acts.resolve_ci, acts.classify, acts.enrich,
            acts.record_outcome]


def triage_workflow_id(tenant_id: str, number: str) -> str:
    """One triage per ticket: the sweep starts it under this id only."""
    return f"triage-{tenant_id}-{number}"


@dataclass
class SweepRequest:
    tenant_id: str  # from the tenant list, never from a model
    limit: int


# The tenant ids the sweep visits (migration 0013's definer function: ids only).
_SWEEP_TENANTS: Final = text("SELECT tenant_id FROM cloudops.knowledge_tenants()")
# Open incidents in the window with no triage result, newest first. RLS limits it
# to the scope's one tenant; the tenant filter says so again.
_UNTRIAGED: Final = text(
    "SELECT w.number FROM cloudops.work_item w"
    " WHERE w.tenant_id = :t AND w.kind = 'incident' AND w.closed_at IS NULL"
    " AND w.opened_at >= now() - CAST(:window AS interval)"
    " AND lower(coalesce(w.state, '')) <> ALL(:closed)"
    " AND NOT EXISTS (SELECT 1 FROM cloudops.triage_result r"
    "                 WHERE r.tenant_id = w.tenant_id AND r.number = w.number)"
    " GROUP BY w.number ORDER BY max(w.opened_at) DESC, w.number LIMIT :limit"
)


class TriageSweepActivities:
    def __init__(self, db: Database, gateway: Gateway) -> None:
        self._db = db
        self._gateway = gateway

    @activity.defn
    async def triage_tenants(self) -> list[str]:
        async with self._db.transaction(TenantScope.for_resolution(SWEEP_PRINCIPAL)) as conn:
            return [str(r[0]) for r in await conn.execute(_SWEEP_TENANTS)]

    @activity.defn
    async def untriaged(self, req: SweepRequest) -> list[str]:
        """This tenant's tickets to triage now, or none while its AI budget is near
        its alert fraction: they wait for a later run rather than being refused."""
        ctx = _ctx(TriageRequest(req.tenant_id, ""))
        if not await self._gateway.headroom(ctx, CLASSIFIER.name):
            return []
        async with self._db.transaction(ctx.scope) as conn:
            return [str(r[0]) for r in await conn.execute(_UNTRIAGED, {
                "t": ctx.tenant_id, "window": f"{int(SWEEP_WINDOW.total_seconds())} seconds",
                "closed": sorted(triage.CLOSED_STATES), "limit": max(req.limit, 0)})]


def sweep_activities(acts: TriageSweepActivities) -> list[Any]:
    return [acts.triage_tenants, acts.untriaged]


@workflow.defn
class TicketTriageWorkflow:
    @workflow.run
    async def run(self, req: TriageRequest) -> TriageOutcome:
        outcome = await self._triage(req)
        outcome.result_id = await workflow.execute_activity_method(
            TriageActivities.record_outcome,
            RecordRequest(req.tenant_id, workflow.info().workflow_id, outcome),
            start_to_close_timeout=DB_TIMEOUT, retry_policy=DB_RETRY)
        return outcome

    async def _triage(self, req: TriageRequest) -> TriageOutcome:
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


@workflow.defn
class TriageSweepWorkflow:
    """Start triage for open incidents that have none. Scheduled; safe to overlap
    or repeat, since each ticket's workflow id is fixed. Returns the workflow ids
    it started."""

    @workflow.run
    async def run(self, limit: int = SWEEP_LIMIT) -> list[str]:
        tenants = await workflow.execute_activity_method(
            TriageSweepActivities.triage_tenants, start_to_close_timeout=DB_TIMEOUT,
            retry_policy=DB_RETRY)
        started: list[str] = []
        for tenant in tenants:
            room = min(SWEEP_TENANT_LIMIT, limit - len(started))
            if room <= 0:
                break
            try:
                numbers = await workflow.execute_activity_method(
                    TriageSweepActivities.untriaged, SweepRequest(tenant, room),
                    start_to_close_timeout=DB_TIMEOUT, retry_policy=DB_RETRY)
            except ActivityError:
                continue  # one tenant's failure never stops the tenants after it
            for number in numbers[:room]:
                wf_id = triage_workflow_id(tenant, number)
                try:
                    await workflow.start_child_workflow(
                        TicketTriageWorkflow.run, TriageRequest(tenant, number), id=wf_id,
                        task_queue=TASK_QUEUE, parent_close_policy=ParentClosePolicy.ABANDON,
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
                except WorkflowAlreadyStartedError:
                    continue  # running, or done and its result not yet recorded
                started.append(wf_id)
        return started


WORKFLOWS: Final = [TicketTriageWorkflow, TriageSweepWorkflow]
SCHEDULES: Final = (("triage-sweep", TriageSweepWorkflow, SWEEP_EVERY),)
