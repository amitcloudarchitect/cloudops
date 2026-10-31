"""One Event Hubs batch through to the column store.

    counts = await pipeline.handle(partition_id, events)

1. Parse every event (``normalise.parse``); malformed records are counted.
2. Resolve accounts and resources (``resolve.Resolver``); unknown ones are
   quarantined with counts, exactly as the batch connectors do.
3. Keep allowlisted (resource type, metric) pairs; count the rest.
4. Write the samples through ``cloudops_tenancy.telemetry`` under an ingest
   scope of exactly the batch's tenants, with a deduplication token naming the
   partition and sequence range: a batch delivered again after a crash is
   stored once.
5. In PostgreSQL, one transaction: quarantine counts, drift, and the
   partition's watermark (the newest enqueued time, for lag).

The caller checkpoints only after ``handle`` returns (``consumer.py``). The
raw events are not stored here: Event Hubs Capture writes the bronze copy in
parallel, independent of this consumer (LLD; ``infra/terraform`` 04-data).

Run figures accumulate per window and ``flush`` writes them as one
``staging.ingest_run`` row (source ``telemetry``, entity ``metrics``), so
connector health covers telemetry without a row per batch.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from cloudops_connectors.core import drift, identity, state
from cloudops_connectors.telemetry.normalise import METRICS, RawMetric, parse
from cloudops_connectors.telemetry.resolve import PRINCIPAL, Resolver
from cloudops_tenancy import Database, Role, TenantScope
from cloudops_tenancy.telemetry import MetricSample, TelemetryStore

SOURCE: Final = "telemetry"
ENTITY: Final = "metrics"
HUB: Final = "evh-metrics"
_MAPPED: Final = frozenset({
    "time", "resourceId", "metricName", "timeGrain", "count", "total", "minimum", "maximum",
    "average", "account_id", "region", "namespace", "metric_name", "dimensions", "timestamp",
    "value", "unit", "metric_stream_name",
})


@dataclass(frozen=True, slots=True)
class HubEvent:
    partition_id: str
    sequence_number: int
    enqueued_time: datetime
    body: bytes


def dedupe_token(hub: str, partition_id: str, events: Sequence[HubEvent]) -> str:
    return f"{hub}:{partition_id}:{events[0].sequence_number}-{events[-1].sequence_number}"


class TelemetryPipeline:
    def __init__(self, db: Database, store: TelemetryStore, resolver: Resolver, *,
                 hub: str = HUB, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
                 ) -> None:
        self._db = db
        self._store = store
        self._resolver = resolver
        self._hub = hub
        self._clock = clock
        self._window = state.RunCounts()
        self._window_start = clock()
        self._window_failed = False
        self._newest: datetime | None = None

    async def handle(self, partition_id: str, events: Sequence[HubEvent]) -> state.RunCounts:
        if not events:
            return state.RunCounts()
        counts = state.RunCounts(pages=1)
        raws: list[RawMetric] = []
        for event in events:
            parsed = parse(event.body)
            counts.records += parsed.records
            counts.rejected += parsed.malformed
            raws.extend(parsed.metrics)

        accounts = await self._resolver.accounts({(m.provider, m.account) for m in raws})
        resources = await self._resolver.resources(
            {(m.provider, m.account, m.resource_key) for m in raws
             if accounts.get((m.provider, m.account)) is not None}, accounts)
        allow = await self._resolver.allowlist()

        unknown_accounts: dict[str, Counter[str]] = {}
        unlinked: Counter[identity.Unlinked] = Counter()
        samples: list[MetricSample] = []
        for m in raws:
            acct = accounts.get((m.provider, m.account))
            if acct is None:
                unknown_accounts.setdefault(m.provider, Counter())[m.account] += 1
                continue
            res = resources.get((m.provider, m.account, m.resource_key))
            if res is None:
                unlinked[identity.Unlinked(acct.tenant_id, f"{m.provider}:{m.resource_key}",
                                           m.resource_key[-200:], acct.account_id)] += 1
                continue
            if (res.type_canonical, m.metric) not in allow:
                continue  # not on the allowlist: counted by difference, kept in capture
            samples.append(MetricSample(res.tenant_id, res.account_id, res.id, m.metric, m.ts,
                                        m.value, m.unit, f"{m.provider}_metrics"))
        counts.quarantined = sum(sum(c.values()) for c in unknown_accounts.values()) + sum(
            unlinked.values())
        counts.ci_unlinked = sum(unlinked.values())

        tenants = {s.tenant_id for s in samples}
        if samples:
            await self._store.write(TenantScope(tenants, Role.INGEST, PRINCIPAL), samples,
                                    dedupe_token=dedupe_token(self._hub, partition_id, events))
        counts.written = len(samples)

        newest = max(e.enqueued_time for e in events)
        ref = f"{self._hub}/{partition_id}/{events[0].sequence_number}"
        scope_tenants: set[UUID] = {u.tenant_id for u in unlinked}
        async with self._db.transaction(TenantScope(scope_tenants, Role.INGEST,
                                                    PRINCIPAL)) as conn:
            for provider, names in sorted(unknown_accounts.items()):
                await state.quarantine_aliases(conn, provider, names, ref)
            await identity.quarantine(conn, SOURCE, unlinked, ref)
            await drift.observe(conn, SOURCE, ENTITY, {f for m in raws for f in m.fields},
                                mapped=_MAPPED, expected=frozenset(), records=len(raws),
                                raw_ref=ref)
            await state.advance_watermark(conn, SOURCE, ENTITY,
                                          f"{self._hub}/{partition_id}", newest)
        self._window.add(counts)
        self._newest = newest if self._newest is None else max(self._newest, newest)
        return counts

    def failed(self) -> None:
        """A batch failed (it will be delivered again); the window reports it."""
        self._window_failed = True

    async def flush(self) -> state.RunCounts:
        """Write the window's figures as one run, and start a new window."""
        counts, started = self._window, self._window_start
        failed = self._window_failed
        self._window, self._window_start, self._window_failed = (
            state.RunCounts(), self._clock(), False)
        run_id = uuid4()
        async with self._db.transaction(TenantScope((), Role.INGEST, PRINCIPAL)) as conn:
            await state.start_run(conn, run_id, SOURCE, ENTITY, "incremental", started)
            await state.finish_run(conn, run_id, "failed" if failed else "succeeded", counts,
                                   self._clock(), self._newest)
        return counts


def known_metrics() -> frozenset[str]:
    return frozenset(m for m, _, _ in METRICS.values())
