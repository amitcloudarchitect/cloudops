"""Platform gauges from the database: connector health, data quality, FinOps, own cost.

    python -m cloudops_connectors.platform_metrics      # one per environment

Connector health is state, not an event, so it is read from the run log every
``INTERVAL`` seconds and reported as gauges (``cloudops_observability.gauges``)
that the collector exposes to managed Prometheus. Every figure is per connector,
per quarantine kind, or platform-wide: **never per tenant** (ADR 0022). Per-tenant
health is the console's (``/v1/connectors``), under RLS.

It runs on the ingest login, like the FinOps worker, with a scope covering every
tenant from ``cloudops.finops_tenants()``: row-level security still applies, so
there is no new way around it, only the reach the ingest role already has.

**The platform's own cost** (LLD, Observability) runs through the customer FinOps
code unchanged. The internal tenant (``cloudops.tenant.internal``, migration 0019)
is a tenant like any other: its cost lands through the Azure connector, the
FinOps jobs reconcile it and look for anomalies with ``cloudops_commercial``, and
this job reports what they found, plus allocation computed with the same
``cloudops_commercial.finops`` functions the console's FinOps view uses. A defect
in the FinOps engine shows up on our own bill first.

Money is reported in millionths of its currency (integers, never floats).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_commercial import finops
from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_config.probes import serve_probes
from cloudops_observability import TelemetryConfigError, micros
from cloudops_observability.gauges import Gauge, GaugeSet, Snapshot
from cloudops_observability.process import start
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

log = logging.getLogger("cloudops.platform_metrics")

PRINCIPAL: Final = "job:platform-metrics"
INTERVAL: Final = 60.0
SERVICE: Final = "platform-metrics"


def _g(name: str, unit: str, description: str) -> Gauge:
    return Gauge(f"cloudops.{name}", unit, description)


GAUGES: Final = (
    _g("connector.last_success_age", "s", "Seconds since the last successful incremental run"),
    _g("connector.expected_interval", "s", "The connector's schedule interval"),
    _g("connector.lag", "s", "Source lag at the last finished run (now minus watermark)"),
    _g("connector.runs_24h", "{run}", "Finished incremental runs, last 24 hours"),
    _g("connector.failed_runs_24h", "{run}", "Failed or dead-lettered runs, last 24 hours"),
    _g("connector.records_24h", "{record}", "Records read, last 24 hours"),
    _g("connector.rejected_24h", "{record}", "Records rejected, last 24 hours"),
    _g("connector.dead_letters_open", "{page}", "Dead-lettered pages not yet replayed"),
    _g("connector.schema_drift_open", "{field}", "Schema drift waiting for a person"),
    _g("quarantine.depth", "{row}", "Rows held in quarantine, by kind and source"),
    _g("quarantine.occurrences", "{occurrence}", "Occurrences behind the quarantined rows"),
    _g("finops.reconciliation_unverified", "{check}",
       "Latest checks per account and kind, current and previous period, not verified"),
    _g("finops.reconciliation_drift_max", "1",
       "Largest absolute drift among the latest checks (LLD target: under 0.005)"),
    _g("platform.cost_micros", "{micro}",
       "The platform's own cost this month, amortized, in millionths of its currency"),
    _g("platform.cost_verified", "1", "1 when the platform's own cost reconciles this month"),
    _g("platform.cost_anomalies", "{anomaly}", "Anomalies on the platform's own cost, 30 days"),
    _g("platform.cost_unallocated_share", "1",
       "Share of the platform's own cost with no owner tag, by dimension"),
)

_TENANTS: Final = text("SELECT cloudops.finops_tenants()")
_CONNECTORS: Final = text("""
SELECT s.source_system, s.entity, extract(epoch FROM s.every)::bigint,
       extract(epoch FROM now() - (
           SELECT max(r.finished_at) FROM staging.ingest_run r
           WHERE r.source_system = s.source_system AND r.entity = s.entity
             AND r.status = 'succeeded' AND r.mode = 'incremental'))::bigint,
       (SELECT r.lag_seconds FROM staging.ingest_run r
         WHERE r.source_system = s.source_system AND r.entity = s.entity
           AND r.mode = 'incremental' AND r.finished_at IS NOT NULL
         ORDER BY r.finished_at DESC LIMIT 1),
       count(r.id), count(r.id) FILTER (WHERE r.status IN ('failed', 'dead_lettered')),
       coalesce(sum(r.records), 0), coalesce(sum(r.rejected), 0),
       (SELECT count(*) FROM staging.ingest_dead_letter d
         WHERE d.source_system = s.source_system AND d.entity = s.entity
           AND d.replayed_at IS NULL),
       (SELECT count(*) FROM staging.schema_field f
         WHERE f.source_system = s.source_system AND f.entity = s.entity
           AND f.drift IS NOT NULL AND f.acknowledged_at IS NULL)
