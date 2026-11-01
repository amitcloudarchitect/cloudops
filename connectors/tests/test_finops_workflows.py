"""The FinOps state machine (CLAUDE.md rule 3): the order of the daily steps, a
failing step failing the run after its retries, and month-end reprocessing.
Activities are stand-ins; the jobs are tested against a database in
db/tests/test_finops_jobs.py."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from cloudops_connectors.finops.workflows import (
    WORKFLOWS,
    DayRequest,
    FinOpsDaily,
    FinOpsRecompute,
    FinOpsReprocess,
    PeriodRequest,
)

QUEUE = "finops-test"


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


def _stubs(calls: list[str], fail: str | None = None) -> list[Any]:
    def stub(name: str) -> Any:
        @activity.defn(name=name)
        async def run(req: Any) -> dict[str, Any]:
            calls.append(name)
            if name == fail:
                raise ValueError("boom")  # ValueError is not retried (core RETRY)
            return {"ok": True}
        return run

    return [stub(n) for n in ("finops_reconcile", "finops_anomalies", "finops_margins",
                              "finops_reprocess", "finops_reconcile_close",
                              "finops_recompute")]


async def _run(env: WorkflowEnvironment, calls: list[str], wf: Any, req: Any,
               fail: str | None = None) -> Any:
    async with Worker(env.client, task_queue=QUEUE, workflows=WORKFLOWS,
                      activities=_stubs(calls, fail)):
        return await env.client.execute_workflow(wf, req, id=f"t-{uuid.uuid4().hex}",
                                                 task_queue=QUEUE)


async def test_daily_reconciles_before_margin(env: WorkflowEnvironment) -> None:
    calls: list[str] = []
    out = await _run(env, calls, FinOpsDaily.run, DayRequest("2026-10-20"))
    assert calls == ["finops_reconcile", "finops_anomalies", "finops_margins"]
    assert set(out) == {"on", "reconcile", "anomalies", "margins"}


async def test_a_failed_step_stops_the_run(env: WorkflowEnvironment) -> None:
    calls: list[str] = []
    with pytest.raises(WorkflowFailureError):
        await _run(env, calls, FinOpsDaily.run, DayRequest("2026-10-20"), fail="finops_anomalies")
    assert "finops_margins" not in calls


async def test_month_end_reprocess(env: WorkflowEnvironment) -> None:
    calls: list[str] = []
    await _run(env, calls, FinOpsReprocess.run, PeriodRequest("2026-09-01"))
    assert calls == ["finops_reconcile_close", "finops_reprocess"]


async def test_a_scheduled_run_takes_the_day_it_starts(env: WorkflowEnvironment) -> None:
    """The schedule stores an empty request: each run resolves its own day, so a
    schedule created once never repeats the day it was created."""
    calls: list[str] = []
    before = (await env.get_current_time()).date().isoformat()
    out = await _run(env, calls, FinOpsDaily.run, DayRequest())
    after = (await env.get_current_time()).date().isoformat()
    # Time skips while the workflow runs; the day is the one it started on.
    assert before <= out["on"] <= after


async def test_recompute_answers_the_waiting_requests(env: WorkflowEnvironment) -> None:
    calls: list[str] = []
    out = await _run(env, calls, FinOpsRecompute.run, DayRequest())
    assert calls == ["finops_recompute"]
    assert out == {"ok": True}
