"""Run the FinOps worker.

    python -m cloudops_connectors.finops run [--on YYYY-MM-DD]
    python -m cloudops_connectors.finops reprocess --period YYYY-MM
    python -m cloudops_connectors.finops worker
    python -m cloudops_connectors.finops schedule

``run`` and ``reprocess`` start a worker in this process and execute one workflow;
``worker`` serves the queue with probes; ``schedule`` creates or updates the
daily schedule (safe to repeat). It reads only the database: no source, no
credential.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime, timedelta

from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.worker import Worker

from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_config.probes import serve_probes
from cloudops_config.temporal import connect, run_worker
from cloudops_connectors.finops.workflows import (
    TASK_QUEUE,
    WORKFLOWS,
    DayRequest,
    FinOpsActivities,
    FinOpsDaily,
    FinOpsReprocess,
    PeriodRequest,
    activities,
)
from cloudops_tenancy import Database

SCHEDULE_ID = "finops-daily"


async def _main(args: argparse.Namespace) -> int:
    if args.command == "schedule":
        settings = load_settings(os.environ, require={Component.TEMPORAL})
        temporal = await connect(settings.temporal)
        wanted = Schedule(
            action=ScheduleActionStartWorkflow(
                FinOpsDaily.run, DayRequest(datetime.now(UTC).date().isoformat()),
                id=SCHEDULE_ID, task_queue=TASK_QUEUE),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(days=1))]),
        )
        try:
            await temporal.create_schedule(SCHEDULE_ID, wanted)
            print(f"created {SCHEDULE_ID}")
        except ScheduleAlreadyRunningError:
            def update(_: ScheduleUpdateInput) -> ScheduleUpdate:
                return ScheduleUpdate(schedule=wanted)

            await temporal.get_schedule_handle(SCHEDULE_ID).update(update)
            print(f"updated {SCHEDULE_ID}")
        return 0
    settings = load_settings(os.environ, require={Component.DATABASE_INGEST, Component.TEMPORAL})
    temporal = await connect(settings.temporal)
    async with AsyncExitStack() as stack:
        db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=2)
        stack.push_async_callback(db.dispose)
        worker = Worker(temporal, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                        activities=activities(FinOpsActivities(db)))
        if args.command == "worker":
            async def temporal_ready() -> None:
                if not await temporal.service_client.check_health():
                    raise ConnectionError("temporal unhealthy")

            local = settings.environment is Environment.LOCAL
            probes = await serve_probes(
                {"database": db.ping, "temporal": temporal_ready},
                host="127.0.0.1" if local else "0.0.0.0",  # noqa: S104 - the kubelet probes the pod IP
            )

            async def close() -> None:
                probes.close()
                await probes.wait_closed()

            stack.push_async_callback(close)
            print(f"worker serving {TASK_QUEUE}")
            await run_worker(worker, settings.temporal)
            return 0
        async with worker:
            wf_id = f"finops-{args.command}-{uuid.uuid4().hex[:8]}"
            if args.command == "run":
                report = await temporal.execute_workflow(
                    FinOpsDaily.run, DayRequest(args.on), id=wf_id, task_queue=TASK_QUEUE)
            else:
                report = await temporal.execute_workflow(
                    FinOpsReprocess.run, PeriodRequest(f"{args.period}-01"), id=wf_id,
                    task_queue=TASK_QUEUE)
            print(json.dumps(report, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_connectors.finops")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--on", default=datetime.now(UTC).date().isoformat(),
                     type=lambda v: date.fromisoformat(v).isoformat(),
                     help="the business day; the run covers the days before it")
    rep = sub.add_parser("reprocess")
    rep.add_argument("--period", required=True,
                     type=lambda v: date.fromisoformat(f"{v}-01").strftime("%Y-%m"),
                     help="the closed month, YYYY-MM")
    sub.add_parser("worker")
    sub.add_parser("schedule")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
