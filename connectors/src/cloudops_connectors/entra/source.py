"""Entra ID through the connector contract (``core/ingest.py``).

scopes    the configured customer directories (one client, and token, each)
fetch     ``user``: the users delta, or the persisted deltaLink, then nextLinks;
          ``role_assignment``: the whole collection, page by page
process   resolve the directory to its tenant, upsert, and on the final page:
          ``user`` persists the deltaLink, ``role_assignment`` closes what the
          run did not see; both advance the watermark to the run's start

**Which tenant.** A directory id is an alias of source ``entra``, approved
through the customer mapping, and only an *exact* match counts
(``identity.resolve_ids``). A directory that resolves to nobody is
quarantined with an occurrence count and nothing it carries is written; its
delta link is not persisted either, so once a person maps it the first sync is
complete.

**Snapshot close.** Role assignments have no delta, so each run reads them all
and stamps ``last_seen`` with the run's start. The final page, and only on a
live run, marks every assignment of the directory not seen since the run
started as removed. A run that stops part-way never reaches its final page and
closes nothing. A record that is malformed but names its id still counts as
seen, so a bad record never closes a live assignment.

**Replay** never closes an assignment and never brings back a deleted user or
removed assignment; ``last_seen`` only ever moves forward (to the page's fetch
time on replay).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.core import drift, identity, state
from cloudops_connectors.core.ingest import (
    Fetched,
    PageContext,
    PageInfo,
    PageOutcome,
    Source,
    principal,
)
from cloudops_connectors.core.raw import RawStore
from cloudops_connectors.entra import mapping
from cloudops_connectors.entra.client import ROLE_ASSIGNMENTS, USERS_DELTA
from cloudops_connectors.graph.client import (
    GRAPH,
    GraphClient,
    GraphError,
    GraphThrottledError,
    retry_later,
)
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

_UPSERT_USER: Final = text(
    "INSERT INTO cloudops.directory_principal (tenant_id, directory_id, object_id, kind,"
    " display_name, user_principal_name, account_enabled, user_type, created_at_source,"
    " raw_extra)"
    " VALUES (:tenant_id, :directory_id, :object_id, 'user', :display_name, :upn, :enabled,"
    "         :user_type, :created_at, CAST(:raw_extra AS jsonb))"
    " ON CONFLICT (directory_id, object_id) DO UPDATE SET"
    "   display_name = EXCLUDED.display_name,"
    "   user_principal_name = EXCLUDED.user_principal_name,"
    "   account_enabled = EXCLUDED.account_enabled, user_type = EXCLUDED.user_type,"
    "   created_at_source = EXCLUDED.created_at_source, raw_extra = EXCLUDED.raw_extra,"
    "   ingested_at = now(),"
    "   deleted_at = CASE WHEN CAST(:live AS boolean) THEN NULL"
    "                     ELSE cloudops.directory_principal.deleted_at END"
)
_REMOVE_USER: Final = text(
    "UPDATE cloudops.directory_principal SET deleted_at = coalesce(deleted_at, now())"
    " WHERE directory_id = :directory_id AND object_id = :object_id"
)
_UPSERT_ASSIGNMENT: Final = text(
    "INSERT INTO cloudops.directory_role_assignment (tenant_id, directory_id, assignment_id,"
    " principal_object_id, role_definition_id, role_name, directory_scope_id, first_seen,"
    " last_seen, raw_extra)"
    " VALUES (:tenant_id, :directory_id, :assignment_id, :principal, :role_id, :role_name,"
    "         :scope_id, :seen, :seen, CAST(:raw_extra AS jsonb))"
    " ON CONFLICT (directory_id, assignment_id) DO UPDATE SET"
    "   principal_object_id = EXCLUDED.principal_object_id,"
    "   role_definition_id = EXCLUDED.role_definition_id, role_name = EXCLUDED.role_name,"
    "   directory_scope_id = EXCLUDED.directory_scope_id, raw_extra = EXCLUDED.raw_extra,"
    "   last_seen = GREATEST(cloudops.directory_role_assignment.last_seen, EXCLUDED.last_seen),"
    "   removed_at = CASE WHEN CAST(:live AS boolean) THEN NULL"
    "                     ELSE cloudops.directory_role_assignment.removed_at END"
)
_TOUCH_ASSIGNMENT: Final = text(
    "UPDATE cloudops.directory_role_assignment SET last_seen = GREATEST(last_seen, :seen)"
    " WHERE directory_id = :directory_id AND assignment_id = :assignment_id"
)
_CLOSE_ASSIGNMENTS: Final = text(
    "UPDATE cloudops.directory_role_assignment SET removed_at = now()"
    " WHERE directory_id = :directory_id AND removed_at IS NULL AND last_seen < :started"
)


def _scope(tenants: Iterable[UUID]) -> TenantScope:
    return TenantScope(tenants, Role.INGEST, principal(mapping.SOURCE))


class EntraSource(Source):
    source = mapping.SOURCE
    entities = mapping.ENTITIES

    def __init__(self, directories: Mapping[str, GraphClient]) -> None:
        """``directories``: customer directory id -> a client holding that
        directory's token."""
        self._clients = dict(directories)
        bases = {c.base_url for c in self._clients.values()}
        if len(bases) > 1:
            raise ValueError("every directory must be read from the same Graph endpoint")
        self.instance = next(iter(bases), GRAPH)

    async def scopes(self, db: Database, entity: str) -> list[str]:
        return sorted(self._clients)

    async def fetch(
        self, entity: str, scope: str, since: datetime, cursor: str | None
    ) -> Fetched:
        if entity not in mapping.ENTITIES:
            raise ValueError(f"unknown entity {entity!r}")
        client = self._clients.get(scope)
        if client is None:
            raise ValueError("directory is not configured")
        first = USERS_DELTA if entity == mapping.USER else ROLE_ASSIGNMENTS
        try:
            try:
                body = await client.get(cursor or first)
            except GraphError as exc:
                if not (cursor and exc.status == 410):
                    raise
                body = await client.get(first)  # delta token expired: full resync
        except GraphThrottledError as exc:
            raise retry_later(exc) from None
        return Fetched(body, metadata={"directory_id": scope})

    async def inspect(
        self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes, cursor: str | None
    ) -> PageInfo:
        return PageInfo(len(mapping.records(body)), mapping.next_link(body))

    async def process(
        self, db: Database, raw: RawStore, entity: str, ref: str, ctx: PageContext
    ) -> PageOutcome:
        body = await raw.get(ref)
        records = mapping.records(body)
        counts = state.RunCounts(pages=1, records=len(records))
        async with db.transaction(_scope(())) as conn:
            tenant = (await identity.resolve_ids(conn, self.source, [ctx.scope]))[ctx.scope]
            if tenant is None:
                # Nobody has mapped this directory: nothing it carries is written.
                await state.quarantine_aliases(conn, self.source,
                                               {ctx.scope: max(len(records), 1)}, ref)
        if tenant is None:
            counts.quarantined = len(records)
            return PageOutcome(counts)
        async with db.transaction(_scope({tenant})) as conn:
            if entity == mapping.USER:
                await self._users(conn, tenant, ctx, records, counts)
                expected, mapped = mapping.USER_EXPECTED, mapping.USER_MAPPED
            else:
                seen = ctx.run_started if ctx.advance else await _fetched_at(raw, ref, ctx)
                await self._assignments(conn, tenant, ctx, records, counts, seen)
                expected, mapped = mapping.ASSIGNMENT_EXPECTED, mapping.ASSIGNMENT_MAPPED
            await drift.observe(conn, self.source, entity, drift.fields_of(records),
                                mapped=mapped, expected=expected, records=len(records),
                                raw_ref=ref)
            if ctx.final and ctx.advance:
                if entity == mapping.USER:
                    link = mapping.delta_link(body)
                    if link is not None:
                        await state.advance_cursor(conn, self.source, entity, ctx.scope_key,
                                                   link)
                else:
                    await conn.execute(_CLOSE_ASSIGNMENTS, {"directory_id": ctx.scope,
                                                            "started": ctx.run_started})
                await state.advance_watermark(conn, self.source, entity, ctx.scope_key,
                                              ctx.run_started)
        return PageOutcome(counts)

    async def _users(self, conn: ScopedConnection, tenant: UUID, ctx: PageContext,
                     records: list[dict[str, Any]], counts: state.RunCounts) -> None:
        latest: dict[str, mapping.User] = {}
        for record in records:
            try:
                u = mapping.user(record)
            except mapping.MalformedRecordError:
                counts.rejected += 1
                continue
            latest[u.object_id] = u
        for u in latest.values():
            if u.removed:
                await conn.execute(_REMOVE_USER, {"directory_id": ctx.scope,
                                                  "object_id": u.object_id})
            else:
                await conn.execute(_UPSERT_USER, {
                    "tenant_id": tenant, "directory_id": ctx.scope, "object_id": u.object_id,
                    "display_name": u.display_name, "upn": u.user_principal_name,
                    "enabled": u.account_enabled, "user_type": u.user_type,
                    "created_at": u.created_at, "live": ctx.advance,
                    "raw_extra": json.dumps(u.raw_extra, sort_keys=True) if u.raw_extra
                    else None,
                })
            counts.written += 1

    async def _assignments(self, conn: ScopedConnection, tenant: UUID, ctx: PageContext,
                           records: list[dict[str, Any]], counts: state.RunCounts,
                           seen: datetime) -> None:
        rows: list[dict[str, Any]] = []
        for record in records:
            try:
                a = mapping.assignment(record)
            except mapping.MalformedRecordError:
                counts.rejected += 1
                named = record.get("id")
                if ctx.advance and isinstance(named, str) and named:
                    await conn.execute(_TOUCH_ASSIGNMENT, {
                        "seen": seen, "directory_id": ctx.scope, "assignment_id": named})
                continue
            rows.append({
                "tenant_id": tenant, "directory_id": ctx.scope, "assignment_id": a.assignment_id,
                "principal": a.principal_object_id, "role_id": a.role_definition_id,
                "role_name": a.role_name, "scope_id": a.directory_scope_id, "seen": seen,
                "live": ctx.advance,
                "raw_extra": json.dumps(a.raw_extra, sort_keys=True) if a.raw_extra else None,
            })
        if rows:
            await conn.execute(_UPSERT_ASSIGNMENT, rows)
        counts.written += len(rows)


async def _fetched_at(raw: RawStore, ref: str, ctx: PageContext) -> datetime:
    """When a replayed page was read from Graph: what it proves was seen then."""
    stamp = (await raw.meta(ref)).get("fetched_at")
    try:
        return datetime.fromisoformat(stamp) if stamp else ctx.run_started
    except ValueError:
        return ctx.run_started
