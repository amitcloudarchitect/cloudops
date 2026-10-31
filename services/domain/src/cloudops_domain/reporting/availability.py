"""Availability from the telemetry store, for the pack's availability section.

Reads the hourly ``availability`` metric (percent) through the tenancy
package's telemetry wrapper, which is the store's only door and puts the
tenant predicate in every statement. Each resource's figure is the mean of its
hourly means over the month.

A month that would exceed the wrapper's row limit is refused rather than
summarised from a truncated read.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Final
from uuid import UUID

from cloudops_tenancy import TenantScope
from cloudops_tenancy.telemetry import MAX_LIMIT, Aggregate, Grain, MetricQuery, TelemetryStore

METRIC: Final = "availability"


class AvailabilityTruncatedError(Exception):
    """More hourly points than one read returns: the figure would be wrong."""


class TelemetryAvailability:
    def __init__(self, store: TelemetryStore) -> None:
        self._store = store

    async def availability(self, scope: TenantScope, start: datetime, end: datetime
                           ) -> Mapping[UUID, float]:
        points = await self._store.query(scope, MetricQuery(
            METRIC, start, end, grain=Grain.HOUR, aggregate=Aggregate.AVG, limit=MAX_LIMIT))
        if len(points) >= MAX_LIMIT:
            raise AvailabilityTruncatedError("too many hourly points for one month")
        sums: dict[UUID, float] = defaultdict(float)
        counts: dict[UUID, int] = defaultdict(int)
        for p in points:
            sums[p.resource_id] += p.value
            counts[p.resource_id] += 1
        return {rid: sums[rid] / counts[rid] for rid in sums}
