"""Run the AI worker: triage (pipeline stages 1 to 3), drafting and indexing.

    CLOUDOPS_ENV=local python -m cloudops_agents.worker
    CLOUDOPS_ENV=local python -m cloudops_agents.worker schedule

Serves ``TicketTriageWorkflow`` on the ``remediation-triage`` queue, and the
step 17 drafting sweep and knowledge indexer (``drafting``) on the ``drafting``
queue, with the AI gateway in-process (ADR 0007). ``schedule`` creates or
updates the drafting and indexing schedules, safe to repeat. Probes on port 8080:
liveness is the event loop answering; readiness is the database and Temporal.

Locally the gateway uses the mock providers. In Azure it calls the Azure
OpenAI account (ADR 0012) as the worker's workload identity: an Entra token,
no key, over the private endpoint. Nothing falls back from one to the other.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.worker import Worker

from cloudops_agents import drafting
from cloudops_agents.enricher import knowledge_tool
from cloudops_agents.remediation import (
    TASK_QUEUE,
    TicketTriageWorkflow,
    TriageActivities,
    activities,
)
from cloudops_config import (
    Component,
    ConfigurationError,
    DbLogin,
    Environment,
    Settings,
    load_settings,
)
from cloudops_config.probes import serve_probes
from cloudops_config.temporal import connect, run_worker
from cloudops_gateway.azure_openai import TOKEN_SCOPE, AzureOpenAIProvider
from cloudops_gateway.gateway import Gateway
from cloudops_gateway.pricing import AZURE_DEPLOYMENTS, LOCAL_DEPLOYMENTS
from cloudops_gateway.providers import MockProvider, Provider
from cloudops_gateway.registry import ToolRegistry
from cloudops_gateway.retrieval import KnowledgeIndex
from cloudops_gateway.tools import TOOLS
from cloudops_observability.process import start, temporal_options
from cloudops_tenancy import Database

SERVICE = "worker-triage"


async def _gateway(settings: Settings, db: Database, stack: AsyncExitStack) -> Gateway:
    if settings.environment is Environment.LOCAL:
        return Gateway(db, {"mock": MockProvider("mock"),
                            "mock-secondary": MockProvider("mock-secondary")},
                       LOCAL_DEPLOYMENTS)
    from azure.identity.aio import WorkloadIdentityCredential

    credential = WorkloadIdentityCredential()  # caches and renews the token
    stack.push_async_callback(credential.close)

    async def token() -> str:
        return (await credential.get_token(TOKEN_SCOPE)).token

    provider = AzureOpenAIProvider(settings.azure_openai.endpoint, token=token)
    stack.push_async_callback(provider.aclose)
    providers: dict[str, Provider] = {provider.name: provider}
    return Gateway(db, providers, AZURE_DEPLOYMENTS)


async def _schedule() -> int:
    settings = load_settings(os.environ, require={Component.TEMPORAL})
    temporal: Client = await connect(settings.temporal)
    for schedule_id, wf, every in drafting.SCHEDULES:
        wanted = Schedule(
            action=ScheduleActionStartWorkflow(wf.run, id=schedule_id,
                                               task_queue=drafting.TASK_QUEUE),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)]),
        )
        try:
            await temporal.create_schedule(schedule_id, wanted)
            print(f"created {schedule_id}: every {every}")
        except ScheduleAlreadyRunningError:
            def update(_: ScheduleUpdateInput, wanted: Schedule = wanted) -> ScheduleUpdate:
                return ScheduleUpdate(schedule=wanted)

            await temporal.get_schedule_handle(schedule_id).update(update)
            print(f"updated {schedule_id}: every {every}")
    return 0


async def _main() -> int:
    settings = load_settings(
        os.environ, require={Component.DATABASE_APP, Component.TEMPORAL, Component.LLM}
    )
    local = settings.environment is Environment.LOCAL
    telemetry = start(SERVICE, os.environ)
    temporal = await connect(settings.temporal, **temporal_options(telemetry))
    async with AsyncExitStack() as stack:
        stack.callback(telemetry.shutdown)
        db = Database.create(settings.database_dsn(DbLogin.APP), pool_size=4)
        stack.push_async_callback(db.dispose)
        gateway = await _gateway(settings, db, stack)
        registry = ToolRegistry(db)
        for tool in (*TOOLS, knowledge_tool(KnowledgeIndex(db, gateway))):
            registry.register(tool)

        async def temporal_ready() -> None:
            if not await temporal.service_client.check_health():
                raise ConnectionError("temporal unhealthy")

        probes = await serve_probes(
            {"database": db.ping, "temporal": temporal_ready},
            host="127.0.0.1" if local else "0.0.0.0",  # noqa: S104 - the kubelet probes the pod IP
        )
        stack.push_async_callback(probes.wait_closed)
        stack.callback(probes.close)
        worker = Worker(temporal, task_queue=TASK_QUEUE, workflows=[TicketTriageWorkflow],
                        activities=activities(TriageActivities(db, gateway, registry)))
        drafter = Worker(temporal, task_queue=drafting.TASK_QUEUE, workflows=drafting.WORKFLOWS,
                         activities=drafting.activities(drafting.DraftingActivities(db, gateway)))
        print(f"worker serving {TASK_QUEUE} and {drafting.TASK_QUEUE}")
        await asyncio.gather(run_worker(worker, settings.temporal),
                             run_worker(drafter, settings.temporal))
    return 0


def main() -> int:
    try:
        if sys.argv[1:] == ["schedule"]:
            return asyncio.run(_schedule())
        return asyncio.run(_main())
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
