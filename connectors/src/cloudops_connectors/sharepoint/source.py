"""SharePoint through the connector contract (``core/ingest.py``).

scopes    the granted sites in ``cloudops.sharepoint_site`` (see below)
fetch     one delta page: the site's delta, or the persisted deltaLink, then
          nextLinks. 403 ``accessDenied`` or 404 ``itemNotFound`` on the site
          means its consent was withdrawn: ``ScopeRevokedError``
inspect   after the page is in bronze: every changed text file's content is
          downloaded and stored in bronze beside it (``.{item}.content``)
process   upsert ``cloudops.document`` with its kind; a changed text file's
          chunks are replaced in ``knowledge_chunk``, each with its document,
          position and page (no embedding: the knowledge indexer adds it); a
          deleted item's chunks go and its document is marked deleted; on the
          final page the deltaLink is persisted, in the same transaction
revoke    the site is marked revoked, every chunk of it deleted, its documents
          marked deleted and its delta link reset (a re-grant starts afresh)

**Which sites.** ``sharepoint_site`` is a tenant table under FORCE RLS, and the
ingest login can see no tenant it is not scoped to; it cannot list tenants
either. So the source is configured with the customer names that have granted
sites (the names sites are registered under), resolves them with
``cloudops.resolve_tenant_alias`` as every connector does, and reads the sites
under exactly those tenants. A site is read only while its row says
``granted``: registered (``register_site``), visible under a resolved tenant,
and not revoked. The check is repeated, with the row locked, in every page's
write transaction, so a page fetched before a revocation writes nothing after it,
and replaying a revoked site's bronze writes nothing at all.

**Replay** never re-fetches: content is read from bronze only. It does not
bring back a deleted document, and an older version never overwrites a newer
one (``lastModifiedDateTime`` decides), so replaying any range is safe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from azure.core.exceptions import ResourceNotFoundError
from sqlalchemy import text

from cloudops_connectors.core import drift, state
from cloudops_connectors.core.ingest import (
    Fetched,
    MissingRawError,
    PageContext,
    PageInfo,
    PageOutcome,
    ScopeRevokedError,
    Source,
    principal,
    scope_key,
)
from cloudops_connectors.core.raw import RawStore, sha256
from cloudops_connectors.graph.client import (
    GraphClient,
    GraphError,
    GraphThrottledError,
    retry_later,
)
from cloudops_connectors.sharepoint import mapping
from cloudops_connectors.sharepoint.client import content_path, delta_path
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

CONTENT_SUFFIX: Final = ".content"
_SAFE_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")

_SITES: Final = text(
    "SELECT site_id, tenant_id, consent FROM cloudops.sharepoint_site"
    " WHERE site_id = ANY(CAST(:sites AS text[])) OR CAST(:all AS boolean)"
)
_REGISTER: Final = text(
    "INSERT INTO cloudops.sharepoint_site (tenant_id, site_id, site_url, customer_raw)"
    " VALUES (:tenant_id, :site_id, :site_url, :customer)"
    " ON CONFLICT (site_id) DO UPDATE SET site_url = EXCLUDED.site_url,"
    "   customer_raw = EXCLUDED.customer_raw, consent = 'granted', revoked_at = NULL"
    " RETURNING tenant_id"
)
_LOCK_SITE: Final = text(
    "SELECT consent FROM cloudops.sharepoint_site WHERE site_id = :site FOR SHARE"
)
_DOC_COLUMNS: Final = (
    "tenant_id", "source_system", "site_id", "item_id", "name", "web_url", "mime_type",
    "etag", "size_bytes", "modified_at", "extraction", "chunk_count", "raw_extra",
    "doc_kind", "folder_path",
)
_UPSERT_DOC: Final = text(
    f"INSERT INTO cloudops.document ({', '.join(_DOC_COLUMNS)}) VALUES ("  # noqa: S608
    + ", ".join(f"CAST(:{c} AS jsonb)" if c == "raw_extra" else f":{c}" for c in _DOC_COLUMNS)
    + ") ON CONFLICT (source_system, site_id, item_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _DOC_COLUMNS
                if c not in ("tenant_id", "source_system", "site_id", "item_id"))
    + ", deleted_at = NULL, ingested_at = now()"
    # An older version never overwrites a newer one (a replay of old bronze).
    " WHERE cloudops.document.modified_at IS NULL OR EXCLUDED.modified_at IS NULL"
    "    OR EXCLUDED.modified_at >= cloudops.document.modified_at"
    " RETURNING id"
)
_DELETED_DOC: Final = text(
    "SELECT deleted_at IS NOT NULL FROM cloudops.document"
    " WHERE source_system = :source AND site_id = :site AND item_id = :item"
)
_MARK_DELETED: Final = text(
    "UPDATE cloudops.document SET deleted_at = coalesce(deleted_at, now()), chunk_count = 0"
    " WHERE source_system = :source AND site_id = :site AND item_id = :item"
)
_DELETE_CHUNKS: Final = text(
    "DELETE FROM cloudops.knowledge_chunk WHERE source_kind = :kind AND source_ref = :ref"
)
_INSERT_CHUNK: Final = text(
    "INSERT INTO cloudops.knowledge_chunk (tenant_id, source_kind, source_ref, title, body,"
    " document_id, ordinal, page)"
    " VALUES (:tenant_id, :kind, :ref, :title, :body, :document_id, :ordinal, :page)"
)
_REVOKE_SITE: Final = text(
    "UPDATE cloudops.sharepoint_site SET consent = 'revoked',"
    " revoked_at = coalesce(revoked_at, now()) WHERE site_id = :site"
)
_DELETE_SITE_CHUNKS: Final = text(
    "DELETE FROM cloudops.knowledge_chunk"
    " WHERE source_kind = :kind AND starts_with(source_ref, :prefix)"
)
_DELETE_SITE_DOCS: Final = text(
    "UPDATE cloudops.document SET deleted_at = coalesce(deleted_at, now()), chunk_count = 0"
    " WHERE source_system = :source AND site_id = :site"
)


class UnresolvedCustomerError(Exception):
    """The customer name a site was offered under resolves to no tenant. The
    name is quarantined for the customer mapping; the site is not registered."""


@dataclass(frozen=True)
class Site:
    site_id: str
    tenant_id: UUID
    consent: str


def content_ref(page_ref: str, item_id: str) -> str:
    """Where an item's content is stored, beside the page that named it."""
    safe = item_id if _SAFE_ID.fullmatch(item_id) else sha256(item_id.encode())[:32]
    return f"{page_ref.removesuffix('.json')}.{safe}{CONTENT_SUFFIX}"


