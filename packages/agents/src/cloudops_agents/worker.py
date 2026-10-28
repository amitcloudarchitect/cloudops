"""Run the triage worker: pipeline stages 1 to 3 on Temporal.

    CLOUDOPS_ENV=local python -m cloudops_agents.worker

Serves ``TicketTriageWorkflow`` and its activities on the ``remediation-triage``
queue, with the AI gateway in-process (ADR 0007). Probes on port 8080:
liveness is the event loop answering; readiness is the database and Temporal.

Locally the gateway uses the mock providers. The Azure profile refuses to
load the LLM component until the Azure OpenAI tiers exist (LLD open item L12),
so this worker cannot start in Azure yet: it fails at startup, loudly, rather
than serving a model that is not there.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack

from temporalio.client import Client
from temporalio.worker import Worker

from cloudops_agents.enricher import knowledge_tool
from cloudops_agents.remediation import (
    TASK_QUEUE,
    TicketTriageWorkflow,
    TriageActivities,
    activities,
)
from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_config.probes import serve_probes
from cloudops_gateway.gateway import Gateway
from cloudops_gateway.pricing import LOCAL_DEPLOYMENTS
from cloudops_gateway.providers import MockProvider
from cloudops_gateway.registry import ToolRegistry
from cloudops_gateway.retrieval import KnowledgeIndex
from cloudops_gateway.tools import TOOLS
from cloudops_tenancy import Database


async def _main() -> int:
    settings = load_settings(
        os.environ, require={Component.DATABASE_APP, Component.TEMPORAL, Component.LLM}
    )
    local = settings.environment is Environment.LOCAL
    temporal = await Client.connect(settings.temporal.address,
                                    namespace=settings.temporal.namespace)
    async with AsyncExitStack() as stack:
        db = Database.create(settings.database_dsn(DbLogin.APP), pool_size=4)
        stack.push_async_callback(db.dispose)
        gateway = Gateway(db, {"mock": MockProvider("mock"),
                               "mock-secondary": MockProvider("mock-secondary")},
                          LOCAL_DEPLOYMENTS)
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
        await worker.run()
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
