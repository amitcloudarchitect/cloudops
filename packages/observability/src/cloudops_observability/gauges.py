"""Gauges whose values come from the database, refreshed by a background job.

Connector health (last success age, lag, failure rate, quarantine depth) is
state, not an event, so it is read from the platform's run log rather than
counted in a process. A job computes a ``Snapshot`` periodically; observable
gauges report the latest one at each metric export. A stale snapshot is
reported as such (``cloudops_platform_snapshot_age_seconds``), never silently.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from cloudops_observability.metrics import METER


@dataclass(frozen=True, slots=True)
class Gauge:
    name: str
    unit: str
    description: str


@dataclass
class Snapshot:
    taken_at: float = field(default_factory=time.time)
    values: dict[str, list[tuple[Mapping[str, str], float | int]]] = field(default_factory=dict)

    def add(self, name: str, value: float | int, labels: Mapping[str, str]) -> None:
        self.values.setdefault(name, []).append((dict(labels), value))


SNAPSHOT_AGE: Final = Gauge("cloudops.platform.snapshot_age", "s",
                            "Seconds since the platform gauges were last computed")


class GaugeSet:
    """Registers each gauge once; ``publish`` swaps in a new snapshot."""

    def __init__(self, gauges: Iterable[Gauge]) -> None:
        self._snapshot: Snapshot | None = None
        meter = metrics.get_meter(METER)
        self.names = frozenset(g.name for g in gauges)
        for g in gauges:
            meter.create_observable_gauge(g.name, callbacks=[self._callback(g.name)],
                                          unit=g.unit, description=g.description)
        meter.create_observable_gauge(SNAPSHOT_AGE.name, callbacks=[self._age],
                                      unit=SNAPSHOT_AGE.unit,
                                      description=SNAPSHOT_AGE.description)

    def publish(self, snapshot: Snapshot) -> None:
        unknown = set(snapshot.values) - self.names
        if unknown:
            raise ValueError(f"unregistered gauges: {sorted(unknown)}")
        self._snapshot = snapshot

    def _callback(self, name: str):  # type: ignore[no-untyped-def]
        def observe(_: CallbackOptions) -> Iterable[Observation]:
            snap = self._snapshot
            if snap is None:
                return []
            return [Observation(v, dict(labels)) for labels, v in snap.values.get(name, [])]
        return observe

    def _age(self, _: CallbackOptions) -> Iterable[Observation]:
        snap = self._snapshot
        return [] if snap is None else [Observation(time.time() - snap.taken_at)]


__all__ = ["SNAPSHOT_AGE", "Gauge", "GaugeSet", "Snapshot"]
