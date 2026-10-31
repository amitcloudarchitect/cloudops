"""An in-memory Event Hub, for tests and the local profile, with Capture.

There is no Event Hubs emulator in the local stack, so locally events are
published here and consumed through the same ``handle_batch`` the SDK binding
uses. ``Capture`` stands in for Event Hubs Capture: every published event is
written to bronze *at publish time*, unmodified, independent of whether any
consumer reads it, which is what makes the bronze copy parallel rather than
downstream. The layout follows the namespace's capture format
(``{Namespace}/{EventHub}/{PartitionId}/{Year}/{Month}/{Day}/{Hour}/{Minute}/
{Second}``); Azure writes Avro files per window, locally one object per event.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from cloudops_connectors.core import state
from cloudops_connectors.core.raw import RawStore, sha256
from cloudops_connectors.telemetry.consumer import Checkpointer, handle_batch
from cloudops_connectors.telemetry.pipeline import HUB, HubEvent, TelemetryPipeline

NAMESPACE: Final = "evhns-cloudops-local"


class Capture:
    def __init__(self, raw: RawStore, namespace: str = NAMESPACE, hub: str = HUB) -> None:
        self._raw = raw
        self._namespace = namespace
        self._hub = hub

    def ref(self, event: HubEvent) -> str:
        t = event.enqueued_time
        return (f"{self._namespace}/{self._hub}/{event.partition_id}/"
                f"{t:%Y/%m/%d/%H/%M/%S}-{event.sequence_number:012d}.json")

    async def write(self, event: HubEvent) -> str:
        ref = self.ref(event)
        await self._raw.put(ref, event.body, {
            "partition": event.partition_id, "sequence": str(event.sequence_number),
            "enqueued_time": event.enqueued_time.isoformat(), "sha256": sha256(event.body),
        })
        return ref


class MemoryCheckpointer:
    def __init__(self) -> None:
        self.positions: dict[str, int] = {}

    async def checkpoint(self, partition_id: str, event: HubEvent) -> None:
        self.positions[partition_id] = event.sequence_number


class InMemoryHub:
    def __init__(self, partitions: int = 8, *, capture: Capture | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.partitions = [str(p) for p in range(partitions)]
        self.events: dict[str, list[HubEvent]] = {p: [] for p in self.partitions}
        self._capture = capture
        self._clock = clock

    async def publish(self, body: bytes, partition_key: str) -> HubEvent:
        digest = int.from_bytes(hashlib.sha256(partition_key.encode()).digest()[:4], "big")
        partition = self.partitions[digest % len(self.partitions)]
        event = HubEvent(partition, len(self.events[partition]), self._clock(), body)
        self.events[partition].append(event)
        if self._capture is not None:
            await self._capture.write(event)
        return event

    async def consume(self, pipeline: TelemetryPipeline, checkpointer: Checkpointer, *,
                      start: dict[str, int] | None = None, batch_size: int = 100
                      ) -> state.RunCounts:
        """Every partition from after its checkpoint (``start``) to the end."""
        total = state.RunCounts()
        for partition, events in self.events.items():
            after = (start or {}).get(partition, -1)
            pending = [e for e in events if e.sequence_number > after]
            for i in range(0, len(pending), batch_size):
                total.add(await handle_batch(pipeline, checkpointer, partition,
                                             pending[i:i + batch_size]))
        return total
