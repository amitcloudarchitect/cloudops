"""The telemetry stream end to end: recorded events published to the
in-memory hub (with Capture to bronze), consumed through the real pipeline,
resolved against the synthetic inventory in PostgreSQL and written to a real
column store through the tenancy wrapper. Ground truth from
``cloudops_devdata.telemetry_fixtures.expectations``."""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.telemetry.hub import Capture, InMemoryHub, MemoryCheckpointer
from cloudops_connectors.telemetry.pipeline import TelemetryPipeline
from cloudops_connectors.telemetry.resolve import Resolver
from cloudops_devdata.synthetic import tenant_id
from cloudops_devdata.telemetry_fixtures import UNKNOWN_SUBSCRIPTION, build
from cloudops_tenancy import Database, Role, TenantScope
from cloudops_tenancy.telemetry import Aggregate, Grain, MetricQuery, TelemetryStore

from .conftest import Synthetic, Telemetry
from .connectors import rows

pytestmark = [pytest.mark.integration]

# Relative to now: the store expires rows by the real clock.
ANCHOR = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=10)


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


@pytest.fixture
async def store(telemetry_store: Telemetry) -> AsyncIterator[TelemetryStore]:
    s = TelemetryStore.create(telemetry_store.url, "telemetry", telemetry_store.username,
                              telemetry_store.password)
    yield s
    await s.aclose()


async def _publish(hub: InMemoryHub) -> None:
    events, _ = build(ANCHOR)
    for e in events:
        await hub.publish(e["body"].encode(), e["partition_key"])


async def _stored(store: TelemetryStore, code: str, metric: str) -> Counter[object]:
    q = MetricQuery(metric, ANCHOR - timedelta(hours=2), ANCHOR + timedelta(hours=1),
                    grain=Grain.HOUR, aggregate=Aggregate.COUNT)
    points = await store.query(TenantScope([tenant_id(code)], Role.ENGINEER, "test"), q)
    out: Counter[object] = Counter()
    for p in points:
        out[p.resource_id] += int(p.value)
    return out


async def test_events_land_per_the_ground_truth_and_redelivery_duplicates_nothing(
    db: Database, store: TelemetryStore, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    raw = MemoryRawStore()
    hub = InMemoryHub(8, capture=Capture(raw))
    await _publish(hub)
    _, expected = build(ANCHOR)
    pipeline = TelemetryPipeline(db, store, Resolver(db))
    checkpoints = MemoryCheckpointer()
    counts = await hub.consume(pipeline, checkpoints, batch_size=3)

    stored = [e for e in expected if e.outcome == "stored"]
    assert counts.records == len(expected)
    assert counts.written == len(stored)
    assert counts.rejected == sum(e.outcome == "malformed" for e in expected)
    assert counts.quarantined == sum(e.outcome.endswith("quarantine") for e in expected)
    assert len(raw.pages) == sum(len(v) for v in hub.events.values()), "every event captured"
    assert checkpoints.positions == {p: len(ev) - 1 for p, ev in hub.events.items() if ev}

    want: dict[str, Counter[object]] = {}
    for e in stored:
        want.setdefault(e.tenant_code or "", Counter())[e.resource_id] += 1
    for code, resources in want.items():
        assert await _stored(store, code, "cpu.utilisation") == resources, code
        # On the stream, never on the allowlist: counted, left in the capture.
        assert await _stored(store, code, "disk.queue_depth") == Counter()

    assert rows(dsn, "SELECT occurrence_count FROM staging.tenant_alias_quarantine"
                     " WHERE source_system = 'azure' AND alias = %s",
                UNKNOWN_SUBSCRIPTION) == [(3,)]
    ghosts = rows(dsn, "SELECT external_key, occurrence_count FROM staging.resource_quarantine"
                       " WHERE source_system = 'telemetry'")
    assert len(ghosts) == 1
    assert ghosts[0][0].endswith("/ghost-vm-01")

    # A crash before any checkpoint: everything is delivered again. The store
    # keeps one copy of each batch (same partition and sequence range).
    again = await hub.consume(pipeline, MemoryCheckpointer(), batch_size=3)
    assert again.written == counts.written
    for code, resources in want.items():
        assert await _stored(store, code, "cpu.utilisation") == resources, code

    flushed = await pipeline.flush()
    assert flushed.records == 2 * counts.records
    run = rows(dsn, "SELECT status, records, written, lag_seconds FROM staging.ingest_run"
                    " WHERE source_system = 'telemetry' ORDER BY started_at DESC LIMIT 1")
    assert run[0][:3] == ("succeeded", 2 * counts.records, 2 * counts.written)
    assert run[0][3] is not None
    assert run[0][3] >= 0
    marks = rows(dsn, "SELECT count(*) FROM staging.ingest_watermark"
                      " WHERE source_system = 'telemetry'")
    assert marks[0][0] == sum(1 for ev in hub.events.values() if ev)


async def test_a_failed_store_write_leaves_no_checkpoint_and_no_run_state(
    db: Database, synthetic_module: Synthetic
) -> None:
    from cloudops_connectors.telemetry.consumer import ConsumerHaltedError

    broken = TelemetryStore.create("http://127.0.0.1:9", "telemetry", "u", "p")
    pipeline = TelemetryPipeline(db, broken, Resolver(db))
    hub = InMemoryHub(1)
    await _publish(hub)
    checkpoints = MemoryCheckpointer()

    async def no_sleep(_: float) -> None:
        return None

    from cloudops_connectors.telemetry.consumer import handle_batch

    with pytest.raises(ConsumerHaltedError):
        await handle_batch(pipeline, checkpoints, "0", hub.events["0"][:5], sleep=no_sleep)
    assert checkpoints.positions == {}
    await broken.aclose()
