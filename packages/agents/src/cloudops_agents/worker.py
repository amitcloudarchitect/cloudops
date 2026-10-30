"""Run the triage worker: pipeline stages 1 to 3 on Temporal.

    CLOUDOPS_ENV=local python -m cloudops_agents.worker

Serves ``TicketTriageWorkflow`` and its activities on the ``remediation-triage``
queue, with the AI gateway in-process (ADR 0007). Probes on port 8080:
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

from temporalio.worker import Worker

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
from cloudops_tenancy import Database


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


async def _main() -> int:
    settings = load_settings(
        os.environ, require={Component.DATABASE_APP, Component.TEMPORAL, Component.LLM}
    )
    local = settings.environment is Environment.LOCAL
    temporal = await connect(settings.temporal)
    async with AsyncExitStack() as stack:
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
        print(f"worker serving {TASK_QUEUE}")
        await run_worker(worker, settings.temporal)
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