def _scope(tenants: Iterable[UUID]) -> TenantScope:
    return TenantScope(tenants, Role.INGEST, principal(mapping.SOURCE))


async def _tenants(db: Database, names: Iterable[str]) -> set[UUID]:
    async with db.transaction(_scope(())) as conn:
        resolved = await state.resolve_aliases(conn, mapping.SOURCE, names)
    return {r.tenant_id for r in resolved.values() if r.tenant_id is not None}


async def sites(db: Database, customers: Sequence[str],
                only: Sequence[str] | None = None) -> dict[str, Site]:
    """The registered sites of the configured customers, granted or not."""
    tenants = await _tenants(db, customers)
    if not tenants:
        return {}
    async with db.transaction(_scope(tenants)) as conn:
        rows = [tuple(r) for r in await conn.execute(
            _SITES, {"sites": list(only or []), "all": only is None})]
    return {str(r[0]): Site(str(r[0]), UUID(str(r[1])), str(r[2])) for r in rows}


async def register_site(db: Database, customer: str, site_id: str, site_url: str) -> UUID:
    """Record a site's Sites.Selected grant under the customer it belongs to.

    The name resolves as every connector's does (exact, then normalised). One
    that does not is quarantined (source ``sharepoint``) for a person to map,
    and the site is refused: a site is never filed under a guessed customer.
    Registering a revoked site again records a new grant."""
    if not customer.strip() or not site_id.strip() or not site_url.startswith("https://"):
        raise ValueError("customer, site id and an https site URL are required")
    async with db.transaction(_scope(())) as conn:
        resolved = (await state.resolve_aliases(conn, mapping.SOURCE, [customer]))[customer]
        if resolved.tenant_id is None:
            await state.quarantine_aliases(conn, mapping.SOURCE, {customer: 1},
                                           f"register:{site_id}")
    if resolved.tenant_id is None:
        raise UnresolvedCustomerError(f"customer did not resolve ({resolved.outcome})")
    async with db.transaction(_scope({resolved.tenant_id})) as conn:
        await conn.execute(_REGISTER, {"tenant_id": resolved.tenant_id, "site_id": site_id,
                                       "site_url": site_url, "customer": customer})
    return resolved.tenant_id


