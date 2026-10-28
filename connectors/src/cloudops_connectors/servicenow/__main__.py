"""Run the ServiceNow connector.

    python -m cloudops_connectors.servicenow run --fixtures --entity incident
    python -m cloudops_connectors.servicenow replay --fixtures --entity incident \
        --start 2026-10-28 --end 2026-10-28
    python -m cloudops_connectors.servicenow worker --fixtures
    python -m cloudops_connectors.servicenow schedule --entity incident

``run`` and ``replay`` start a worker in this process, run one workflow on
Temporal and print its report. ``worker`` serves the task queue until
stopped. ``schedule`` creates a Temporal schedule (every 5 minutes, LLD) that
starts ``ServiceNowIngest`` for an entity.

``--fixtures`` serves recorded fixtures from an in-process fake instance:
local profile only. Against a real instance this refuses until the ServiceNow
read account exists (LLD readiness gate 3): no credential path is invented
here (CLAUDE.md rule 2).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from azure.storage.blob.aio import ContainerClient
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.worker import Worker

from cloudops_config import (
    AuthMode,
    Component,
    ConfigurationError,
    DbLogin,
    Environment,
    NotBuiltError,
    Settings,
    load_settings,
)
from cloudops_connectors.core.raw import BlobRawStore
from cloudops_connectors.servicenow.client import ServiceNowClient
from cloudops_connectors.servicenow.fake import FIXTURE_HOST, FakeServiceNow
from cloudops_connectors.servicenow.mapping import ENTITIES
from cloudops_connectors.servicenow.workflows import (
    TASK_QUEUE,
    WORKFLOWS,
    IngestRequest,
    ReplayRequest,
    ServiceNowActivities,
    ServiceNowIngest,
    ServiceNowReplay,
    activities,
)
from cloudops_tenancy import Database

AZURITE = "UseDevelopmentStorage=true"


def _container(settings: Settings) -> ContainerClient:
    blob = settings.blob
    if blob.auth is AuthMode.AZURITE_DEV_ACCOUNT:
        return ContainerClient.from_connection_string(AZURITE, "bronze")
    from azure.identity.aio import WorkloadIdentityCredential

    return ContainerClient.from_container_url(
        blob.bronze_container_url, credential=WorkloadIdentityCredential()
    )


def _client(settings: Settings, fixtures: bool) -> ServiceNowClient:
    if fixtures:
        if settings.environment is not Environment.LOCAL:
            raise ConfigurationError("--fixtures is local-only")
        return ServiceNowClient(FIXTURE_HOST, transport=FakeServiceNow.from_fixtures().transport())
    raise NotBuiltError(
        "no ServiceNow read account yet (LLD readiness gate 3); run with --fixtures"
    )


async def _main(args: argparse.Namespace) -> int:
    settings = load_settings(
        os.environ, require={Component.DATABASE_INGEST, Component.BLOB, Component.TEMPORAL}
    )
    temporal = await Client.connect(settings.temporal.address,
                                    namespace=settings.temporal.namespace)
    if args.command == "schedule":
        for entity in args.entity or sorted(ENTITIES):
            await temporal.create_schedule(
                f"servicenow-{entity}",
                Schedule(
                    action=ScheduleActionStartWorkflow(
                        ServiceNowIngest.run,
                        IngestRequest(entity, (datetime.now(UTC) - timedelta(days=90)).isoformat()),
                        id=f"servicenow-{entity}", task_queue=TASK_QUEUE,
                    ),
                    spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=5))]),
                ),
            )
            print(f"scheduled servicenow-{entity} every 5 minutes")
        return 0

    async with AsyncExitStack() as stack:
        db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=2)
        stack.push_async_callback(db.dispose)
        container = _container(settings)
        stack.push_async_callback(container.close)
        client = _client(settings, args.fixtures)
        stack.push_async_callback(client.aclose)
        acts = ServiceNowActivities(db, BlobRawStore(container), client)
        worker = Worker(temporal, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                        activities=activities(acts))
        if args.command == "worker":
            print(f"worker serving {TASK_QUEUE}")
            await worker.run()
            return 0
        async with worker:
            for entity in args.entity or sorted(ENTITIES):
                wf_id = f"servicenow-{args.command}-{entity}-{uuid.uuid4().hex[:8]}"
                if args.command == "run":
                    report = await temporal.execute_workflow(
                        ServiceNowIngest.run, IngestRequest(entity, args.since),
                        id=wf_id, task_queue=TASK_QUEUE,
                    )
                else:
                    report = await temporal.execute_workflow(
                        ServiceNowReplay.run, ReplayRequest(entity, args.start, args.end),
                        id=wf_id, task_queue=TASK_QUEUE,
                    )
                print(json.dumps(asdict(report), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_connectors.servicenow")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "replay", "worker", "schedule"):
        p = sub.add_parser(name)
        p.add_argument("--fixtures", action="store_true", help="serve recorded fixtures (local)")
        p.add_argument("--entity", action="append", choices=sorted(ENTITIES))
        if name == "run":
            p.add_argument("--since", default=(datetime.now(UTC) - timedelta(days=120)).isoformat(),
                           help="ISO time to start from when no watermark exists")
        if name == "replay":
            p.add_argument("--start", required=True, help="first bronze day, YYYY-MM-DD")
            p.add_argument("--end", required=True, help="last bronze day, YYYY-MM-DD")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
