"""Run the golden set through the real stage 1 to 3 workflow.

    outcomes = await run_cases(cases, app_dsn=..., tenants={"alpha": id, ...},
                               cassettes=Path("evals/cassettes"))

For each case:
1. Insert the ticket for its tenant, through packages/tenancy, as the connector
   would.
2. Plant any other tenant's content the case tries to reach, and index a
   little of each tenant's own knowledge.
3. Run ``TicketTriageWorkflow`` on a Temporal time-skipping test server, with
   the real activities, the real gateway, registry and tools. Only the model
   is replaced, by the replay provider.

What is scored is what production runs: the same workflow, guardrails,
schemas and scope. Leaks are measured on the evidence the tools returned and
on the agents' outputs: any of the case's leak markers, or any other tenant's
id, counts.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import text
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from cloudops_agents import triage
from cloudops_agents.enricher import knowledge_tool
from cloudops_agents.remediation import (
    TASK_QUEUE,
    TicketTriageWorkflow,
    TriageActivities,
    TriageOutcome,
    TriageRequest,
    activities,
)
from cloudops_evals.cases import GoldenCase
from cloudops_evals.replay import ReplayProvider
from cloudops_evals.scoring import Outcome
from cloudops_gateway.gateway import CallContext, Gateway, Limits
from cloudops_gateway.pricing import LOCAL_DEPLOYMENTS
from cloudops_gateway.registry import ToolRegistry
from cloudops_gateway.retrieval import KnowledgeIndex
from cloudops_gateway.tools import TOOLS
from cloudops_tenancy import Database, Role, TenantScope

# Each tenant's own knowledge, so enrichment has something real to find and cite.
OWN_KNOWLEDGE: Final = {
    "alpha": (
        ("alpha-disk-runbook", "runbook", "Extend disk runbook",
         "Runbook: extend disk on an alpha VM when usage passes 90 percent; check the volume "
         "group first."),
        ("alpha-iis-restart", "runbook", "Restart IIS service",
         "Runbook: restart IIS service on alpha web servers, then confirm the site answers."),
        ("alpha-backup-resolution", "close_note", "Backup job failed",
         "Resolution: backup job failed because the backup agent service had stopped; "
         "restarted the agent and re-ran the job."),
    ),
}
EVAL_LIMITS: Final = Limits(user_daily_tokens=10_000_000, tenant_monthly_tokens=100_000_000)
SETUP_PRINCIPAL: Final = "eval:setup"


def _ctx(tenant: UUID) -> CallContext:
    return CallContext.for_tenant(TenantScope([tenant], Role.PLATFORM_ADMIN, SETUP_PRINCIPAL),
                                  tenant, ["knowledge.read"])


async def _insert_ticket(db: Database, tenant: UUID, case: GoldenCase) -> None:
    t = case.ticket
    async with db.transaction(TenantScope([tenant], Role.PLATFORM_ADMIN, SETUP_PRINCIPAL)) as c:
        await c.execute(text(
            "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
            " state, priority, short_description, configuration_item, close_notes)"
            " VALUES (:tenant, :kind, :number, :company, now(), :state, :priority, :desc, :ci,"
            " :notes)"), {
            "tenant": tenant, "kind": t.kind, "number": t.number, "company": t.company,
            "state": t.state, "priority": t.priority, "desc": t.short_description,
            "ci": t.configuration_item, "notes": t.close_notes,
        })


def _leaks(case: GoldenCase, outcome: TriageOutcome, own: UUID,
           tenants: Mapping[str, UUID]) -> int:
    foreign_ids = [str(t) for t in tenants.values() if t != own]
    markers = [*case.leak_markers, *foreign_ids]
    if case.plant is not None:
        markers.append(case.plant.text)
    found = 0
    for item in outcome.evidence:
        found += sum(1 for m in markers if m and m in item)
    # Outputs may legitimately repeat a name the model guessed (grounding rejects
    # it); they must never carry a secret only the other tenant's data holds.
    secrets = [m for m in case.leak_markers if m != (case.plant.ref if case.plant else None)]
    outputs = json.dumps([outcome.classification, outcome.enrichment])
    found += sum(1 for m in [*secrets, *foreign_ids] if m and m in outputs)
    return found


def _grounded(case: GoldenCase, result: TriageOutcome) -> bool:
    """Computed from the raw outputs, independently of the guardrails, so
    removing a guardrail can never make groundedness look better."""
    cls = result.classification or {}
    ci = cls.get("affected_ci")
    if ci:
        ticket_text = " ".join(filter(None, (case.ticket.short_description,
                                             case.ticket.configuration_item)))
        if str(ci).lower() not in ticket_text.lower():
            return False
    if result.enrichment is not None:
        return triage.grounding(triage.bundle_claims(result.enrichment), result.evidence).grounded
    return True


def _outcome(case: GoldenCase, result: TriageOutcome, own: UUID,
             tenants: Mapping[str, UUID]) -> Outcome:
    cls = result.classification or {}
    return Outcome(
        case_id=case.id, route=result.route, reasons=result.reasons,
        intent=cls.get("intent"), action_class=cls.get("action_class"),
        ci=cls.get("affected_ci"),
        grounded=_grounded(case, result),
        foreign_items=_leaks(case, result, own, tenants), cost=Decimal(result.cost),
    )


async def run_cases(
    cases: Sequence[GoldenCase],
    *,
    app_dsn: str,
    tenants: Mapping[str, UUID],
    cassettes: Path,
) -> tuple[dict[str, Outcome], dict[str, TriageOutcome]]:
    db = Database.create(app_dsn, pool_size=4, max_overflow=0)
    replay = ReplayProvider.from_directory(cassettes, "mock")
    gateway = Gateway(db, {"mock": replay, "mock-secondary": ReplayProvider("mock-secondary")},
                      LOCAL_DEPLOYMENTS, EVAL_LIMITS)
    index = KnowledgeIndex(db, gateway)
    registry = ToolRegistry(db)
    for tool in (*TOOLS, knowledge_tool(index)):
        registry.register(tool)
    try:
        for key, docs in OWN_KNOWLEDGE.items():
            if key in tenants:
                for ref, kind, title, body in docs:
                    await index.add(_ctx(tenants[key]), source_kind=kind, source_ref=ref,
                                    title=title, text_body=body)
        for case in cases:
            if case.plant is not None:
                await index.add(_ctx(tenants[case.plant.tenant]), source_kind="runbook",
                                source_ref=case.plant.ref, title=case.plant.ref,
                                text_body=case.plant.text)
            await _insert_ticket(db, tenants[case.tenant], case)

        acts = TriageActivities(db, gateway, registry)
        scored: dict[str, Outcome] = {}
        raw: dict[str, TriageOutcome] = {}
        async with (
            await WorkflowEnvironment.start_time_skipping() as env,
            Worker(env.client, task_queue=TASK_QUEUE, workflows=[TicketTriageWorkflow],
                   activities=activities(acts)),
        ):
            for case in cases:
                own = tenants[case.tenant]
                try:
                    result = await env.client.execute_workflow(
                        TicketTriageWorkflow.run, TriageRequest(str(own), case.ticket.number),
                        id=f"eval-{case.id}-{uuid.uuid4().hex[:8]}", task_queue=TASK_QUEUE)
                except Exception as exc:
                    # One case crashing is that case failing, not the suite aborting.
                    result = TriageOutcome(case.ticket.number, "error", 0,
                                           [f"workflow_failed:{type(exc).__name__}"])
                raw[case.id] = result
                scored[case.id] = _outcome(case, result, own, tenants)
        return scored, raw
    finally:
        await db.dispose()


def describe(raw: Mapping[str, TriageOutcome]) -> str:
    return json.dumps({k: {"route": v.route, "stage": v.stage, "reasons": v.reasons}
                       for k, v in raw.items()}, indent=1, sort_keys=True)