FROM staging.connector_schedule s
LEFT JOIN staging.ingest_run r
  ON r.source_system = s.source_system AND r.entity = s.entity AND r.mode = 'incremental'
 AND r.started_at > now() - interval '24 hours' AND r.status <> 'running'
GROUP BY s.source_system, s.entity, s.every
""")
# Quarantine has no useful per-tenant reading here: resource quarantine is
# counted under the all-tenant scope, so RLS still applies to it.
_QUARANTINE: Final = text("""
SELECT 'alias', source_system, count(*), coalesce(sum(occurrence_count), 0)
  FROM staging.tenant_alias_quarantine GROUP BY source_system
UNION ALL
SELECT 'resource', source_system, count(*), coalesce(sum(occurrence_count), 0)
  FROM staging.resource_quarantine GROUP BY source_system
""")
_LATEST_CHECKS: Final = text("""
SELECT DISTINCT ON (account_id, invoice_period, kind)
       tenant_id, kind, status, reason, drift
FROM cloudops.cost_reconciliation
WHERE invoice_period = ANY(CAST(:periods AS text[]))
ORDER BY account_id, invoice_period, kind, checked_on DESC
""")
_INTERNAL: Final = text("SELECT id FROM cloudops.tenant WHERE internal")
_OWN_COST: Final = text("""
SELECT c.charge_class::text, c.currency, sum(c.amortized_cost)
FROM cloudops.cost_fact c
WHERE c.tenant_id = :tenant AND c.invoice_period = :period
GROUP BY c.charge_class, c.currency
""")
# As the console's FinOps view reads it (cloudops_api.commercial._ALLOCATION).
_OWN_ALLOCATION: Final = text("""
SELECT c.resource_id IS NOT NULL, c.tags_snapshot, r.tags, c.currency, sum(c.amortized_cost)
FROM cloudops.cost_fact c LEFT JOIN cloudops.resource r ON r.id = c.resource_id
WHERE c.tenant_id = :tenant AND c.invoice_period = :period
GROUP BY c.resource_id IS NOT NULL, c.tags_snapshot, r.tags, c.currency
""")
_OWN_ANOMALIES: Final = text(
    "SELECT count(*) FROM cloudops.cost_anomaly"
    " WHERE tenant_id = :tenant AND observed_on > CAST(:today AS date) - 30")


def _periods(today: date) -> list[str]:
    first = today.replace(day=1)
    prev = (first.replace(year=first.year - 1, month=12) if first.month == 1
            else first.replace(month=first.month - 1))
    return [first.strftime("%Y-%m"), prev.strftime("%Y-%m")]


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


async def _tenants(db: Database) -> frozenset[UUID]:
    async with db.transaction(TenantScope((), Role.INGEST, PRINCIPAL)) as conn:
        ids = await conn.scalar(_TENANTS)
    return frozenset(UUID(str(t)) for t in ids or ())


async def _connectors(conn: ScopedConnection, snap: Snapshot) -> None:
    for (source, entity, every, success_age, lag, runs, failed, records, rejected, dead,
         drift) in _rows(await conn.execute(_CONNECTORS)):
        labels = {"source": str(source), "entity": str(entity)}
        snap.add("cloudops.connector.expected_interval", int(every), labels)
        if success_age is not None:  # never succeeded: absent, and the alert says so
            snap.add("cloudops.connector.last_success_age", int(success_age), labels)
        if lag is not None:
            snap.add("cloudops.connector.lag", int(lag), labels)
        snap.add("cloudops.connector.runs_24h", int(runs), labels)
        snap.add("cloudops.connector.failed_runs_24h", int(failed), labels)
        snap.add("cloudops.connector.records_24h", int(records), labels)
        snap.add("cloudops.connector.rejected_24h", int(rejected), labels)
        snap.add("cloudops.connector.dead_letters_open", int(dead), labels)
        snap.add("cloudops.connector.schema_drift_open", int(drift), labels)
    for kind, source, depth, occurrences in _rows(await conn.execute(_QUARANTINE)):
        labels = {"kind": str(kind), "source": str(source)}
        snap.add("cloudops.quarantine.depth", int(depth), labels)
        snap.add("cloudops.quarantine.occurrences", int(occurrences), labels)


async def _finops(conn: ScopedConnection, snap: Snapshot, today: date,
                  internal: UUID | None) -> None:
    periods = _periods(today)
    unverified: dict[tuple[str, str], int] = defaultdict(int)
    worst = Decimal(0)
    own_checks: list[str] = []
    for tenant, kind, status, reason, drift in _rows(
            await conn.execute(_LATEST_CHECKS, {"periods": periods})):
        if status != "verified":
            unverified[(str(kind), str(reason))] += 1
        if drift is not None:
            worst = max(worst, abs(Decimal(drift)))
        if internal is not None and tenant == internal:
            own_checks.append(str(status))
    for (kind, reason), n in sorted(unverified.items()):
        snap.add("cloudops.finops.reconciliation_unverified", n, {"kind": kind, "reason": reason})
    snap.add("cloudops.finops.reconciliation_drift_max", float(worst), {})
    if internal is None:
        return
    params = {"tenant": internal, "period": periods[0]}
    for charge_class, currency, amount in _rows(await conn.execute(_OWN_COST, params)):
        snap.add("cloudops.platform.cost_micros", micros(Decimal(amount)),
                 {"charge_class": str(charge_class), "currency": str(currency)})
    snap.add("cloudops.platform.cost_verified",
             int(bool(own_checks) and all(s == "verified" for s in own_checks)), {})
    snap.add("cloudops.platform.cost_anomalies",
             int(await conn.scalar(_OWN_ANOMALIES, {"tenant": internal, "today": today}) or 0),
             {})
    # Allocation exactly as the customer FinOps view computes it: per currency,
    # the cost row's tag snapshot first, then the resource's tags.
    by_ccy: dict[str, list[tuple[finops.Allocation | None, Decimal]]] = defaultdict(list)
    for linked, snapshot, tags, ccy, amount in _rows(
            await conn.execute(_OWN_ALLOCATION, params)):
        by_ccy[str(ccy)].append(
            (finops.allocate(snapshot, tags) if linked else None, Decimal(amount)))
    for ccy, rows in sorted(by_ccy.items()):
        for dimension in ("application", "cost_centre"):
            share = finops.unallocated(rows, dimension).unallocated_share
            if share is not None:
                snap.add("cloudops.platform.cost_unallocated_share", float(share),
                         {"dimension": dimension, "currency": ccy})


async def collect(db: Database, today: date | None = None) -> Snapshot:
    """One snapshot. Each part in its own transaction; a part that fails is
    logged and left out, so the others still report."""
    snap = Snapshot()
    tenants = await _tenants(db)
    every = TenantScope(tenants, Role.INGEST, PRINCIPAL)
    async with db.transaction(every) as conn:
        await _connectors(conn, snap)
        internal_ids = [UUID(str(r[0])) for r in await conn.execute(_INTERNAL)]
    async with db.transaction(TenantScope(tenants, Role.FINOPS, PRINCIPAL)) as conn:
        await _finops(conn, snap, today or datetime.now(UTC).date(),
                      internal_ids[0] if internal_ids else None)
    return snap


async def run(db: Database, gauges: GaugeSet, *, interval: float = INTERVAL) -> None:
    while True:
        try:
            gauges.publish(await collect(db))
        except Exception:
            # The snapshot ages instead; cloudops_platform_snapshot_age_seconds alerts.
            log.exception("platform metrics snapshot failed")
        await asyncio.sleep(interval)


async def _main() -> int:
    settings = load_settings(os.environ, require={Component.DATABASE_INGEST})
    db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=1)
    local = settings.environment is Environment.LOCAL
    probes = await serve_probes(
        {"database": db.ping},
        host="127.0.0.1" if local else "0.0.0.0",  # noqa: S104 - the kubelet probes the pod IP
    )
    try:
        await run(db, GaugeSet(GAUGES))
    finally:
        probes.close()
        await probes.wait_closed()
        await db.dispose()
    return 0


def main() -> int:
    try:
        telemetry = start(SERVICE, os.environ)
        try:
            return asyncio.run(_main())
        finally:
            telemetry.shutdown()
    except (ConfigurationError, TelemetryConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
