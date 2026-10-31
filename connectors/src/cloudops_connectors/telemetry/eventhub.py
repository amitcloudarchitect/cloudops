"""The Event Hubs binding: the SDK consumer, its blob checkpoint store, and
``handle_batch``.

One consumer group per service, never shared; managed identity, no SAS keys
(LLD, Event Hubs). The checkpoint is the SDK's own (a blob per partition),
updated through ``partition_context.update_checkpoint`` only once
``handle_batch`` has stored the batch. A batch that fails every attempt stops
the consumer: the process exits non-zero, Kubernetes restarts it, and it
resumes from the last checkpoint. Run figures are flushed to the run log
every ``flush_every`` seconds.

Not wired to a command yet: the Azure telemetry store is LLD open item L1, so
there is nowhere in Azure to write to. The local profile uses ``hub.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from azure.eventhub import EventData
from azure.eventhub.aio import EventHubConsumerClient, PartitionContext
from azure.eventhub.extensions.checkpointstoreblobaio import BlobCheckpointStore

from cloudops_connectors.telemetry.consumer import ConsumerHaltedError, handle_batch
from cloudops_connectors.telemetry.pipeline import HubEvent, TelemetryPipeline


class _SdkCheckpoint:
    def __init__(self, context: PartitionContext, last: EventData) -> None:
        self._context = context
        self._last = last

    async def checkpoint(self, partition_id: str, event: HubEvent) -> None:
        await self._context.update_checkpoint(self._last)


def to_hub_events(partition_id: str, events: Sequence[EventData]) -> list[HubEvent]:
    """Received events always carry a sequence number and an enqueued time."""
    out = []
    for e in events:
        if e.sequence_number is None or e.enqueued_time is None:
            raise ValueError("a received event without its position")
        try:
            body = e.body_as_str(encoding="utf-8").encode("utf-8")
        except UnicodeDecodeError:
            body = b"\xff"  # not text: parsed as one malformed record, counted
        out.append(HubEvent(partition_id, e.sequence_number, e.enqueued_time, body))
    return out


async def consume(
    pipeline: TelemetryPipeline, *, namespace: str, hub: str, consumer_group: str,
    storage_account_url: str, checkpoint_container: str, credential: Any,
    batch_size: int = 200, flush_every: float = 60.0,
) -> None:
    store = BlobCheckpointStore(storage_account_url, checkpoint_container,
                                credential=credential)
    client = EventHubConsumerClient(
        fully_qualified_namespace=namespace, eventhub_name=hub, consumer_group=consumer_group,
        credential=credential, checkpoint_store=store,
    )
    halted: list[BaseException] = []
    last_flush = time.monotonic()

    async def on_batch(context: PartitionContext, events: list[EventData]) -> None:
        nonlocal last_flush
        if not events or halted:
            return
        try:
            await handle_batch(pipeline, _SdkCheckpoint(context, events[-1]),
                               context.partition_id, to_hub_events(context.partition_id, events))
        except ConsumerHaltedError as exc:
            halted.append(exc)
            asyncio.get_running_loop().create_task(client.close())
            return
        if time.monotonic() - last_flush >= flush_every:
            last_flush = time.monotonic()
            await pipeline.flush()

    async with client:
        await client.receive_batch(on_event_batch=on_batch, max_batch_size=batch_size,
                                   max_wait_time=5, starting_position="-1")
    await pipeline.flush()
    if halted:
        raise halted[0]
