"""The drafting worker's workflows against scripted activities: one tenant's or
one draft's failure never stops the rest (step 17, ADR 0020)."""

from __future__ import annotations

import uuid

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from cloudops_agents import drafting

TENANTS = ["a", "b", "c"]


@activity.defn(name="knowledge_tenants")
async def _tenants() -> list[str]:
    return TENANTS


@activity.defn(name="index_tenant")
async def _index(tenant: str) -> int:
    if tenant == "a":  # visited first: its LLM budget is spent
        raise ApplicationError("quota exceeded", type="QuotaExceededError")
    return len(tenant) + 1


async def test_one_tenants_indexing_failure_never_stops_the_tenants_after_it() -> None:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=drafting.TASK_QUEUE, workflows=drafting.WORKFLOWS,
               activities=[_tenants, _index]),
    ):
        done = await env.client.execute_workflow(
            drafting.KnowledgeIndexWorkflow.run, id=f"index-{uuid.uuid4()}",
            task_queue=drafting.TASK_QUEUE)
    assert done == {"a": drafting.INDEX_FAILED, "b": 2, "c": 2}
