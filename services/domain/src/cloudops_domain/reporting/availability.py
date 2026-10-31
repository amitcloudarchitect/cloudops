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

import httpx

from cloudops_domain.reporting.sections import AvailabilityGapError
from cloudops_tenancy import TenantScope
from cloudops_tenancy.telemetry import (
    MAX_LIMIT,
    Aggregate,
    Grain,
    MetricQuery,
    TelemetryError,
    TelemetryScopeError,
    TelemetryStore,
)

METRIC: Final = "availability"


class AvailabilityTruncatedError(AvailabilityGapError):
    """More hourly points than one read returns: the figure would be wrong."""


class AvailabilityUnreachableError(AvailabilityGapError):
    """The telemetry store could not be read."""


class TelemetryAvailability:
    def __init__(self, store: TelemetryStore) -> None:
        self._store = store

    async def availability(self, scope: TenantScope, start: datetime, end: datetime
                           ) -> Mapping[UUID, float]:
        try:
            points = await self._store.query(scope, MetricQuery(
                METRIC, start, end, grain=Grain.HOUR, aggregate=Aggregate.AVG,
                limit=MAX_LIMIT))
        except TelemetryScopeError:
            raise  # a scope defect is never a gap
        except (TelemetryError, httpx.TransportError) as exc:
            # The store is down or refused the read: say so in the section, never
            # fail the whole pack over it.
            raise AvailabilityUnreachableError("the telemetry store did not answer") from exc
        if len(points) >= MAX_LIMIT:
            raise AvailabilityTruncatedError(
                "more hourly measurements than one read returns; the figure would be partial")
        sums: dict[UUID, float] = defaultdict(float)
        counts: dict[UUID, int] = defaultdict(int)
        for p in points:
            sums[p.resource_id] += p.value
            counts[p.resource_id] += 1
        return {rid: sums[rid] / counts[rid] for rid in sums}
