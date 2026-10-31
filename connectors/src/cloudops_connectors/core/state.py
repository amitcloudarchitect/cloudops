"""Watermarks, identity resolution, quarantine and the run log.

Every function takes a ``ScopedConnection`` from packages/tenancy: connectors
reach the database as the ingest login, under an explicit scope, like
everything else.

- Watermarks advance with GREATEST, so a replay or a late page can never move
  one backwards, and only inside the transaction that commits the batch.
- Identity resolution calls ``cloudops.resolve_tenant_alias`` (migration
  0005): exact, then normalised, then nothing. The connector never searches
  aliases itself; it could not see other tenants' aliases if it tried.
- Unmatched names go to quarantine with an occurrence count. Counts are
  sightings: a ticket seen again in the five-minute overlap, or replayed,
  counts again. Depth is an alerting signal, not an audit figure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_tenancy import ScopedConnection

_READ_WATERMARK: Final = text(
    "SELECT watermark FROM staging.ingest_watermark"
    " WHERE source_system = :source AND entity = :entity AND scope_key = :scope"
)
_ADVANCE_WATERMARK: Final = text(
    "INSERT INTO staging.ingest_watermark (source_system, entity, scope_key, watermark)"
    " VALUES (:source, :entity, :scope, :watermark)"
    " ON CONFLICT (source_system, entity, scope_key) DO UPDATE"
    " SET watermark = GREATEST(staging.ingest_watermark.watermark, EXCLUDED.watermark),"
    "     updated_at = now()"
)
_RESOLVE: Final = text(
    "SELECT tenant_id, outcome FROM cloudops.resolve_tenant_alias(:source, :alias)"
)
_QUARANTINE_ALIAS: Final = text(
    "INSERT INTO staging.tenant_alias_quarantine"
    " (source_system, alias, occurrence_count, raw_ref)"
    " VALUES (:source, :alias, :count, :raw_ref)"
    " ON CONFLICT (source_system, alias) DO UPDATE"
    " SET occurrence_count = staging.tenant_alias_quarantine.occurrence_count"
    "                        + EXCLUDED.occurrence_count,"
    "     last_seen = now(), raw_ref = EXCLUDED.raw_ref"
)
_QUARANTINE_RESOURCE: Final = text(
    "INSERT INTO staging.resource_quarantine"
    " (tenant_id, source_system, external_key, name, occurrence_count, raw_ref)"
    " VALUES (:tenant_id, :source, :key, :name, :count, :raw_ref)"
    " ON CONFLICT (tenant_id, source_system, external_key) DO UPDATE"
    " SET occurrence_count = staging.resource_quarantine.occurrence_count"
    "                        + EXCLUDED.occurrence_count,"
    "     last_seen = now(), name = EXCLUDED.name, raw_ref = EXCLUDED.raw_ref"
)


async def read_watermark(
    conn: ScopedConnection, source: str, entity: str, scope_key: str
) -> datetime | None:
    value = await conn.scalar(
        _READ_WATERMARK, {"source": source, "entity": entity, "scope": scope_key}
    )
    return value if isinstance(value, datetime) else None


async def advance_watermark(
    conn: ScopedConnection, source: str, entity: str, scope_key: str, watermark: datetime
) -> None:
    if watermark.tzinfo is None:
        raise ValueError("watermark must be timezone-aware")
    await conn.execute(
        _ADVANCE_WATERMARK,
        {"source": source, "entity": entity, "scope": scope_key, "watermark": watermark},
    )


@dataclass(frozen=True, slots=True)
class Resolution:
    tenant_id: UUID | None
    outcome: str  # exact | normalised | ambiguous | unmatched


async def resolve_aliases(
    conn: ScopedConnection, source: str, names: Iterable[str]
) -> dict[str, Resolution]:
    out: dict[str, Resolution] = {}
    for name in sorted(set(names)):
        row: tuple[Any, ...] = tuple(
            (await conn.execute(_RESOLVE, {"source": source, "alias": name})).one()
        )
        out[name] = Resolution(row[0], str(row[1]))
    return out


async def quarantine_aliases(
    conn: ScopedConnection, source: str, counts: Mapping[str, int], raw_ref: str
) -> None:
    if counts:
        await conn.execute(
            _QUARANTINE_ALIAS,
            [
                {"source": source, "alias": a, "count": n, "raw_ref": raw_ref}
                for a, n in sorted(counts.items())
            ],
        )


async def quarantine_resources(
    conn: ScopedConnection,
    source: str,
    counts: Mapping[tuple[UUID, str, str], int],
    raw_ref: str,
) -> None:
    """``counts`` is keyed by (tenant_id, external_key, name)."""
    if counts:
        await conn.execute(
            _QUARANTINE_RESOURCE,
            [
                {"tenant_id": t, "source": source, "key": k, "name": name, "count": n,
                 "raw_ref": raw_ref}
                for (t, k, name), n in sorted(counts.items(), key=lambda i: str(i[0]))
            ],
        )


# --- Run log: the health report (LLD: records, lag, quarantine depth, errors) --


@dataclass
class RunCounts:
    pages: int = 0
    records: int = 0
    written: int = 0
    quarantined: int = 0
    rejected: int = 0
    ci_unlinked: int = 0

    def add(self, other: RunCounts) -> None:
        for name in ("pages", "records", "written", "quarantined", "rejected", "ci_unlinked"):
            setattr(self, name, getattr(self, name) + getattr(other, name))


async def start_run(
    conn: ScopedConnection, run_id: UUID, source: str, entity: str, mode: str,
    started_at: datetime,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO staging.ingest_run (id, source_system, entity, mode, started_at, status)"
            " VALUES (:id, :source, :entity, :mode, :started, 'running')"
            " ON CONFLICT (id) DO NOTHING"
        ),
        {"id": run_id, "source": source, "entity": entity, "mode": mode, "started": started_at},
    )


async def finish_run(
    conn: ScopedConnection, run_id: UUID, status: str, counts: RunCounts,
    finished_at: datetime, watermark: datetime | None,
) -> None:
    lag = None if watermark is None else int((finished_at - watermark).total_seconds())
    await conn.execute(
        text(
            "UPDATE staging.ingest_run SET status = :status, finished_at = :finished,"
            " pages = :pages, records = :records, written = :written,"
            " quarantined = :quarantined, rejected = :rejected, ci_unlinked = :ci_unlinked,"
            " watermark = :watermark, lag_seconds = :lag WHERE id = :id"
        ),
        {
            "id": run_id, "status": status, "finished": finished_at, "watermark": watermark,
            "lag": lag, "pages": counts.pages, "records": counts.records,
            "written": counts.written, "quarantined": counts.quarantined,
            "rejected": counts.rejected, "ci_unlinked": counts.ci_unlinked,
        },
    )


async def dead_letter(
    conn: ScopedConnection, run_id: UUID, source: str, entity: str, raw_ref: str, error: str
) -> None:
    await conn.execute(
        text(
            "INSERT INTO staging.ingest_dead_letter (run_id, source_system, entity, raw_ref, error)"
            " VALUES (:run, :source, :entity, :ref, :error)"
        ),
        # The error is ours (an exception type and message), never payload text.
        {"run": run_id, "source": source, "entity": entity, "ref": raw_ref, "error": error[:2000]},
    )


# --- Continuation state (Graph delta links), beside the time watermark --------

_READ_CURSOR: Final = text(
    "SELECT cursor FROM staging.ingest_cursor"
    " WHERE source_system = :source AND entity = :entity AND scope_key = :scope"
)
_ADVANCE_CURSOR: Final = text(
    "INSERT INTO staging.ingest_cursor (source_system, entity, scope_key, cursor)"
    " VALUES (:source, :entity, :scope, :cursor)"
    " ON CONFLICT (source_system, entity, scope_key) DO UPDATE"
    " SET cursor = EXCLUDED.cursor, updated_at = now()"
)


async def read_cursor(
    conn: ScopedConnection, source: str, entity: str, scope_key: str
) -> str | None:
    value = await conn.scalar(
        _READ_CURSOR, {"source": source, "entity": entity, "scope": scope_key}
    )
    return value if isinstance(value, str) else None


async def advance_cursor(
    conn: ScopedConnection, source: str, entity: str, scope_key: str, cursor: str
) -> None:
    """Only inside the transaction that commits the page the cursor ends."""
    await conn.execute(
        _ADVANCE_CURSOR,
        {"source": source, "entity": entity, "scope": scope_key, "cursor": cursor},
    )
