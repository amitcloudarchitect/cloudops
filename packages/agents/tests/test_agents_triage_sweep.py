"""The triage sweep against scripted activities: it starts each ticket's triage
under a fixed id, at most its limit per run, never the same ticket twice, and
one tenant's failure never stops the tenants after it."""

from __future__ import annotations

import uuid

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from cloudops_agents import remediation
from cloudops_agents.remediation import SweepRequest, TriageRequest, triage_workflow_id

TICKETS = {"a": ["INC1", "INC2", "INC3"], "b": ["INC9"], "c": ["INC5"]}
TRIAGED: list[tuple[str, str]] = []


@activity.defn(name="triage_tenants")
async def _tenants() -> list[str]:
    return list(TICKETS)


@activity.defn(name="untriaged")
async def _untriaged(req: SweepRequest) -> list[str]:
    if req.tenant_id == "b":
        raise ApplicationError("database unavailable", non_retryable=True)
    return TICKETS[req.tenant_id][: req.limit]


@workflow.defn(name="TicketTriageWorkflow", sandboxed=False)
class _Triage:
    """Stands in for the real triage: records the ticket and finishes."""

    @workflow.run
    async def run(self, req: TriageRequest) -> None:
        await workflow.execute_activity(_record, req, start_to_close_timeout=remediation.DB_TIMEOUT)


@activity.defn(name="record")
async def _record(req: TriageRequest) -> None:
    TRIAGED.append((req.tenant_id, req.number))


def _worker(env: WorkflowEnvironment) -> Worker:
    return Worker(env.client, task_queue=remediation.TASK_QUEUE,
                  workflows=[remediation.TriageSweepWorkflow, _Triage],
                  activities=[_tenants, _untriaged, _record])


async def test_the_sweep_starts_each_ticket_once_under_its_own_id() -> None:
    TRIAGED.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env, _worker(env):
        first = await env.client.execute_workflow(
            remediation.TriageSweepWorkflow.run, id=f"sweep-{uuid.uuid4()}",
            task_queue=remediation.TASK_QUEUE)
        for wf_id in first:
            await env.client.get_workflow_handle(wf_id).result()
        again = await env.client.execute_workflow(
            remediation.TriageSweepWorkflow.run, id=f"sweep-{uuid.uuid4()}",
            task_queue=remediation.TASK_QUEUE)
    # b's failure is skipped; c is still visited.
    assert first == [triage_workflow_id("a", n) for n in TICKETS["a"]] \
        + [triage_workflow_id("c", "INC5")]
    assert again == []
    assert sorted(TRIAGED) == sorted([("a", n) for n in TICKETS["a"]] + [("c", "INC5")])


async def test_the_sweep_starts_at_most_its_limit() -> None:
    TRIAGED.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env, _worker(env):
        started = await env.client.execute_workflow(
            remediation.TriageSweepWorkflow.run, 2, id=f"sweep-{uuid.uuid4()}",
            task_queue=remediation.TASK_QUEUE)
    assert started == [triage_workflow_id("a", "INC1"), triage_workflow_id("a", "INC2")]


def test_the_sweep_is_scheduled_on_the_triage_queue() -> None:
    assert [(s, wf) for s, wf, _ in remediation.SCHEDULES] == [
        ("triage-sweep", remediation.TriageSweepWorkflow)]
    assert remediation.TriageSweepWorkflow in remediation.WORKFLOWS