class SharePointSource(Source):
    source = mapping.SOURCE
    entities = mapping.ENTITIES

    def __init__(self, client: GraphClient, customers: Sequence[str]) -> None:
        self._client = client
        self._customers = tuple(sorted(set(customers)))
        self.instance = client.base_url

    async def scopes(self, db: Database, entity: str) -> list[str]:
        found = await sites(db, self._customers)
        return sorted(s.site_id for s in found.values() if s.consent == "granted")

    async def fetch(
        self, entity: str, scope: str, since: datetime, cursor: str | None
    ) -> Fetched:
        if entity != mapping.ENTITY:
            raise ValueError(f"unknown entity {entity!r}")
        try:
            try:
                body = await self._client.get(cursor or delta_path(scope))
            except GraphError as exc:
                if not (cursor and exc.status == 410):
                    raise
                # The delta token expired: Graph asks for a full resync.
                body = await self._client.get(delta_path(scope))
        except GraphError as exc:
            if (exc.status, exc.code) in ((403, "accessDenied"), (404, "itemNotFound")):
                raise ScopeRevokedError("site consent withdrawn") from None
            raise
        except GraphThrottledError as exc:
            raise retry_later(exc) from None
        return Fetched(body, metadata={"site_id": scope})

    async def inspect(
        self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes, cursor: str | None
    ) -> PageInfo:
        records = mapping.items(body)
        for record in records:
            try:
                item = mapping.normalise(record)
            except mapping.MalformedRecordError:
                continue
            if not item.wants_content:
                continue
            status = "200"
            try:
                data = await self._client.download(content_path(scope, item.item_id))
            except GraphError as exc:
                if exc.status == 403:
                    raise ScopeRevokedError("site consent withdrawn") from None
                if exc.status != 404:
                    raise
                data, status = b"", "404"  # gone since the delta; the next one says so
            except GraphThrottledError as exc:
                raise retry_later(exc) from None
            await raw.put(content_ref(ref, item.item_id), data, {
                "source": self.source, "entity": "drive_item_content", "page_ref": ref,
                "scope": scope, "item_id": item.item_id, "status": status,
                "sha256": sha256(data), "instance": self.instance,
            })
        return PageInfo(len(records), mapping.next_link(body))

    async def _content(self, raw: RawStore, ref: str, item: mapping.DriveItem) -> str | None:
        """The file's text from bronze; None if Graph had none to give."""
        cref = content_ref(ref, item.item_id)
        try:
            meta = await raw.meta(cref)
            data = await raw.get(cref)
        except (KeyError, ResourceNotFoundError):
            raise MissingRawError(f"no content beside {ref}") from None
        return mapping.decode(data) if meta.get("status", "200") == "200" else None

    async def process(
        self, db: Database, raw: RawStore, entity: str, ref: str, ctx: PageContext
    ) -> PageOutcome:
        if ref.endswith(CONTENT_SUFFIX):
            return PageOutcome(state.RunCounts())  # a companion payload, read with its page
        body = await raw.get(ref)
        records = mapping.items(body)
        counts = state.RunCounts(pages=1, records=len(records))
        latest: dict[str, mapping.DriveItem] = {}
        for record in records:
            try:
                item = mapping.normalise(record)
            except mapping.MalformedRecordError:
                counts.rejected += 1
                continue
            latest[item.item_id] = item  # an item twice in a page: the later wins
        texts: dict[str, str] = {}
        for item in list(latest.values()):
            if item.wants_content:
                content = await self._content(raw, ref, item)
                if content is None:
                    latest[item.item_id] = replace(item, extraction="unsupported")
                else:
                    texts[item.item_id] = content

        site = (await sites(db, self._customers, [ctx.scope])).get(ctx.scope)
        if site is None or site.consent != "granted":
            return PageOutcome(counts)  # not ours to write; the page stays in bronze
        async with db.transaction(_scope({site.tenant_id})) as conn:
            if await conn.scalar(_LOCK_SITE, {"site": ctx.scope}) != "granted":
                return PageOutcome(counts)
            for item in latest.values():
                if item.root:
                    continue
                if item.deleted:
                    await _delete_item(conn, ctx.scope, item.item_id)
                    counts.written += 1
                elif await _write_item(conn, site.tenant_id, ctx, item,
                                       texts.get(item.item_id)):
                    counts.written += 1
            await drift.observe(conn, self.source, entity, drift.fields_of(records),
                                mapped=mapping.MAPPED, expected=mapping.EXPECTED,
                                records=len(records), raw_ref=ref)
            if ctx.final and ctx.advance:
                link = mapping.delta_link(body)
                if link is not None:
                    await state.advance_cursor(conn, self.source, entity, ctx.scope_key, link)
                await state.advance_watermark(conn, self.source, entity, ctx.scope_key,
                                              ctx.run_started)
        return PageOutcome(counts)

    async def revoke(self, db: Database, entity: str, scope: str) -> int:
        site = (await sites(db, self._customers, [scope])).get(scope)
        if site is None:
            return 0
        async with db.transaction(_scope({site.tenant_id})) as conn:
            await conn.execute(_REVOKE_SITE, {"site": scope})
            gone = (await conn.execute(_DELETE_SITE_CHUNKS, {
                "kind": mapping.SOURCE_KIND, "prefix": mapping.site_prefix(scope)})).rowcount
            await conn.execute(_DELETE_SITE_DOCS, {"source": self.source, "site": scope})
            # A re-grant must start with a full sync: the old delta link would
            # only report changes, and every chunk is gone.
            await state.advance_cursor(conn, self.source, entity,
                                       scope_key(self.instance, scope), "")
        return int(gone)


