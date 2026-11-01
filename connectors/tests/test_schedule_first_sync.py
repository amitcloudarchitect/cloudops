"""A daily connector schedule runs once when it is first created, so a new
deployment syncs now rather than at the next 00:00 UTC; a later sync of the
same Job (an update) never triggers it again."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleUpdate,
)

from cloudops_connectors.core.cli import upsert_schedule


class FakeHandle:
    def __init__(self, client: FakeClient, schedule_id: str) -> None:
        self.client, self.id = client, schedule_id

    async def update(self, updater: Any) -> None:
        result = updater(None)
        assert isinstance(result, ScheduleUpdate)
        self.client.schedules[self.id] = result.schedule


class FakeClient:
    def __init__(self) -> None:
        self.schedules: dict[str, Schedule] = {}
        self.triggered: list[str] = []

    async def create_schedule(self, schedule_id: str, schedule: Schedule, *,
                              trigger_immediately: bool = False) -> None:
        if schedule_id in self.schedules:
            raise ScheduleAlreadyRunningError()
        self.schedules[schedule_id] = schedule
        if trigger_immediately:
            self.triggered.append(schedule_id)

    def get_schedule_handle(self, schedule_id: str) -> FakeHandle:
        return FakeHandle(self, schedule_id)


def _daily() -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow("ConnectorIngest", id="qualys-detection",
                                           task_queue="connectors-qualys"),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(days=1))]))


async def test_a_new_schedule_is_triggered_once_and_an_update_never() -> None:
    fake = FakeClient()
    client = cast(Client, fake)
    assert await upsert_schedule(client, "qualys-detection", _daily(),
                                 trigger_on_create=True) == "created qualys-detection"
    assert await upsert_schedule(client, "qualys-detection", _daily(),
                                 trigger_on_create=True) == "updated qualys-detection"
    assert fake.triggered == ["qualys-detection"]


async def test_no_trigger_unless_asked() -> None:
    fake = FakeClient()
    await upsert_schedule(cast(Client, fake), "customer-azure-x-resource", _daily())
    assert fake.triggered == []
