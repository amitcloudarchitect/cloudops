"""Helpers for the step 15 connector integration tests: run the generic
connector workflows on Temporal's time-skipping test server, and read tables
as the superuser (which bypasses RLS: what the connector wrote, all of it)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

import psycopg
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from cloudops_connectors.core.ingest import (
    WORKFLOWS,
    ConnectorActivities,
    RunReport,
    activities,
)

SINCE = "2026-01-01T00:00:00+00:00"


async def no_sleep(_: float) -> None:
    return None


async def run(
    env: WorkflowEnvironment, acts: ConnectorActivities, workflow: Any, requests: Iterable[Any],
) -> list[RunReport]:
    queue = f"q-{uuid.uuid4().hex[:8]}"
    out: list[RunReport] = []
    async with Worker(env.client, task_queue=queue, workflows=WORKFLOWS,
                      activities=activities(acts)):
        for req in requests:
            out.append(await env.client.execute_workflow(
                workflow, req, id=f"wf-{uuid.uuid4().hex}", task_queue=queue))
    return out


def rows(dsn: str, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as cx:
        return list(cx.execute(sql, params).fetchall())


def raw_first(events: list[tuple[str, str]]) -> bool:
    """Every bronze object was stored before anything read it back."""
    first: dict[str, str] = {}
    for op, ref in events:
        first.setdefault(ref, op)
    return bool(first) and set(first.values()) == {"put"}
