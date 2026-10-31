"""Connector runs and schema drift for the console's connector health page. Local only.

    CLOUDOPS_ENV=local python -m cloudops_devdata.ingest_health
    CLOUDOPS_ENV=local python -m cloudops_devdata.ingest_health --drift aws config newField

The seed calls ``record`` once, so the page shows every band locally and in the
console's journeys: one connector on time, one late, one in outage with a dead
letter, one with failures and rejected records, open drift, and the rest never
run. Times are relative to now, so the picture ages as a real one would: an
on-time connector turns late, then an outage, if nothing runs.

``--drift`` raises (or re-raises) drift on one field, as a connector would, so a
journey acknowledges drift of its own and never uses up the seeded rows.

Written as the ingest login through packages/tenancy. Every row is the
platform's run log; none carries a tenant or a payload value.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Final

from sqlalchemy import text

from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

_RUN = text(
    "INSERT INTO staging.ingest_run (id, source_system, entity, mode, started_at, finished_at,"
    " status, pages, records, written, quarantined, rejected, lag_seconds)"
    " VALUES (:id, :source, :entity, 'incremental', now() - :ago,"
    " now() - :ago + interval '20 seconds', :status, 1, :records,"
    " :records - :quarantined - :rejected, :quarantined, :rejected, :lag)"
)
_DEAD = text(
    "INSERT INTO staging.ingest_dead_letter (run_id, source_system, entity, raw_ref, error)"
    " VALUES (:run, :source, :entity, :ref, 'parse_error')"
)
_DRIFT = text(
    "INSERT INTO staging.schema_field (source_system, entity, field, drift, drift_at,"
    " drift_count) VALUES (:source, :entity, :field, :drift, now(), 1)"
    " ON CONFLICT (source_system, entity, field) DO UPDATE"
    " SET drift = EXCLUDED.drift, drift_at = now(), last_seen = now(),"
    "     drift_count = staging.schema_field.drift_count + 1,"
    "     acknowledged_at = NULL, acknowledged_by = NULL"
)

# (source, entity, minutes ago, status, records, quarantined, rejected)
RUNS: Final[tuple[tuple[str, str, int, str, int, int, int], ...]] = (
    # On time: every five minutes for the last hour.
    *(("servicenow", "incident", m, "succeeded", 40, 2, 0) for m in range(1, 60, 5)),
    # Late: last success twelve minutes ago on a five-minute schedule.
    ("servicenow", "change_task", 12, "succeeded", 15, 0, 0),
    ("servicenow", "change_task", 17, "succeeded", 12, 0, 0),
    # Outage: daily, last success four days ago, and the retry today failed.
    ("qualys", "host_detection", 4 * 24 * 60, "succeeded", 900, 14, 0),
    ("qualys", "host_detection", 120, "dead_lettered", 0, 0, 0),
    # Attention: on time, but a run failed and records were rejected.
    ("azure", "resource", 6, "succeeded", 400, 5, 3),
    ("azure", "resource", 21, "failed", 0, 0, 0),
    ("azure", "resource", 36, "succeeded", 398, 5, 0),
    # On time, with drift waiting.
    ("aws", "config", 4, "succeeded", 120, 0, 0),
)

DRIFT: Final = (
    ("aws", "config", "supplementaryConfiguration", "added"),
    ("servicenow", "incident", "u_major_incident", "added"),
    ("qualys", "host_detection", "QDS_FACTORS", "missing"),
)


def _scope() -> TenantScope:
    return TenantScope((), Role.INGEST, "devdata:ingest-health")


async def _drift(conn: ScopedConnection, source: str, entity: str, field: str,
                 drift: str) -> None:
    await conn.execute(_DRIFT, {"source": source, "entity": entity, "field": field,
                                "drift": drift})


async def record(db: Database) -> int:
    """The demo run log and drift; returns the number of runs written."""
    async with db.transaction(_scope()) as conn:
        for source, entity, minutes, status, records, quarantined, rejected in RUNS:
            run = uuid.uuid4()
            await conn.execute(_RUN, {
                "id": run, "source": source, "entity": entity,
                "ago": timedelta(minutes=minutes), "status": status, "records": records,
                "quarantined": quarantined, "rejected": rejected, "lag": 60})
            if status == "dead_lettered":
                await conn.execute(_DEAD, {"run": run, "source": source, "entity": entity,
                                           "ref": f"{source}/{entity}/page-0001"})
        for source, entity, field, drift in DRIFT:
            await _drift(conn, source, entity, field, drift)
    return len(RUNS)


async def raise_drift(source: str, entity: str, field: str, drift: str) -> None:
    settings = load_settings(os.environ, require={Component.DATABASE_INGEST})
    if settings.environment is not Environment.LOCAL:
        raise ConfigurationError("raising drift by hand is local-only")
    db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=1)
    try:
        async with db.transaction(_scope()) as conn:
            await _drift(conn, source, entity, field, drift)
    finally:
        await db.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_devdata.ingest_health")
    parser.add_argument("--drift", nargs=3, metavar=("SOURCE", "ENTITY", "FIELD"),
                        required=True)
    parser.add_argument("--kind", choices=("added", "missing"), default="added")
    args = parser.parse_args(argv)
    source, entity, field = args.drift
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined,unused-ignore]
    try:
        asyncio.run(raise_drift(source, entity, field, args.kind))
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
