"""ServiceNow project tasks end to end (ADR 0038): the ingest workflow on a
time-skipping Temporal server, the real migrated database, and the synthetic
fixtures served by the fake instance through the real client.

Ground truth is ``project_expectations()``, built with the fixtures. A database
of this module's own (``synthetic_module``), so the quarantine and run log hold
only what these tests put there.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.servicenow.client import ServiceNowClient
from cloudops_connectors.servicenow.fake import FakeServiceNow, load_fixture
from cloudops_connectors.servicenow.mapping import PROJECT_ENTITY
from cloudops_connectors.servicenow.workflows import (
    WORKFLOWS,
    IngestRequest,
    ReplayRequest,
    RunReport,
    ServiceNowActivities,
    ServiceNowIngest,
    ServiceNowReplay,
    activities,
)
from cloudops_devdata.servicenow_fixtures import project_expectations
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database

from .conftest import Synthetic

pytestmark = [pytest.mark.integration]

SINCE = "2026-01-01T00:00:00+00:00"


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture
async def ingest_db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    db = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield db
    await db.dispose()


def _rows(dsn: str, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as cx:
        return list(cx.execute(sql, params).fetchall())


async def _run(env: WorkflowEnvironment, acts: ServiceNowActivities, workflow: Any,
               req: Any) -> RunReport:
    queue = f"q-{uuid.uuid4().hex[:8]}"
    async with Worker(env.client, task_queue=queue, workflows=WORKFLOWS,
                      activities=activities(acts)):
        report: RunReport = await env.client.execute_workflow(
            workflow, req, id=f"wf-{uuid.uuid4().hex}", task_queue=queue)
    return report


def _acts(db: Database, raw: MemoryRawStore, fake: FakeServiceNow,
          price_field: str = "cost") -> tuple[ServiceNowActivities, str]:
    base = f"https://t{uuid.uuid4().hex[:8]}.service-now.example"
    client = ServiceNowClient(base, transport=fake.transport(), sleep=_no_sleep)
    return ServiceNowActivities(db, raw, client, price_field=price_field), base


async def test_project_tasks_land_where_the_ground_truth_says(
    env: WorkflowEnvironment, ingest_db: Database, synthetic_module: Synthetic
) -> None:
    raw, fake = MemoryRawStore(), FakeServiceNow.from_fixtures()
    acts, base = _acts(ingest_db, raw, fake)
    report = await _run(env, acts, ServiceNowIngest.run, IngestRequest(PROJECT_ENTITY, SINCE))
    truth = project_expectations()
    held = [e for e in truth if e.status is not None and e.tenant_code is None]
    good = [e for e in truth if e.tenant_code is not None]
    assert report.status == "succeeded"
    c = report.counts
    assert (c.records, c.written, c.quarantined, c.rejected) == (len(truth), len(good),
                                                                 len(held), 1)

    rows = {r[0]: r[1:] for r in _rows(
        synthetic_module.admin_dsn,
        "SELECT number, tenant_id, status, price, price_currency, price_field"
        " FROM cloudops.project_task")}
    for e in truth:
        if e.tenant_code is None:
            assert e.number not in rows, f"{e.number} must not be written"
            continue
        price = None if e.price is None else Decimal(e.price)
        assert rows[e.number] == (tenant_id(e.tenant_code), e.status, price, e.currency,
                                  "cost" if price is not None else None), e.number

    # Raw before parsed, under the entity's own prefix.
    first_seen: dict[str, str] = {}
    for op, ref in raw.events:
        first_seen.setdefault(ref, op)
    assert set(first_seen.values()) == {"put"}
    assert all(ref.startswith(f"servicenow/{PROJECT_ENTITY}/") for ref in raw.pages)

    # The unmatched company held with its count, never guessed.
    held_alias = next(r["company"]["display_value"] for r in load_fixture(PROJECT_ENTITY)
                      if r["number"]["value"] == held[0].number)
    assert _rows(synthetic_module.admin_dsn,
                 "SELECT occurrence_count FROM staging.tenant_alias_quarantine"
                 " WHERE source_system = 'servicenow' AND alias = %s", held_alias) == [(1,)]

    # The watermark moved with the committed batch; unknown fields kept.
    newest = max(r["sys_updated_on"]["value"] for r in load_fixture(PROJECT_ENTITY))
    mark = _rows(synthetic_module.admin_dsn,
                 "SELECT to_char(watermark AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
                 " FROM staging.ingest_watermark WHERE entity = %s AND scope_key = %s",
                 PROJECT_ENTITY, base)
    assert mark == [(newest,)]
    extra = _rows(synthetic_module.admin_dsn,
                  "SELECT raw_extra FROM cloudops.project_task LIMIT 1")[0][0]
    assert {"cost", "work_cost", "percent_complete"} <= set(extra)


async def test_a_changed_price_field_needs_a_replay_not_a_migration(
    env: WorkflowEnvironment, ingest_db: Database, synthetic_module: Synthetic
) -> None:
    """Which cost stands for the price is a setting (ADR 0038). Switching it and replaying
    bronze re-prices every task from the new field, recorded on each row."""
    raw, fake = MemoryRawStore(), FakeServiceNow.from_fixtures()
    acts, _ = _acts(ingest_db, raw, fake)
    await _run(env, acts, ServiceNowIngest.run, IngestRequest(PROJECT_ENTITY, SINCE))
    switched = ServiceNowActivities(ingest_db, raw, acts._client, price_field="work_cost")
    day = next(iter(raw.pages)).split("/")[2:5]
    on = "-".join(day)
    report = await _run(env, switched, ServiceNowReplay.run,
                        ReplayRequest(PROJECT_ENTITY, on, on))
    assert report.status == "succeeded"
    # work_cost carries "0" with no currency code: no price, never a guessed one.
    assert _rows(synthetic_module.admin_dsn,
                 "SELECT count(*) FROM cloudops.project_task WHERE price IS NOT NULL") == [(0,)]


async def test_a_source_without_the_table_is_a_state_not_a_failure(
    env: WorkflowEnvironment, ingest_db: Database, synthetic_module: Synthetic
) -> None:
    """dev407774 without Project Portfolio Management answers 400 Invalid table."""
    raw = MemoryRawStore()
    fake = FakeServiceNow({"incident": load_fixture("incident")})
    acts, _ = _acts(ingest_db, raw, fake)
    report = await _run(env, acts, ServiceNowIngest.run, IngestRequest(PROJECT_ENTITY, SINCE))
    assert report.status == "source_absent"
    assert report.watermark is None
    assert not raw.pages
    assert len(fake.requests) == 1, "not retried: the table will not appear by retrying"
    assert _rows(synthetic_module.admin_dsn,
                 "SELECT status FROM staging.ingest_run WHERE id = %s",
                 uuid.UUID(report.run_id)) == [("source_absent",)]
    with psycopg.connect(synthetic_module.app_dsn) as cx:
        assert cx.execute("SELECT state FROM cloudops.project_task_source()").fetchone() == (
            "source_absent",)
