"""Checkpointing: progress is recorded only after the batch is stored.

    await handle_batch(pipeline, checkpointer, partition_id, events)

At-least-once: the checkpoint moves after ``TelemetryPipeline.handle`` has
written the samples and committed the run state, never before. A crash in
between means the batch is delivered again from the last checkpoint, and the
store's deduplication token (partition and sequence range) keeps it once.

A batch that keeps failing is retried with exponential backoff, then the
consumer halts (``ConsumerHaltedError``) rather than checkpoint past data it
never stored; the pod restarts and resumes from the checkpoint. Skipping a
batch would be a silent gap: the bronze capture still has it, but nothing would
say so.

``eventhub.py`` binds this to the Event Hubs SDK and its blob checkpoint store;
``hub.py`` is the in-memory hub tests and the local profile use.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, Protocol

from cloudops_connectors.core import state
from cloudops_connectors.telemetry.pipeline import HubEvent, TelemetryPipeline

ATTEMPTS: Final = 5  # LLD: five attempts


class ConsumerHaltedError(Exception):
    """A batch failed every attempt. Nothing was checkpointed past it."""


class Checkpointer(Protocol):
    async def checkpoint(self, partition_id: str, event: HubEvent) -> None: ...


async def handle_batch(
    pipeline: TelemetryPipeline,
    checkpointer: Checkpointer,
    partition_id: str,
    events: Sequence[HubEvent],
    *,
    attempts: int = ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    base_delay: float = 2.0,
) -> state.RunCounts:
    if not events:
        return state.RunCounts()
    for attempt in range(attempts):
        try:
            counts = await pipeline.handle(partition_id, events)
        except Exception as exc:
            pipeline.failed()
            if attempt == attempts - 1:
                raise ConsumerHaltedError(
                    f"partition {partition_id}: batch from sequence "
                    f"{events[0].sequence_number} failed {attempts} times "
                    f"({type(exc).__name__})") from exc
            await sleep(base_delay * 2 ** attempt)
            continue
        await checkpointer.checkpoint(partition_id, events[-1])
        return counts
    raise ConsumerHaltedError(partition_id)  # pragma: no cover - the loop returns or raises
