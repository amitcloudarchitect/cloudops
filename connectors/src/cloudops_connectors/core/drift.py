"""Schema drift: what a source started or stopped sending.

Every page reports the top-level field names its records carry. Fields are
registered per source and entity in ``staging.schema_field``:

- the first page an entity ever sees is the baseline: nothing is drift;
- after that, a field never seen before and not mapped is drift ``added``: it
  is landing in ``raw_extra`` and someone should decide whether to map it;
- a field the mapping expects on every record that is absent from every record
  of a page is drift ``missing``: an upstream rename, or a permission change.

Recorded in the page's own transaction, so a replay records nothing twice that
the first pass did not. Field names only, never values. A person acknowledges
drift in the console; a later ``missing`` raises it again.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from sqlalchemy import text

from cloudops_tenancy import ScopedConnection

MAX_FIELD: Final = 200
MAX_NEW_PER_PAGE: Final = 100  # a page of junk keys cannot flood the registry

_KNOWN: Final = text(
    "SELECT field FROM staging.schema_field WHERE source_system = :source AND entity = :entity"
)
_SEEN: Final = text(
    "INSERT INTO staging.schema_field (source_system, entity, field, drift, drift_at,"
    " drift_count, raw_ref)"
    " VALUES (:source, :entity, :field, :drift,"
    "         CASE WHEN CAST(:drift AS text) IS NULL THEN NULL ELSE now() END,"
    "         CASE WHEN CAST(:drift AS text) IS NULL THEN 0 ELSE 1 END, :raw_ref)"
    " ON CONFLICT (source_system, entity, field) DO UPDATE SET last_seen = now()"
)
_MISSING: Final = text(
    "INSERT INTO staging.schema_field (source_system, entity, field, drift, drift_at,"
    " drift_count, raw_ref)"
    " VALUES (:source, :entity, :field, 'missing', now(), 1, :raw_ref)"
    " ON CONFLICT (source_system, entity, field) DO UPDATE SET drift = 'missing',"
    "   drift_at = now(), drift_count = staging.schema_field.drift_count + 1,"
    "   raw_ref = EXCLUDED.raw_ref, acknowledged_at = NULL, acknowledged_by = NULL"
)


def fields_of(records: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(k)[:MAX_FIELD] for r in records for k in r}


async def observe(
    conn: ScopedConnection,
    source: str,
    entity: str,
    seen: set[str],
    *,
    mapped: frozenset[str],
    expected: frozenset[str],
    records: int,
    raw_ref: str,
) -> int:
    """Register the page's fields; return how many drift events it raised."""
    known = {str(r[0]) for r in await conn.execute(_KNOWN, {"source": source, "entity": entity})}
    baseline = not known
    new = sorted(seen - known)[:MAX_NEW_PER_PAGE]
    rows = []
    events = 0
    for field in [*new, *sorted(seen & known)]:
        drift = None if baseline or field in known or field in mapped else "added"
        events += drift is not None
        rows.append({"source": source, "entity": entity, "field": field, "drift": drift,
                     "raw_ref": raw_ref})
    if rows:
        await conn.execute(_SEEN, rows)
    missing = sorted(expected - seen) if records > 0 and not baseline else []
    if missing:
        await conn.execute(_MISSING, [
            {"source": source, "entity": entity, "field": f, "raw_ref": raw_ref}
            for f in missing
        ])
    return events + len(missing)
