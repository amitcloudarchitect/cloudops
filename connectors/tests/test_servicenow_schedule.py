"""The schedule command runs as a Job on every Argo CD sync (ADR 0010), so it
must be safe to repeat: create once, then bring the schedule back in line."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from temporalio.client import Client, Schedule, ScheduleAlreadyRunningError, ScheduleUpdate

from cloudops_connectors.servicenow.__main__ import (
    REPROCESS_EVERY,
    SCHEDULE_EVERY,
    _schedule,
    _schedule_reprocess,
)
from cloudops_connectors.servicenow.workflows import TASK_QUEUE


class FakeHandle:
    def __init__(self, store: dict[str, Schedule], schedule_id: str) -> None:
        self.store, self.id = store, schedule_id

    async def update(self, updater: Any) -> None:
        result = updater(None)
        assert isinstance(result, ScheduleUpdate)
        self.store[self.id] = result.schedule


class FakeClient:
    def __init__(self) -> None:
        self.schedules: dict[str, Schedule] = {}

    async def create_schedule(self, schedule_id: str, schedule: Schedule) -> None:
        if schedule_id in self.schedules:
            raise ScheduleAlreadyRunningError()
        self.schedules[schedule_id] = schedule

    def get_schedule_handle(self, schedule_id: str) -> FakeHandle:
        return FakeHandle(self.schedules, schedule_id)


async def test_the_first_run_creates_and_every_later_run_updates() -> None:
    fake = FakeClient()
    client = cast(Client, fake)
    assert (await _schedule(client, "incident")).startswith("created servicenow-incident")
    fake.schedules["servicenow-incident"].spec.intervals[0].every = timedelta(hours=1)  # drift
    assert (await _schedule(client, "incident")).startswith("updated servicenow-incident")
    schedule = fake.schedules["servicenow-incident"]
    assert schedule.spec.intervals[0].every == SCHEDULE_EVERY == timedelta(minutes=5)
    action = schedule.action
    assert getattr(action, "id", None) == "servicenow-incident"
    assert getattr(action, "task_queue", None) == TASK_QUEUE


async def test_the_reprocess_queue_is_scheduled_every_minute_and_kept_in_line() -> None:
    """ADR 0015: re-process requests are picked up by the worker, never by the API."""
    fake = FakeClient()
    client = cast(Client, fake)
    assert (await _schedule_reprocess(client)).startswith("created servicenow-reprocess")
    assert (await _schedule_reprocess(client)).startswith("updated servicenow-reprocess")
    schedule = fake.schedules["servicenow-reprocess"]
    assert schedule.spec.intervals[0].every == REPROCESS_EVERY == timedelta(minutes=1)
    assert getattr(schedule.action, "workflow", None) == "ServiceNowReprocess"
    assert getattr(schedule.action, "task_queue", None) == TASK_QUEUE
