"""The Qualys connector end to end: the generic connector workflow on a
time-skipping Temporal server, the migrated database (a module of its own,
holding the synthetic tenants, aliases and inventory, and no findings), and the
recorded fixtures served by the fake subscription through the real client.

Ground truth comes from ``cloudops_devdata.qualys_fixtures.expectations()``:
every detection's tenant (or quarantine) and the resource it must link to.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from xml.etree.ElementTree import SubElement

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment

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
from cloudops_connectors.qualys import mapping
from cloudops_connectors.qualys.client import QualysClient
from cloudops_connectors.qualys.fake import FakeQualys, load_fixture
from cloudops_connectors.qualys.source import QualysSource, kb_ref
from cloudops_devdata.qualys_fixtures import INJECTION, expectations
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

ENTITY = mapping.ENTITY


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _source(fake: FakeQualys, page_size: int = 25) -> QualysSource:
    base = f"https://t{uuid.uuid4().hex[:8]}.qualysapi.example"
    return QualysSource(QualysClient(base, transport=fake.transport(), page_size=page_size,
                                     sleep=no_sleep))


def _max_processed() -> Any:
    return max(t for h in mapping.hosts(load_fixture("host_detection.xml"))
               for t in mapping.processed_times(h))


def _clear(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as cx:
        for table in ("cloudops.vulnerability", "staging.tenant_alias_quarantine",
                      "staging.resource_quarantine", "staging.schema_field"):
            cx.execute(f"DELETE FROM {table}")


async def test_ingest_lands_every_detection_where_the_ground_truth_says(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    raw, fake = MemoryRawStore(), FakeQualys.from_fixtures()
    source = _source(fake)
    (report,) = await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
                          [IngestRequest(ENTITY, SINCE)])
    truth = expectations()
    good = [e for e in truth if e.tenant_code and not e.malformed]
    held = [e for e in truth if e.tenant_code is None and not e.malformed]
    assert report.status == "succeeded"
    c = report.counts
    assert c.pages > 2, "25 hosts per page"
    assert (c.written, c.quarantined, c.rejected) == (len(good), len(held), 2)
    assert c.records == len(truth)

    stored = {(r[0], r[1], r[2]): (r[3], r[4]) for r in rows(
        dsn, "SELECT asset_name, qid, first_detected, tenant_id, resource_id"
             " FROM cloudops.vulnerability")}
    assert len(stored) == len(good)
    for e in good:
        tenant, resource = stored[(e.asset_name, e.qid, e.first_detected)]
        assert tenant == tenant_id(e.tenant_code or "")
        assert resource == e.resource_id, (e.asset_name, e.qid)
    linked = sum(1 for e in good if e.resource_id)
    assert 0 < linked < len(good), "some link, some cannot be linked without guessing"

    # Unmatched customer tags quarantined with counts; nothing guessed.
    tags = Counter(t for e in held for t in _tags_of(e.host_id))
    assert dict(rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                          " WHERE source_system = 'qualys'")) == dict(tags)
    assert mapping.NO_CUSTOMER in tags
    # Hosts that matched no resource in their tenant: quarantined by host id.
    unlinked = Counter(f"qualys:{e.host_id}" for e in good if e.resource_id is None)
    assert dict(rows(dsn, "SELECT external_key, occurrence_count"
                          " FROM staging.resource_quarantine")) == dict(unlinked)

    # Raw before parsed, with each page's KnowledgeBase stored beside it.
    assert raw_first(raw.events)
    pages = [r for r in raw.pages if not r.endswith(".kb.xml")]
    assert all(kb_ref(p) in raw.pages for p in pages)
    assert all(r.startswith("qualys/host_detection/") for r in raw.pages)

    # Fields stored verbatim; the injection is data in raw_extra, not an instruction.
    extra = rows(dsn, "SELECT raw_extra FROM cloudops.vulnerability"
                      " WHERE raw_extra->>'RESULTS' = %s", INJECTION)
    assert extra, "the hostile results text is kept, untouched"
    assert {"TYPE", "TIMES_FOUND"} <= set(extra[0][0])
    assert rows(dsn, "SELECT count(*) FROM cloudops.vulnerability WHERE title IS NULL") == [(0,)]

    # One watermark, the run's highest processed time; the first run is the baseline.
    assert rows(dsn, "SELECT watermark FROM staging.ingest_watermark WHERE scope_key = %s",
                source.instance) == [(_max_processed(),)]
    assert rows(dsn, "SELECT count(*) FROM staging.schema_field WHERE drift IS NOT NULL"
                ) == [(0,)]
    assert rows(dsn, "SELECT status, written, quarantined, rejected, ci_unlinked"
                     " FROM staging.ingest_run WHERE id = %s", uuid.UUID(report.run_id)) == [
        ("succeeded", len(good), len(held), 2, sum(unlinked.values()))]


def _tags_of(host_id: str) -> tuple[str, ...]:
    for h in mapping.hosts(load_fixture("host_detection.xml")):
        if h["ID"] == host_id:
            found, _ = mapping.normalise(h)
            return found[0].customers
    raise KeyError(host_id)


async def test_a_second_run_reads_only_the_overlap_and_duplicates_nothing(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    fake = FakeQualys.from_fixtures()
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    before = rows(dsn, "SELECT count(*) FROM cloudops.vulnerability")
    (second,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    since = (_max_processed() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert fake.requests[-1].url.params.get("vm_processed_after") == since or any(
        r.url.params.get("vm_processed_after") == since for r in fake.requests[-3:])
    assert second.counts.records < 5
    assert rows(dsn, "SELECT count(*) FROM cloudops.vulnerability") == before


async def test_a_fix_updates_the_finding_in_place(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    fake = FakeQualys.from_fixtures()
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    target = next(e for e in expectations() if e.tenant_code and not e.malformed)
    fake.touch(target.host_id, target.qid, "2026-10-30T08:00:00Z",
               STATUS="Fixed", LAST_FIXED_DATETIME="2026-10-30T07:00:00Z")
    (report,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert report.counts.written >= 1
    got = rows(dsn, "SELECT count(*), max(status), max(remediated_at)::text"
                    " FROM cloudops.vulnerability WHERE asset_name = %s AND qid = %s"
                    " AND first_detected = %s",
               target.asset_name, target.qid, target.first_detected)
    assert got == [(1, "Fixed", "2026-10-30 07:00:00+00")]


class _FailsSecondPage(ConnectorActivities):
    @activity.defn(name="process")
    async def process(self, req: ProcessRequest) -> PageOutcome:
        if req.raw_ref.endswith("-00001.xml"):
            raise RuntimeError("normaliser defect")
        return await super().process(req)


async def test_a_dead_letter_part_way_leaves_the_watermark_where_it_was(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    """Pages come in host order, not time order: the watermark may only move
    once the whole run has committed, or a later run would skip detections on
    the pages after the failure."""
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    raw, source = MemoryRawStore(), _source(FakeQualys.from_fixtures())
    (report,) = await run(env, _FailsSecondPage(db, raw, source), ConnectorIngest.run,
                          [IngestRequest(ENTITY, SINCE)])
    assert report.status == "dead_lettered"
    assert report.counts.pages == 1, "the first page committed"
    assert report.scopes[0].watermark is None
    letters = rows(dsn, "SELECT raw_ref, error FROM staging.ingest_dead_letter"
                        " WHERE run_id = %s", uuid.UUID(report.run_id))
    assert len(letters) == 1
    assert letters[0][0] in raw.pages
    assert "RuntimeError" in letters[0][1]
    assert rows(dsn, "SELECT count(*) FROM staging.ingest_watermark WHERE scope_key = %s",
                source.instance) == [(0,)]


async def test_replay_reprocesses_bronze_without_refetching_or_moving_the_watermark(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    raw, fake = MemoryRawStore(), FakeQualys.from_fixtures()
    source = _source(fake)
    acts = ConnectorActivities(db, raw, source)
    (first,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    snapshot = rows(dsn, "SELECT tenant_id, qid, asset_name, first_detected, resource_id,"
                         " status FROM cloudops.vulnerability ORDER BY 2, 3, 4")
    mark = rows(dsn, "SELECT watermark FROM staging.ingest_watermark WHERE scope_key = %s",
                source.instance)
    requests = len(fake.requests)
    day = next(iter(raw.pages)).split("/")[2:5]
    on = "-".join(day)
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ENTITY, on, on)])
    assert replay.status == "succeeded"
    assert len(fake.requests) == requests, "replay never touches the source"
    assert replay.counts.written == first.counts.written
    assert rows(dsn, "SELECT tenant_id, qid, asset_name, first_detected, resource_id,"
                     " status FROM cloudops.vulnerability ORDER BY 2, 3, 4") == snapshot
    assert rows(dsn, "SELECT watermark FROM staging.ingest_watermark WHERE scope_key = %s",
                source.instance) == mark


async def test_schema_drift_is_recorded_after_the_baseline(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    fake = FakeQualys.from_fixtures()
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake, page_size=1000))
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    # Qualys starts sending a new host field and stops sending TRACKING_METHOD.
    for host in fake.hosts:
        SubElement(host, "ASSET_RISK_SCORE").text = "512"
        tracking = host.find("TRACKING_METHOD")
        if tracking is not None:
            host.remove(tracking)
        for d in host.iterfind("DETECTION_LIST/DETECTION"):
            processed = d.find("LAST_PROCESSED_DATETIME")
            if processed is not None:
                processed.text = "2026-10-30T09:00:00Z"
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    drift = dict(rows(dsn, "SELECT field, drift FROM staging.schema_field"
                           " WHERE drift IS NOT NULL AND source_system = 'qualys'"))
    assert drift == {"ASSET_RISK_SCORE": "added", "TRACKING_METHOD": "missing"}
    # The new field's values were kept, not dropped.
    assert rows(dsn, "SELECT count(*) FROM cloudops.vulnerability"
                     " WHERE raw_extra->'host'->>'ASSET_RISK_SCORE' = '512'")[0][0] > 0
