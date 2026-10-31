"""Knowledge and document intelligence against the real schema (step 17, ADR 0020).

Staleness and gaps are computed under the caller's scope, the indexer embeds
and indexes one tenant at a time and never touches another's chunks, and a
retrieved passage cites its document and page. Isolation: another tenant's
document, chunk or citation is invisible, and its pending chunks stay pending.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from cloudops_domain import knowledge
from cloudops_gateway.gateway import CallContext, Gateway
from cloudops_gateway.pricing import LOCAL_DEPLOYMENTS
from cloudops_gateway.providers import MockProvider, Provider
from cloudops_gateway.retrieval import KnowledgeIndex, KnowledgeIndexer
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Step17

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

NOW = datetime.now(UTC)


@dataclass(frozen=True)
class Docs:
    a_design: UUID
    a_design_chunk: UUID
    b_handover: UUID
    b_chunk: UUID


@pytest.fixture(scope="module")
def docs(step17: Step17) -> Docs:
    s = step17.seeded
    a, b = s.a, s.b
    ids = [uuid4() for _ in range(4)]
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        # The estate as first discovered, before either document was written.
        cx.execute("UPDATE cloudops.resource SET first_seen = %s WHERE tenant_id = ANY(%s)",
                   (NOW - timedelta(days=200), [a.id, b.id]))
        # Tenant a's design names a VM that was removed after the design was written.
        cx.execute(
            "INSERT INTO cloudops.resource (tenant_id, account_id, native_id, name, type_native,"
            " type_canonical, first_seen, deleted_at) VALUES (%s, %s, '/vm/gone', 'vm-alpha-gone',"
            " 'Microsoft.Compute/virtualMachines', 'compute.vm', %s, %s)",
            (a.id, a.account_id, NOW - timedelta(days=200), NOW - timedelta(days=10)))
        for doc, tenant, name, kind, modified in (
            (ids[0], a.id, "Alpha Solution Design.md", "design", NOW - timedelta(days=60)),
            (ids[2], b.id, "Bravo Handover Pack.md", "handover", NOW - timedelta(days=5)),
        ):
            cx.execute(
                "INSERT INTO cloudops.document (id, tenant_id, source_system, site_id, item_id,"
                " name, web_url, modified_at, extraction, doc_kind) VALUES (%s, %s, 'sharepoint',"
                " %s, %s, %s, %s, %s, 'text', %s)",
                (doc, tenant, f"site-k-{doc}", f"item-{doc}", name,
                 f"https://example.sharepoint.test/{doc}", modified, kind))
        cx.execute(
            "INSERT INTO cloudops.knowledge_chunk (id, tenant_id, source_kind, source_ref, title,"
            " body, document_id, ordinal, page) VALUES (%s, %s, 'sharepoint', %s,"
            " 'Alpha Solution Design.md', %s, %s, 0, 2)",
            (ids[1], a.id, f"sharepoint:site-k:{ids[0]}",
             "The web tier runs on vm-alpha-01 and vm-alpha-gone behind a load balancer.",
             ids[0]))
        cx.execute(
            "INSERT INTO cloudops.knowledge_chunk (id, tenant_id, source_kind, source_ref, title,"
            " body, document_id, ordinal, page) VALUES (%s, %s, 'sharepoint', %s,"
            " 'Bravo Handover Pack.md', 'Bravo handover: escalate to the bravo duty manager.',"
            " %s, 0, 1)",
            (ids[3], b.id, f"sharepoint:site-k:{ids[2]}", ids[2]))
    return Docs(*ids)


async def test_staleness_names_the_removed_resource(step17: Step17, docs: Docs) -> None:
    db = Database.create(step17.seeded.app_dsn, pool_size=1, max_overflow=0)
    try:
        scope = TenantScope([step17.seeded.a.id], Role.ENGINEER, "test:knowledge")
        async with db.transaction(scope) as conn:
            statuses = await knowledge.document_statuses(conn, NOW)
    finally:
        await db.dispose()
    assert [s.document_id for s in statuses] == [docs.a_design]  # never bravo's
    (design,) = statuses
    assert design.removed_resources == ("vm-alpha-gone",)
    assert design.resources_named == 2
    assert design.reasons == ("removed_resources",)


async def test_documents_api_is_scoped_and_staff_only(step17: Step17, docs: Docs) -> None:
    api = step17.api
    mine = (await api.get("/v1/knowledge/documents", step17.engineer_a)).json()
    assert [d["id"] for d in mine["items"]] == [str(docs.a_design)]
    assert mine["items"][0]["stale"] is True
    assert mine["items"][0]["reasons"] == ["removed_resources"]
    assert mine["stale"]["value"] == 1
    every = (await api.get("/v1/knowledge/documents", step17.admin)).json()
    assert {d["id"] for d in every["items"]} == {str(docs.a_design), str(docs.b_handover)}
    fresh = (await api.get("/v1/knowledge/documents", step17.admin,
                           params={"stale": "false"})).json()
    assert [d["id"] for d in fresh["items"]] == [str(docs.b_handover)]
    # Refused either way: 403 without knowledge.read (the portal's roles), 404 with it.
    assert (await api.get("/v1/knowledge/documents", step17.customer_a)).status_code in (403, 404)


async def test_gaps_cover_every_customer_in_scope(step17: Step17, docs: Docs) -> None:
    s = step17.seeded
    gaps = {g["tenant"]["id"]: g for g in
            (await step17.api.get("/v1/knowledge/gaps", step17.admin)).json()}
    assert gaps[str(s.a.id)]["design"] == "stale"
    assert gaps[str(s.a.id)]["handover"] == "missing"
    assert gaps[str(s.b.id)]["design"] == "missing"
    assert gaps[str(s.b.id)]["handover"] == "current"
    assert gaps[str(s.c.id)]["has_gap"] is True
    mine = (await step17.api.get("/v1/knowledge/gaps", step17.engineer_a)).json()
    assert [g["tenant"]["id"] for g in mine] == [str(s.a.id)]


async def test_a_citation_names_document_and_page_and_never_crosses(
    step17: Step17, docs: Docs
) -> None:
    api = step17.api
    got = (await api.get(f"/v1/knowledge/chunks/{docs.a_design_chunk}/citation",
                         step17.engineer_a)).json()
    assert got["document_name"] == "Alpha Solution Design.md"
    assert got["page"] == "2"
    assert got["label"] == "Alpha Solution Design.md, page 2"
    foreign = await api.get(f"/v1/knowledge/chunks/{docs.b_chunk}/citation", step17.engineer_a)
    assert foreign.status_code == 404
    missing = await api.get(f"/v1/knowledge/chunks/{uuid4()}/citation", step17.engineer_a)
    assert missing.status_code == 404


async def test_the_indexer_embeds_and_indexes_only_its_tenant(step17: Step17, docs: Docs
                                                               ) -> None:
    s = step17.seeded
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        for t in (s.a, s.b):
            cx.execute(
                "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
                " closed_at, close_notes, short_description) VALUES (%s, 'incident', %s, %s,"
                " %s, %s, %s, 'Disk full')",
                (t.id, f"INC-K-{t.name}", t.name, NOW - timedelta(days=3),
                 NOW - timedelta(days=2), f"Cleared the {t.name} temp folder."))
    db = Database.create(s.app_dsn, pool_size=2, max_overflow=0)
    provider = MockProvider("mock")
    gateway = Gateway(db, {"mock": provider, "mock-secondary": MockProvider("mock-secondary")},
                      LOCAL_DEPLOYMENTS)
    ctx = CallContext.for_tenant(TenantScope([s.a.id], Role.ENGINEER, "service:knowledge-indexer"),
                                 s.a.id, ["knowledge.read"])
    try:
        indexer = KnowledgeIndexer(db, gateway, batch=2)
        assert await indexer.index_close_notes(ctx) == 1
        assert await indexer.index_close_notes(ctx) == 0  # once only
        embedded = await indexer.embed_pending(ctx)
        hits = await KnowledgeIndex(db, gateway).search(
            ctx, "web tier vm-alpha-01 load balancer", kinds=["sharepoint"], limit=3)
    finally:
        await db.dispose()
    assert embedded >= 2  # the design chunk and the close note
    assert "Cleared the bravo temp folder." not in provider.embedded
    with psycopg.connect(s.admin_dsn) as cx:
        rows = cx.execute(
            "SELECT tenant_id, source_kind, embedding IS NULL FROM cloudops.knowledge_chunk"
            " WHERE id = ANY(%s) OR source_ref LIKE 'INC-K-%%'",
            ([docs.a_design_chunk, docs.b_chunk],)).fetchall()
    state = {(r[0], r[1]): r[2] for r in rows}
    assert state[(s.a.id, "sharepoint")] is False  # embedded
    assert state[(s.a.id, "close_note")] is False
    assert state[(s.b.id, "sharepoint")] is True  # bravo's chunk untouched
    assert (s.b.id, "close_note") not in state  # bravo's note not indexed under alpha
    assert hits
    assert hits[0].chunk_id == docs.a_design_chunk
    assert hits[0].citation()["label"] == "Alpha Solution Design.md, page 2"
    assert all(h.tenant_id in (s.a.id, None) for h in hits)


async def test_the_indexer_never_overwrites_a_changed_chunk(step17: Step17) -> None:
    s = step17.seeded
    chunk = uuid4()
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.knowledge_chunk (id, tenant_id, source_kind, body)"
                   " VALUES (%s, %s, 'sharepoint', 'first version')", (chunk, s.c.id))
    db = Database.create(s.app_dsn, pool_size=1, max_overflow=0)

    class Racing(MockProvider):
        async def embed(self, deployment: str, texts: Sequence[str]) -> list[list[float]]:
            with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
                cx.execute("UPDATE cloudops.knowledge_chunk SET body = 'second version'"
                           " WHERE id = %s", (chunk,))
            return await super().embed(deployment, texts)

    racing: Provider = Racing("mock")
    gateway = Gateway(db, {"mock": racing}, LOCAL_DEPLOYMENTS)
    ctx = CallContext.for_tenant(TenantScope([s.c.id], Role.ENGINEER, "service:indexer"),
                                 s.c.id, [])
    try:
        await KnowledgeIndexer(db, gateway).embed_pending(ctx, limit=1)
    finally:
        await db.dispose()
    with psycopg.connect(s.admin_dsn) as cx:
        row = cx.execute("SELECT embedding IS NULL FROM cloudops.knowledge_chunk WHERE id = %s",
                         (chunk,)).fetchone()
    assert row == (True,)  # the body changed under it: the stale vector is not stored


def test_sharepoint_chunks_carry_document_ordinal_and_page() -> None:
    from cloudops_connectors.sharepoint import mapping

    body = "Intro paragraph.\n\nMore intro.\f\nPage two starts here.\n\n" + ("word " * 500)
    pieces = mapping.pieces(body, limit=300)
    assert [p.ordinal for p in pieces] == list(range(len(pieces)))
    assert pieces[0].page == 1
    assert pieces[-1].page == 2
    item = mapping.normalise({
        "id": "x", "name": "Northwind Handover.md", "file": {"mimeType": "text/markdown"},
        "parentReference": {"path": "/drive/root:/Customer/Docs"}})
    assert (item.doc_kind, item.folder_path) == ("handover", "/Customer/Docs")
