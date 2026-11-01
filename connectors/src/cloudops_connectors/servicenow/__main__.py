"""Run the ServiceNow connector.

    python -m cloudops_connectors.servicenow run --fixtures --entity incident
    python -m cloudops_connectors.servicenow replay --fixtures --entity incident \
        --start 2026-10-28 --end 2026-10-28
    python -m cloudops_connectors.servicenow reprocess --fixtures
    python -m cloudops_connectors.servicenow worker --fixtures
    python -m cloudops_connectors.servicenow schedule --entity incident

``run`` and ``replay`` start a worker in this process, run one workflow on
Temporal and print its report. ``worker`` serves the task queue until
stopped, with liveness and readiness probes on port 8080 (readiness: the
database, Temporal and the bronze container all answer); it stops, to be
restarted, when its Temporal client certificate is renewed (ADR 0010).
``reprocess`` runs the re-process queue once: the oldest re-process request a
person made in the console, if any (ADR 0015).
``schedule`` creates a Temporal schedule (every 5 minutes, LLD; project tasks
every six hours, ADR 0037 and 0038) that starts ``ServiceNowIngest`` for an
entity, or updates it if it exists, and, when no entity is named, the one that
runs the re-process queue every minute: it runs as a Job on every sync, so
repeating it is safe.

``--fixtures`` serves recorded fixtures from an in-process fake instance:
local profile only. Without it the connector reads the instance configured by
the SERVICENOW component (``CLOUDOPS_SERVICENOW_URL``, ``_CLIENT_ID``, and the
client secret ``servicenow-read-client-secret`` from the secret store) with
OAuth client credentials, after checking the account's time zone (ADR 0009).

``CLOUDOPS_SERVICENOW_PROJECT_PRICE_FIELD`` names the project task field read as
the price (default ``cost``, "Total planned cost"; ADR 0038). A change needs no migration:
each row records the field that priced it, and a replay re-reads bronze.
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
from cloudops_connectors.core.raw import BlobRawStore
from cloudops_connectors.core.sync import CUSTOMER_SYNC
from cloudops_connectors.servicenow.auth import ClientCredentials
from cloudops_connectors.servicenow.client import IntegrationUserError, ServiceNowClient
from cloudops_connectors.servicenow.fake import FIXTURE_HOST, FakeServiceNow
from cloudops_connectors.servicenow.mapping import ALL_ENTITIES, PROJECT_ENTITY, price_field
from cloudops_connectors.servicenow.workflows import (
    TASK_QUEUE,
    WORKFLOWS,
    IngestRequest,
    ReplayRequest,
    ServiceNowActivities,
    ServiceNowIngest,
    ServiceNowReplay,
    ServiceNowReprocess,
    activities,
)
from cloudops_observability import TelemetryConfigError
from cloudops_observability.process import start, temporal_options
from cloudops_tenancy import Database

AZURITE = "UseDevelopmentStorage=true"
SCHEDULE_EVERY = timedelta(minutes=5)  # LLD: ServiceNow incremental sync
# Business data that changes slowly: every six hours (ADR 0037, ADR 0038).
EVERY: dict[str, timedelta] = {PROJECT_ENTITY: CUSTOMER_SYNC}
# A person waits on a re-process request; an idle run is one query.
REPROCESS_EVERY = timedelta(minutes=1)
REPROCESS_SCHEDULE = "servicenow-reprocess"


def _container(settings: Settings) -> ContainerClient:
    blob = settings.blob
    if blob.auth is AuthMode.AZURITE_DEV_ACCOUNT:
        return ContainerClient.from_connection_string(AZURITE, "bronze")
    from azure.identity.aio import WorkloadIdentityCredential

    return ContainerClient.from_container_url(
        blob.bronze_container_url, credential=WorkloadIdentityCredential()
    )


def _price_field() -> str:
    try:
        return price_field(os.environ)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from None


def _client(settings: Settings, fixtures: bool) -> ServiceNowClient:
    if fixtures:
        if settings.environment is not Environment.LOCAL:
            raise ConfigurationError("--fixtures is local-only")
        return ServiceNowClient(FIXTURE_HOST, transport=FakeServiceNow.from_fixtures().transport())
    sn = settings.servicenow
    auth = ClientCredentials(f"{sn.instance_url}/oauth_token.do", sn.client_id,
                             lambda: settings.secrets.get(sn.client_secret))
    return ServiceNowClient(sn.instance_url, auth=auth)


async def _main(args: argparse.Namespace) -> int:
    if args.command == "schedule":
        # Temporal only: the schedule job needs no database, storage or instance.
        settings = load_settings(os.environ, require={Component.TEMPORAL})
        temporal = await connect(settings.temporal, **temporal_options())
        for entity in args.entity or sorted(ALL_ENTITIES):
            print(await _schedule(temporal, entity))
        if not args.entity:
            print(await _schedule_reprocess(temporal))
        return 0
    wanted = {Component.DATABASE_INGEST, Component.BLOB, Component.TEMPORAL}
    if not args.fixtures:
        wanted.add(Component.SERVICENOW)
    settings = load_settings(os.environ, require=wanted)
    temporal = await connect(settings.temporal, **temporal_options())

    async with AsyncExitStack() as stack:
        db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=2)
        stack.push_async_callback(db.dispose)
        container = _container(settings)
        stack.push_async_callback(container.close)
        client = _client(settings, args.fixtures)
        stack.push_async_callback(client.aclose)
        if not args.fixtures:
            print(f"reading as {await client.check_integration_user()}")
        acts = ServiceNowActivities(db, BlobRawStore(container), client,
                                    price_field=_price_field())
        worker = Worker(temporal, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
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
            stack.push_async_callback(_close_server, probes)
            print(f"worker serving {TASK_QUEUE}")
            await run_worker(worker, settings.temporal)
            return 0
        async with worker:
            if args.command == "reprocess":
                queue = await temporal.execute_workflow(
                    ServiceNowReprocess.run, id=f"servicenow-reprocess-{uuid.uuid4().hex[:8]}",
                    task_queue=TASK_QUEUE,
                )
                print(json.dumps(asdict(queue), indent=2))
                return 0
            for entity in args.entity or sorted(ALL_ENTITIES):
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


def _schedule_for(entity: str) -> Schedule:
    # "since" applies only before the entity's first watermark; after that the
    # watermark decides where each run starts.
    since = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ServiceNowIngest.run, IngestRequest(entity, since),
            id=f"servicenow-{entity}", task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every(entity))]),
    )


def every(entity: str) -> timedelta:
    return EVERY.get(entity, SCHEDULE_EVERY)


async def _schedule(temporal: Client, entity: str) -> str:
    """Create the entity's schedule, or bring an existing one in line: the job runs
    on every sync, so it must be safe to repeat."""
    return await _upsert(temporal, f"servicenow-{entity}", _schedule_for(entity),
                         every(entity))


async def _schedule_reprocess(temporal: Client) -> str:
    wanted = Schedule(
        action=ScheduleActionStartWorkflow(
            ServiceNowReprocess.run, id=REPROCESS_SCHEDULE, task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=REPROCESS_EVERY)]),
    )
    return await _upsert(temporal, REPROCESS_SCHEDULE, wanted, REPROCESS_EVERY)


async def _upsert(
    temporal: Client, schedule_id: str, wanted: Schedule, every: timedelta,
) -> str:
    try:
        await temporal.create_schedule(schedule_id, wanted)
    except ScheduleAlreadyRunningError:
        def update(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=wanted)

        await temporal.get_schedule_handle(schedule_id).update(update)
        return f"updated {schedule_id}: every {every}"
    return f"created {schedule_id}: every {every}"


async def _close_server(server: asyncio.Server) -> None:
    server.close()
    await server.wait_closed()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_connectors.servicenow")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "replay", "reprocess", "worker", "schedule"):
        p = sub.add_parser(name)
        p.add_argument("--fixtures", action="store_true", help="serve recorded fixtures (local)")
        p.add_argument("--entity", action="append", choices=sorted(ALL_ENTITIES))
        if name == "run":
            p.add_argument("--since", default=(datetime.now(UTC) - timedelta(days=120)).isoformat(),
                           help="ISO time to start from when no watermark exists")
        if name == "replay":
            p.add_argument("--start", required=True, help="first bronze day, YYYY-MM-DD")
            p.add_argument("--end", required=True, help="last bronze day, YYYY-MM-DD")
    args = parser.parse_args(argv)
    try:
        telemetry = start("connector-servicenow", os.environ)
        try:
            return asyncio.run(_main(args))
        finally:
            telemetry.shutdown()
    except (ConfigurationError, IntegrationUserError, TelemetryConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
