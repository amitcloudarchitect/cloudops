"""The command line every step 15 connector shares.

    python -m cloudops_connectors.<source> run --fixtures [--entity E]
    python -m cloudops_connectors.<source> replay --fixtures --entity E --start D --end D
    python -m cloudops_connectors.<source> worker --fixtures
    python -m cloudops_connectors.<source> schedule [--entity E]

``run`` and ``replay`` start a worker in this process, run one workflow and print
its report; ``worker`` serves the source's task queue with liveness and
readiness probes; ``schedule`` creates or updates one Temporal schedule per
entity at the source's cadence (HLD, Integration), safe to repeat. Temporal's
default overlap policy (skip) keeps it to one run at a time per entity: the
LLD's "one report pull at a time" for Qualys.

**Fixtures only.** No customer credentials exist yet (CLAUDE.md rule 2; ADR
0003), so these connectors have no credential path: without ``--fixtures`` they
refuse to start rather than reach for one. ``--fixtures`` serves recorded
fixtures from an in-process fake, in the local profile only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from azure.storage.blob.aio import ContainerClient
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

from cloudops_config import (
    AuthMode,
    Component,
    ConfigurationError,
    DbLogin,
    Environment,
    Settings,
    load_settings,
)
from cloudops_config.probes import serve_probes
from cloudops_config.temporal import connect, run_worker
from cloudops_connectors.core.ingest import (
    WORKFLOWS,
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    ReplayRequest,
    Source,
    activities,
    task_queue,
)
from cloudops_connectors.core.raw import BlobRawStore
from cloudops_observability import TelemetryConfigError
from cloudops_observability.process import start, temporal_options
from cloudops_tenancy import Database

AZURITE = "UseDevelopmentStorage=true"


class NoCredentialPathError(ConfigurationError):
    """Raised instead of reaching for a credential that does not exist."""


def _container(settings: Settings) -> ContainerClient:
    blob = settings.blob
    if blob.auth is AuthMode.AZURITE_DEV_ACCOUNT:
        return ContainerClient.from_connection_string(AZURITE, "bronze")
    from azure.identity.aio import WorkloadIdentityCredential

    return ContainerClient.from_container_url(
        blob.bronze_container_url, credential=WorkloadIdentityCredential()
    )


def _fixture_source(settings: Settings, fixtures: bool, name: str,
                    factory: Callable[[], Source]) -> Source:
    if not fixtures:
        raise NoCredentialPathError(
            f"{name} has no credential path yet: there are no customer credentials "
            "(CLAUDE.md rule 2, ADR 0003). Run with --fixtures in the local profile."
        )
    if settings.environment is not Environment.LOCAL:
        raise ConfigurationError("--fixtures is local-only")
    return factory()


async def _upsert(temporal: Client, schedule_id: str, wanted: Schedule) -> str:
    try:
        await temporal.create_schedule(schedule_id, wanted)
    except ScheduleAlreadyRunningError:
        def update(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=wanted)

        await temporal.get_schedule_handle(schedule_id).update(update)
        return f"updated {schedule_id}"
    return f"created {schedule_id}"


async def _main(args: argparse.Namespace, name: str, entities: Mapping[str, timedelta],
                factory: Callable[[], Source]) -> int:
    queue = task_queue(name)
    if args.command == "schedule":
        settings = load_settings(os.environ, require={Component.TEMPORAL})
        temporal = await connect(settings.temporal, **temporal_options())
        for entity in args.entity or sorted(entities):
            since = (datetime.now(UTC) - timedelta(days=90)).isoformat()
            wanted = Schedule(
                action=ScheduleActionStartWorkflow(
                    ConnectorIngest.run, IngestRequest(entity, since),
                    id=f"{name}-{entity}", task_queue=queue),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=entities[entity])]),
            )
            print(f"{await _upsert(temporal, f'{name}-{entity}', wanted)}: every "
                  f"{entities[entity]}")
        return 0
    settings = load_settings(os.environ, require={Component.DATABASE_INGEST, Component.BLOB,
                                                  Component.TEMPORAL})
    source = _fixture_source(settings, args.fixtures, name, factory)
    temporal = await connect(settings.temporal, **temporal_options())
    async with AsyncExitStack() as stack:
        db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=2)
        stack.push_async_callback(db.dispose)
        container = _container(settings)
        stack.push_async_callback(container.close)
        acts = ConnectorActivities(db, BlobRawStore(container), source)
        worker = Worker(temporal, task_queue=queue, workflows=WORKFLOWS,
                        activities=activities(acts))
        if args.command == "worker":
            async def temporal_ready() -> None:
                if not await temporal.service_client.check_health():
                    raise ConnectionError("temporal unhealthy")

            async def bronze_ready() -> None:
                await container.get_container_properties()

            local = settings.environment is Environment.LOCAL
            probes = await serve_probes(
                {"database": db.ping, "temporal": temporal_ready, "bronze": bronze_ready},
                host="127.0.0.1" if local else "0.0.0.0",  # noqa: S104 - the kubelet probes the pod IP
            )

            async def close() -> None:
                probes.close()
                await probes.wait_closed()

            stack.push_async_callback(close)
            print(f"worker serving {queue}")
            await run_worker(worker, settings.temporal)
            return 0
        async with worker:
            for entity in args.entity or sorted(entities):
                wf_id = f"{name}-{args.command}-{entity}-{uuid.uuid4().hex[:8]}"
                if args.command == "run":
                    report = await temporal.execute_workflow(
                        ConnectorIngest.run, IngestRequest(entity, args.since), id=wf_id,
                        task_queue=queue)
                else:
                    report = await temporal.execute_workflow(
                        ConnectorReplay.run, ReplayRequest(entity, args.start, args.end),
                        id=wf_id, task_queue=queue)
                print(json.dumps(asdict(report), indent=2, default=str))
    return 0


def main(name: str, entities: Mapping[str, timedelta], factory: Callable[[], Source],
         argv: list[str] | None = None) -> int:
    """``entities`` maps each entity to its schedule interval."""
    parser = argparse.ArgumentParser(prog=f"python -m cloudops_connectors.{name}")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "replay", "worker", "schedule"):
        p = sub.add_parser(command)
        p.add_argument("--fixtures", action="store_true", help="serve recorded fixtures (local)")
        p.add_argument("--entity", action="append", choices=sorted(entities))
        if command == "run":
            p.add_argument("--since", default=(datetime.now(UTC) - timedelta(days=400))
                           .isoformat(), help="ISO time to start from when no watermark exists")
        if command == "replay":
            p.add_argument("--start", required=True, help="first bronze day, YYYY-MM-DD")
            p.add_argument("--end", required=True, help="last bronze day, YYYY-MM-DD")
    args = parser.parse_args(argv)
    try:
        telemetry = start(f"connector-{name}", os.environ)
        try:
            return asyncio.run(_main(args, name, entities, factory))
        finally:
            telemetry.shutdown()
    except (ConfigurationError, TelemetryConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
