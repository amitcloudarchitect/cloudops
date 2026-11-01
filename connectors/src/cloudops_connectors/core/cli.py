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

**Fixtures, or the platform's own identity.** A connector without a ``Live`` path
refuses to start without ``--fixtures`` rather than reach for a credential
(CLAUDE.md rule 2). Customer estates read with a customer's own stored credential
(ADR 0003, ADR 0027) are ``cloudops_connectors.customer``'s, not this command's.
``--fixtures`` serves recorded fixtures from an in-process fake, in the local
profile only. A ``Live`` path (Azure, ADR 0023) reads with the pod's workload identity, in the Azure
profile only, and only its own entities: ``run`` and ``schedule`` default to
them and refuse any other.
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
from dataclasses import asdict, dataclass
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
from cloudops_config.settings import environment_from
from cloudops_config.temporal import connect, run_worker
from cloudops_connectors.core import sync
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


def bronze_container(settings: Settings) -> ContainerClient:
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
            f"{name} has no credential path here (CLAUDE.md rule 2). Run with --fixtures in "
            "the local profile; a customer's own stored credential is "
            "cloudops_connectors.customer's (ADR 0027)."
        )
    if settings.environment is not Environment.LOCAL:
        raise ConfigurationError("--fixtures is local-only")
    return factory()


@dataclass(frozen=True)
class Live:
    """A source's live path: the components it needs beyond the database, bronze
    and Temporal, the entities it serves as configured, and how to build it.
    ``build`` pushes anything it opens onto the stack."""

    requires: frozenset[Component]
    entities: Callable[[Settings], frozenset[str]]
    build: Callable[[Settings, AsyncExitStack], Source]
    # What ``schedule`` loads to learn the entities. The schedule job mounts no
    # secrets, so a source whose component checks one (CoreStack's secret key) says
    # here that its entities need none of it.
    schedule_requires: frozenset[Component] | None = None


def _entities(args: argparse.Namespace, entities: Mapping[str, timedelta],
              live: frozenset[str] | None) -> list[str]:
    if live is None:
        return list(args.entity or sorted(entities))
    wanted = list(args.entity or sorted(live))
    other = sorted(set(wanted) - live)
    if other:
        raise ConfigurationError(f"the live path does not serve {', '.join(other)}")
    return wanted


async def upsert_schedule(temporal: Client, schedule_id: str, wanted: Schedule, *,
                          trigger_on_create: bool = False) -> str:
    """Create the schedule, or update it to ``wanted``. ``trigger_on_create`` runs a
    new schedule once now, so a new deployment's first sync is not left to the
    first interval; an update never triggers."""
    try:
        await temporal.create_schedule(schedule_id, wanted, trigger_immediately=trigger_on_create)
    except ScheduleAlreadyRunningError:
        def update(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=wanted)

        await temporal.get_schedule_handle(schedule_id).update(update)
        return f"updated {schedule_id}"
    return f"created {schedule_id}"


async def _main(args: argparse.Namespace, name: str, entities: Mapping[str, timedelta],
                factory: Callable[[], Source], live: Live | None = None) -> int:
    queue = task_queue(name)
    if args.command == "schedule":
        azure_live = live if environment_from(os.environ) is Environment.AZURE else None
        settings = load_settings(os.environ, require={
            Component.TEMPORAL, *(() if azure_live is None else azure_live.requires
                                  if azure_live.schedule_requires is None
                                  else azure_live.schedule_requires)})
        temporal = await connect(settings.temporal, **temporal_options())
        served = azure_live.entities(settings) if azure_live is not None else None
        for entity in _entities(args, entities, served):
            since = (datetime.now(UTC) - timedelta(days=90)).isoformat()
            wanted = Schedule(
                action=ScheduleActionStartWorkflow(
                    ConnectorIngest.run, IngestRequest(entity, since),
                    id=f"{name}-{entity}", task_queue=queue),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=entities[entity])]),
            )
            done = await upsert_schedule(temporal, f"{name}-{entity}", wanted,
                                         trigger_on_create=True)
            print(f"{done}: every {entities[entity]}")
        return 0
    live = None if args.fixtures else live
    settings = load_settings(os.environ, require={
        Component.DATABASE_INGEST, Component.BLOB, Component.TEMPORAL,
        *(live.requires if live is not None else ())})
    if live is None:
        source = _fixture_source(settings, args.fixtures, name, factory)
    elif settings.environment is not Environment.AZURE:  # pragma: no cover - load refuses
        raise ConfigurationError("the live path runs in the Azure profile only")
    run_entities = _entities(args, entities,
                             live.entities(settings) if live is not None else None)
    temporal = await connect(settings.temporal, **temporal_options())
    async with AsyncExitStack() as stack:
        if live is not None:
            source = live.build(settings, stack)
        db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=2)
        stack.push_async_callback(db.dispose)
        container = bronze_container(settings)
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
            if name in sync.SYNC_SOURCES:  # ADR 0037: sync now when a customer is onboarded
                requests = asyncio.create_task(sync.serve(
                    db, temporal, name, [f"{name}-{e}" for e in run_entities]))

                async def stop_requests() -> None:
                    requests.cancel()
                    await asyncio.gather(requests, return_exceptions=True)

                stack.push_async_callback(stop_requests)
            print(f"worker serving {queue}")
            await run_worker(worker, settings.temporal)
            return 0
        async with worker:
            for entity in run_entities:
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
         argv: list[str] | None = None, live: Live | None = None) -> int:
    """``entities`` maps each entity to its schedule interval; ``factory`` builds
    the fixture source, ``live`` the source's live path if it has one."""
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
            return asyncio.run(_main(args, name, entities, factory, live))
        finally:
            telemetry.shutdown()
    except (ConfigurationError, TelemetryConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