async def _delete_item(conn: ScopedConnection, site: str, item: str) -> None:
    await conn.execute(_MARK_DELETED, {"source": mapping.SOURCE, "site": site, "item": item})
    await conn.execute(_DELETE_CHUNKS, {"kind": mapping.SOURCE_KIND,
                                        "ref": mapping.source_ref(site, item)})


async def _write_item(conn: ScopedConnection, tenant: UUID, ctx: PageContext,
                      item: mapping.DriveItem, body: str | None) -> bool:
    if not ctx.advance and await conn.scalar(_DELETED_DOC, {
            "source": mapping.SOURCE, "site": ctx.scope, "item": item.item_id}):
        return False  # replay never brings a deleted document back
    chunks = mapping.pieces(body) if body is not None and item.extraction == "text" else []
    written = await conn.scalar(_UPSERT_DOC, _doc_row(tenant, ctx.scope, item, len(chunks)))
    if written is None:
        return False  # bronze older than what is stored
    ref = mapping.source_ref(ctx.scope, item.item_id)
    await conn.execute(_DELETE_CHUNKS, {"kind": mapping.SOURCE_KIND, "ref": ref})
    if chunks:
        # Verbatim: document text is data, never instruction.
        await conn.execute(_INSERT_CHUNK, [
            {"tenant_id": tenant, "kind": mapping.SOURCE_KIND, "ref": ref, "title": item.name,
             "body": c.body, "document_id": written, "ordinal": c.ordinal, "page": c.page}
            for c in chunks])
    return True


def _doc_row(tenant: UUID, site: str, item: mapping.DriveItem, chunks: int) -> dict[str, Any]:
    return {
        "tenant_id": tenant, "source_system": mapping.SOURCE, "site_id": site,
        "item_id": item.item_id, "name": item.name, "web_url": item.web_url,
        "mime_type": item.mime_type, "etag": item.etag, "size_bytes": item.size,
        "modified_at": item.modified_at, "extraction": item.extraction, "chunk_count": chunks,
        "raw_extra": json.dumps(item.raw_extra, sort_keys=True) if item.raw_extra else None,
        "doc_kind": item.doc_kind, "folder_path": item.folder_path,
    }
