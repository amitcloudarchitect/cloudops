"""The Azure connector end to end: the generic connector workflow on a
time-skipping Temporal server, the migrated database (a module of its own,
holding the synthetic tenants, aliases, accounts and inventory), and the
recorded fixtures served by the fake management plane through the real client.

Ground truth comes from ``cloudops_devdata.azure_fixtures.expectations()``:
every row's tenant (or quarantine), and what the resource must look like.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import FakeAzure, load_fixture
from cloudops_connectors.azure.source import SUBS_SUFFIX, AzureSource
from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    PageOutcome,
    ProcessRequest,
    ReplayRequest,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_devdata.azure_fixtures import UNREGISTERED, Expected, expectations
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

ENTITY = mapping.ENTITY
_RESOURCES = (
    "SELECT r.native_id, r.id, r.tenant_id, a.native_id, r.name, r.type_native,"
    " r.type_canonical, r.region, r.tags, r.operating_system, r.state, r.source_system,"
    " r.deleted_at, r.raw_extra"
    " FROM cloudops.resource r JOIN cloudops.cloud_account a ON a.id = r.account_id"
    " WHERE a.provider = 'azure'"
)


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _source(fake: FakeAzure, page_size: int = 25, batch_size: int = 3) -> AzureSource:
    base = f"https://t{uuid.uuid4().hex[:8]}.management.example"
    return AzureSource(AzureClient(base, transport=fake.transport(), page_size=page_size,
                                   sleep=no_sleep), batch_size=batch_size)


def _clear(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as cx:
        for table in ("staging.tenant_alias_quarantine", "staging.schema_field"):
            cx.execute(f"DELETE FROM {table}")


def _resources(dsn: str) -> dict[str, tuple[Any, ...]]:
    return {r[0]: r for r in rows(dsn, _RESOURCES)}


def _live(e: Expected) -> bool:
    return e.tenant_code is not None and not e.malformed


def _watermark(dsn: str, source: AzureSource) -> list[tuple[Any, ...]]:
    return rows(dsn, "SELECT watermark FROM staging.ingest_watermark WHERE scope_key = %s",
                source.instance)


async def test_ingest_lands_every_resource_where_the_ground_truth_says(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    deleted_before = {r[0]: r[12] for r in _resources(dsn).values() if r[12] is not None}
    aws_before = rows(dsn, "SELECT r.id, r.last_seen, r.deleted_at FROM cloudops.resource r"
                           " JOIN cloudops.cloud_account a ON a.id = r.account_id"
                           " WHERE a.provider = 'aws' ORDER BY 1")
    raw, fake = MemoryRawStore(), FakeAzure.from_fixtures()
    source = _source(fake)
    (report,) = await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
                          [IngestRequest(ENTITY, SINCE)])
    truth = expectations()
    good = [e for e in truth if _live(e)]
    held = [e for e in truth if e.tenant_code is None]
    subs = mapping.subscriptions(load_fixture("subscriptions.json"))
    assert report.status == "succeeded"
    c = report.counts
    assert c.pages > 3, "25 rows per page, three subscriptions per batch"
    assert c.records == len(subs) + len(truth)
    # Subscriptions and resources: the listing's known accounts, then the rows.
    assert (c.written, c.quarantined, c.rejected) == (
        len(subs) - 1 + len(good), 1 + len(held), 2)

    stored = _resources(dsn)
    for e in good:
        (_, rid, tenant, sub, name, type_native, canonical, region, tags, os_name, st,
         system, deleted, _extra) = stored[e.native_id or ""]
        assert tenant == tenant_id(e.tenant_code or "")
        assert sub == e.subscription_id
        if e.resource_id is not None:
            assert rid == e.resource_id, "the synthetic row was updated, not duplicated"
        assert (name, type_native, canonical, region, tags, os_name, st) == (
            e.name, e.type_native, e.type_canonical, e.region, e.tags, e.operating_system,
            e.state), e.native_id
        assert (system, deleted) == ("azure", None)
    # Unknown type kept as "other"; an unmapped field kept in raw_extra.
    assert any(stored[e.native_id or ""][6] == mapping.OTHER for e in good)
    zonal = next(r for r in stored.values() if r[4] == "nwl-weu-zonal-01")
    assert zonal[13]["zones"] == ["2"]
    assert zonal[13]["resourceGroup"] == "rg-nwl-prod"
    # The planted instruction is a tag value, stored verbatim.
    assert any("Ignore previous instructions" in str(r[8]) for r in stored.values())

    # The unmapped subscription: quarantined, sighted once in the listing and
    # once per row; nothing it carries is written.
    assert rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                     " WHERE source_system = 'azure'") == [(UNREGISTERED, 1 + len(held))]
    assert rows(dsn, "SELECT count(*) FROM cloudops.cloud_account WHERE native_id = %s",
                UNREGISTERED) == [(0,)]
    assert not any(k.startswith(f"/subscriptions/{UNREGISTERED}/") for k in stored)
    # Accounts named as the listing names them.
    names = dict(rows(dsn, "SELECT native_id, display_name FROM cloudops.cloud_account"
                           " WHERE provider = 'azure'"))
    assert names == {s.subscription_id: s.display_name for s in subs
                     if s.subscription_id != UNREGISTERED}

    # The snapshot closed nothing that is there, and kept history as it was.
    assert {k: v[12] for k, v in stored.items() if v[12] is not None} == deleted_before
    assert rows(dsn, "SELECT r.id, r.last_seen, r.deleted_at FROM cloudops.resource r"
                     " JOIN cloudops.cloud_account a ON a.id = r.account_id"
                     " WHERE a.provider = 'aws' ORDER BY 1") == aws_before

    assert raw_first(raw.events)
    assert all(r.startswith("azure/resource/") for r in raw.pages)
    assert sum(r.endswith(SUBS_SUFFIX) for r in raw.pages) == 1
    (mark,) = _watermark(dsn, source)
    assert mark[0] is not None
    assert report.scopes[0].watermark == mark[0].isoformat()
    # The first page is the baseline; ``zones``, first sent on a later page, is
    # the only field that can be new.
    assert set(rows(dsn, "SELECT field, drift FROM staging.schema_field"
                         " WHERE drift IS NOT NULL")) <= {("zones", "added")}
    assert rows(dsn, "SELECT status, written, quarantined, rejected"
                     " FROM staging.ingest_run WHERE id = %s", uuid.UUID(report.run_id)) == [
        ("succeeded", c.written, c.quarantined, 2)]


async def test_a_second_run_duplicates_nothing_and_moves_last_seen(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    acts = ConnectorActivities(db, MemoryRawStore(), _source(FakeAzure.from_fixtures()))
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    count = "SELECT count(*) FROM cloudops.resource"
    before = rows(dsn, count)
    seen = dict(rows(dsn, "SELECT native_id, last_seen FROM cloudops.resource"
                          " WHERE source_system = 'azure'"))
    (second,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert second.status == "succeeded"
    assert rows(dsn, count) == before
    after = dict(rows(dsn, "SELECT native_id, last_seen FROM cloudops.resource"
                           " WHERE source_system = 'azure'"))
    assert after.keys() == seen.keys()
    assert all(after[k] > seen[k] for k in seen)
    assert rows(dsn, "SELECT count(*) FROM cloudops.cloud_account WHERE provider = 'azure'"
                ) == [(len(mapping.subscriptions(load_fixture("subscriptions.json"))) - 1,)]


class _FailsALaterPage(ConnectorActivities):
    @activity.defn(name="process")
    async def process(self, req: ProcessRequest) -> PageOutcome:
        if req.raw_ref.endswith("-00002.json"):
            raise RuntimeError("normaliser defect")
        return await super().process(req)


async def test_a_removed_resource_is_deleted_only_by_a_complete_run(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    """The snapshot is authoritative only when it is whole: a run that
    dead-letters part-way has not looked at every page, so it deletes nothing."""
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    fake = FakeAzure.from_fixtures()
    source = _source(fake)
    raw = MemoryRawStore()
    await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
              [IngestRequest(ENTITY, SINCE)])
    mark = _watermark(dsn, source)
    gone = next(e for e in expectations() if _live(e) and e.resource_id is not None)
    row = fake.remove(gone.native_id or "")
    deleted = "SELECT deleted_at FROM cloudops.resource WHERE id = %s"

    (failed,) = await run(env, _FailsALaterPage(db, raw, source), ConnectorIngest.run,
                          [IngestRequest(ENTITY, SINCE)])
    assert failed.status == "dead_lettered"
    assert failed.counts.pages == 2, "the listing and the first page committed"
    assert rows(dsn, deleted, gone.resource_id) == [(None,)]
    assert rows(dsn, "SELECT count(*) FROM cloudops.resource WHERE deleted_at > now()"
                     " - interval '1 hour' AND source_system = 'azure'") == [(0,)]
    assert _watermark(dsn, source) == mark

    (whole,) = await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
                         [IngestRequest(ENTITY, SINCE)])
    assert whole.status == "succeeded"
    (when,) = rows(dsn, deleted, gone.resource_id)
    assert when[0] is not None
    assert rows(dsn, "SELECT count(*) FROM cloudops.resource WHERE deleted_at = %s",
                when[0]) == [(1,)], "only the resource that went"
    assert _watermark(dsn, source) > mark

    # It comes back: seen after its deletion, so it is live again.
    fake.add(row)
    await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
              [IngestRequest(ENTITY, SINCE)])
    assert rows(dsn, deleted, gone.resource_id) == [(None,)]


async def test_replay_reprocesses_bronze_without_refetching_closing_or_moving_the_watermark(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    raw, fake = MemoryRawStore(), FakeAzure.from_fixtures()
    source = _source(fake)
    acts = ConnectorActivities(db, raw, source)
    (first,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    # A resource this run never saw: a snapshot close would delete it, replay must not.
    stray = uuid.uuid4()
    rows(dsn, "INSERT INTO cloudops.resource (id, tenant_id, account_id, native_id, name,"
              " type_native, type_canonical, last_seen)"
              " SELECT %s, tenant_id, id, '/stray', 'stray', 'x', 'other',"
              "        now() - interval '1 day'"
              " FROM cloudops.cloud_account WHERE provider = 'azure' ORDER BY native_id"
              " LIMIT 1 RETURNING id", stray)
    snapshot = sorted(_resources(dsn).values(), key=lambda r: str(r[0]))
    mark = _watermark(dsn, source)
    requests = len(fake.requests)
    on = "-".join(next(iter(raw.pages)).split("/")[2:5])
    try:
        (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ENTITY, on, on)])
        assert replay.status == "succeeded"
        assert len(fake.requests) == requests, "replay never touches the source"
        assert replay.counts.written == first.counts.written
        assert sorted(_resources(dsn).values(), key=lambda r: str(r[0])) == snapshot
        assert _watermark(dsn, source) == mark
    finally:
        rows(dsn, "DELETE FROM cloudops.resource WHERE id = %s RETURNING id", stray)


async def test_schema_drift_is_recorded_after_the_baseline(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    fake = FakeAzure.from_fixtures()
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake, page_size=1000,
                                                             batch_size=100))
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    # Resource Graph starts sending a new column and stops sending location.
    for row in fake.rows:
        if isinstance(row, dict):
            row["extendedLocation"] = {"type": "EdgeZone", "name": "ams-edge"}
            row.pop("location", None)
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    drift = dict(rows(dsn, "SELECT field, drift FROM staging.schema_field"
                           " WHERE drift IS NOT NULL AND source_system = 'azure'"))
    assert drift == {"extendedLocation": "added", "location": "missing"}
    # The new column's values were kept, not dropped.
    assert rows(dsn, "SELECT count(*) FROM cloudops.resource"
                     " WHERE raw_extra->'extendedLocation'->>'name' = 'ams-edge'")[0][0] > 0
