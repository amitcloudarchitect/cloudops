"""Contract tests for the telemetry stream: normalisation of the recorded
events, checkpointing only after a store write, and Capture independent of the
consumer. No database or store: the pipeline is replaced where it would write."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import pytest

from cloudops_connectors.core import state
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.telemetry.consumer import ConsumerHaltedError, handle_batch
from cloudops_connectors.telemetry.hub import Capture, InMemoryHub, MemoryCheckpointer
from cloudops_connectors.telemetry.normalise import AWS, AZURE, METRICS, parse
from cloudops_connectors.telemetry.pipeline import HubEvent, TelemetryPipeline, dedupe_token
from cloudops_devdata.telemetry_fixtures import build, render

T0 = datetime(2026, 10, 27, 11, 5, tzinfo=UTC)


async def _no_sleep(_: float) -> None:
    return None


def _azure(**kw: Any) -> bytes:
    record = {"time": "2026-10-27T11:05:00.0000000Z",
              "resourceId": "/SUBSCRIPTIONS/AB12/RESOURCEGROUPS/RG/PROVIDERS/"
                            "MICROSOFT.COMPUTE/VIRTUALMACHINES/VM01",
              "metricName": "Percentage CPU", "timeGrain": "PT1M", "count": 1, "total": 30.0,
              "minimum": 30.0, "maximum": 30.0, "average": 30.0, "newField": "x"}
    record.update(kw)
    return json.dumps({"records": [record]}).encode()


# --- normalisation ------------------------------------------------------------------------


def test_an_azure_record_maps_to_the_canonical_model() -> None:
    (m,) = parse(_azure()).metrics
    assert (m.provider, m.account, m.metric, m.unit, m.value) == (
        AZURE, "ab12", "cpu.utilisation", "percent", 30.0)
    assert m.resource_key.startswith("/subscriptions/ab12/")  # case-insensitive ids
    assert m.ts == T0
    assert "newField" in m.fields


def test_vm_availability_is_a_percentage() -> None:
    (m,) = parse(_azure(metricName="VmAvailabilityMetric", average=1.0)).metrics
    assert (m.metric, m.value) == ("availability", 100.0)


def test_a_cloudwatch_stream_line_maps_too() -> None:
    line = json.dumps({"account_id": "123456789012", "region": "eu-west-1",
                       "namespace": "AWS/EC2", "metric_name": "CPUUtilization",
                       "dimensions": {"InstanceId": "i-0ABC"}, "timestamp": 1_800_000_000_000,
                       "value": {"sum": 120.0, "count": 4.0, "min": 1, "max": 50},
                       "unit": "Percent"})
    (m,) = parse(f"{line}\n{line}".encode()).metrics[:1]
    assert (m.provider, m.account, m.resource_key, m.metric, m.value) == (
        AWS, "123456789012", "i-0abc", "cpu.utilisation", 30.0)
    assert m.ts == datetime.fromtimestamp(1_800_000_000, UTC)


def test_status_check_failed_becomes_availability() -> None:
    line = json.dumps({"account_id": "1", "namespace": "AWS/EC2",
                       "metric_name": "StatusCheckFailed", "dimensions": {"InstanceId": "i-1"},
                       "timestamp": 0, "value": {"sum": 1.0, "count": 4.0}})
    assert parse(line.encode()).metrics[0].value == 75.0


@pytest.mark.parametrize(
    "body",
    [b"not json", b"\xff\xfe", json.dumps({"records": [{"metricName": "Percentage CPU"}]}).encode(),
     _azure(resourceId="/tenants/x/providers/y"), _azure(average=None),
     _azure(time="2026-10-27T11:05:00"), _azure(average=float("inf"))],
)
def test_malformed_records_are_counted_never_guessed(body: bytes) -> None:
    parsed = parse(body)
    assert parsed.metrics == []
    assert parsed.malformed == parsed.records == 1


def test_a_metric_without_a_mapping_is_not_an_error() -> None:
    parsed = parse(_azure(metricName="Custom Heartbeat"))
    assert (parsed.records, parsed.malformed, parsed.unmapped, parsed.metrics) == (1, 0, 1, [])


def test_every_canonical_name_fits_the_store() -> None:
    import re

    assert all(re.fullmatch(r"[a-z0-9_.]{1,64}", m) for m, _, _ in METRICS.values())


def test_committed_fixtures_equal_the_generator() -> None:
    events, _ = build()
    text = resources.files("cloudops_connectors.telemetry").joinpath(
        "fixtures/events.jsonl").read_text("utf-8")
    assert text == render(events)


def test_the_fixture_ground_truth_matches_what_parse_sees() -> None:
    events, expected = build()
    parsed = [parse(e["body"].encode()) for e in events]
    assert sum(p.records for p in parsed) == len(expected)
    assert sum(p.malformed for p in parsed) == sum(e.outcome == "malformed" for e in expected)
    assert sum(p.unmapped for p in parsed) == sum(e.outcome == "unmapped" for e in expected)


# --- checkpoints: only after the batch is stored ---------------------------------------------


class _Pipeline:
    """Stands in for TelemetryPipeline: fails ``fail`` times, records calls."""

    def __init__(self, fail: int = 0) -> None:
        self.fail = fail
        self.calls = 0
        self.failures = 0

    async def handle(self, partition_id: str, events: list[HubEvent]) -> state.RunCounts:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("store unavailable")
        return state.RunCounts(pages=1, records=len(events), written=len(events))

    def failed(self) -> None:
        self.failures += 1


def _events(n: int) -> list[HubEvent]:
    return [HubEvent("3", i, T0, b"{}") for i in range(n)]


async def test_the_checkpoint_moves_after_the_write_to_the_last_event() -> None:
    cp = MemoryCheckpointer()
    pipeline = _Pipeline()
    counts = await handle_batch(pipeline, cp, "3", _events(4), sleep=_no_sleep)  # type: ignore[arg-type]
    assert counts.written == 4
    assert cp.positions == {"3": 3}


async def test_a_failing_write_is_retried_and_only_then_checkpointed() -> None:
    cp = MemoryCheckpointer()
    pipeline = _Pipeline(fail=2)
    await handle_batch(pipeline, cp, "3", _events(2), sleep=_no_sleep)  # type: ignore[arg-type]
    assert (pipeline.calls, pipeline.failures, cp.positions) == (3, 2, {"3": 1})


async def test_a_batch_that_never_stores_halts_without_a_checkpoint() -> None:
    cp = MemoryCheckpointer()
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    with pytest.raises(ConsumerHaltedError):
        await handle_batch(_Pipeline(fail=99), cp, "3", _events(2), sleep=sleep)  # type: ignore[arg-type]
    assert cp.positions == {}
    assert waits == [2.0, 4.0, 8.0, 16.0], "exponential backoff, five attempts"


def test_the_dedupe_token_names_the_partition_and_sequence_range() -> None:
    assert dedupe_token("evh-metrics", "3", _events(5)) == "evh-metrics:3:0-4"


# --- capture: the bronze copy is written in parallel, not downstream --------------------------


async def test_capture_writes_every_event_at_publish_whether_or_not_it_is_consumed() -> None:
    raw = MemoryRawStore()
    hub = InMemoryHub(4, capture=Capture(raw), clock=lambda: T0)
    for i in range(10):
        await hub.publish(_azure(average=float(i)), partition_key=f"vm{i}")
    assert len(raw.pages) == 10
    ref = next(iter(raw.pages))
    assert ref.startswith("evhns-cloudops-local/evh-metrics/")
    assert "/2026/10/27/11/05/" in ref
    stored = {raw.pages[r] for r in raw.pages}
    assert _azure(average=3.0) in stored, "unmodified bytes"
    # Nothing consumed, yet everything is in bronze.
    assert sum(len(e) for e in hub.events.values()) == 10


async def test_the_hub_resumes_from_the_checkpoint() -> None:
    hub = InMemoryHub(1, clock=lambda: T0)
    for i in range(5):
        await hub.publish(b"{}", partition_key=str(i))
    pipeline, cp = _Pipeline(), MemoryCheckpointer()
    await hub.consume(pipeline, cp, start={"0": 2}, batch_size=10)  # type: ignore[arg-type]
    assert cp.positions == {"0": 4}
    assert pipeline.calls == 1


def test_the_real_pipeline_class_has_the_shape_the_consumer_uses() -> None:
    assert callable(TelemetryPipeline.handle)
    assert callable(TelemetryPipeline.failed)
