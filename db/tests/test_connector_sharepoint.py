"""The SharePoint connector end to end: the generic connector workflow on a
time-skipping Temporal server, the migrated database (a module of its own,
holding the synthetic tenants), and the recorded sites served by the fake
Graph through the real client.

Ground truth is the fixture itself (``cloudops_devdata.graph_fixtures``): which
customer each site belongs to, every item it holds, and every text file's
content. The synthetic dataset has no ``sharepoint`` aliases, so this module
registers the customers' exact names as a person would through the customer
mapping (``graph_fixtures.aliases``); a site registered under a variant
spelling resolves through normalisation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from temporalio import activity
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    PageOutcome,
    ProcessRequest,
    ReplayRequest,
    RunReport,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.graph.client import GraphClient
from cloudops_connectors.sharepoint import mapping
from cloudops_connectors.sharepoint.fake import FIXTURE_GRAPH, FakeSharePoint, load_fixture
from cloudops_connectors.sharepoint.source import (
    CONTENT_SUFFIX,
    SharePointSource,
    UnresolvedCustomerError,
    register_site,
)
from cloudops_devdata.graph_fixtures import INJECTION, UNREGISTERED_CUSTOMER, aliases
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

ENTITY = mapping.ENTITY
SITES = load_fixture()["sites"]
OPS, PROJECTS, CRL_OPS = (s["site_id"] for s in SITES)


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(scope="module")
def mapped(synthetic_module: Synthetic) -> Synthetic:
    """The customer mapping a person would have approved before this connector ran."""
    with psycopg.connect(synthetic_module.admin_dsn, autocommit=True) as cx:
        for source, alias, code in aliases():
            if source == mapping.SOURCE:
                cx.execute("INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias,"
                           " confirmed_by) VALUES (%s, %s, %s, 'test:mapping')"
                           " ON CONFLICT DO NOTHING", (tenant_id(code), source, alias))
    return synthetic_module


@pytest.fixture
async def db(mapped: Synthetic) -> AsyncIterator[Database]:
    _clear(mapped.admin_dsn)
    database = Database.create(mapped.ingest_dsn, pool_size=2, max_overflow=0)
    for s in SITES:
        await register_site(database, s["customer"], s["site_id"], s["site_url"])
    yield database
    await database.dispose()


def _clear(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as cx:
        cx.execute("DELETE FROM cloudops.knowledge_chunk WHERE source_kind = 'sharepoint'")
        for table in ("cloudops.document", "cloudops.sharepoint_site",
                      "staging.tenant_alias_quarantine"):
            cx.execute(f"DELETE FROM {table}")
        for table in ("staging.ingest_cursor", "staging.ingest_watermark",
                      "staging.schema_field"):
            cx.execute(f"DELETE FROM {table} WHERE source_system = 'sharepoint'")


def _source(fake: FakeSharePoint) -> SharePointSource:
    client = GraphClient(FIXTURE_GRAPH, transport=fake.transport(), sleep=no_sleep)
    return SharePointSource(client, [s["customer"] for s in SITES])


async def _ingest(env: WorkflowEnvironment, acts: ConnectorActivities) -> RunReport:
    (report,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    return report


def _chunks(dsn: str, site: str) -> dict[str, list[str]]:
    """source_ref -> chunk bodies, sorted: knowledge_chunk has no ordinal."""
    out: dict[str, list[str]] = {}
    for ref, body in rows(dsn, "SELECT source_ref, body FROM cloudops.knowledge_chunk"
                               " WHERE source_kind = 'sharepoint'"
                               " AND starts_with(source_ref, %s) ORDER BY body",
                          mapping.site_prefix(site)):
        out.setdefault(ref, []).append(body)
    return out


def _docs(dsn: str, site: str) -> dict[str, tuple[Any, ...]]:
    return {r[0]: r[1:] for r in rows(
        dsn, "SELECT item_id, tenant_id, name, extraction, chunk_count, deleted_at, raw_extra"
             " FROM cloudops.document WHERE site_id = %s", site)}


# --- registration -------------------------------------------------------------------------


async def test_a_site_under_an_unmapped_customer_is_refused_and_the_name_quarantined(
    db: Database, mapped: Synthetic
) -> None:
    with pytest.raises(UnresolvedCustomerError):
        await register_site(db, UNREGISTERED_CUSTOMER, "getronics.sharepoint.com,x,y",
                            "https://getronics.sharepoint.com/sites/pharma")
    dsn = mapped.admin_dsn
    assert rows(dsn, "SELECT count(*) FROM cloudops.sharepoint_site WHERE site_id = %s",
                "getronics.sharepoint.com,x,y") == [(0,)]
    assert rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                     " WHERE source_system = 'sharepoint'") == [(UNREGISTERED_CUSTOMER, 1)]
    # The variant spelling resolved through normalisation, to the right tenant.
    assert rows(dsn, "SELECT site_id, tenant_id, customer_raw, consent"
                     " FROM cloudops.sharepoint_site ORDER BY site_url") == sorted(
        [(s["site_id"], tenant_id(s["code"]), s["customer"], "granted") for s in SITES],
        key=lambda r: next(x["site_url"] for x in SITES if x["site_id"] == r[0]))


# --- ingest ---------------------------------------------------------------------------------


async def test_ingest_lands_every_document_and_chunk_where_the_fixture_says(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    raw, fake = MemoryRawStore(), FakeSharePoint.from_fixtures(page_size=3)
    report = await _ingest(env, ConnectorActivities(db, raw, _source(fake)))
    assert report.status == "succeeded"
    assert sorted(s.scope for s in report.scopes) == sorted([OPS, PROJECTS, CRL_OPS])
    assert {s.status for s in report.scopes} == {"succeeded"}
    assert report.counts.rejected == len(SITES), "one record with no id per site"
    assert report.counts.records == sum(len(s["delta"]) for s in SITES)

    for s in SITES:
        docs = _docs(dsn, s["site_id"])
        expected = [i for i in s["delta"] if "id" in i and "root" not in i]
        assert set(docs) == {i["id"] for i in expected}
        chunks = _chunks(dsn, s["site_id"])
        for item in expected:
            tenant, name, extraction, count, deleted, extra = docs[item["id"]]
            assert (tenant, name, deleted) == (tenant_id(s["code"]), item["name"], None)
            ref = mapping.source_ref(s["site_id"], item["id"])
            if "folder" in item:
                assert (extraction, count) == ("folder", 0)
            elif item["id"] in s["content"]:
                want = mapping.chunk(s["content"][item["id"]])
                assert extraction == "text"
                assert count == len(want) > 0
                assert chunks[ref] == sorted(want), item["name"]
            else:
                assert (extraction, count) == ("unsupported", 0)
                assert ref not in chunks
            assert "parentReference" in extra, "unmapped fields are kept"
    assert any(len(c) > 1 for c in _chunks(dsn, OPS).values()), "a long file, several chunks"
    assert rows(dsn, "SELECT DISTINCT tenant_id FROM cloudops.knowledge_chunk"
                     " WHERE starts_with(source_ref, %s)", mapping.site_prefix(CRL_OPS)) == [
        (tenant_id("CRL"),)]
    # Text is data: stored verbatim, nothing acted on it.
    assert rows(dsn, "SELECT count(*) FROM cloudops.knowledge_chunk WHERE body LIKE %s",
                f"%{INJECTION}%") == [(2,)]
    assert rows(dsn, "SELECT count(*) FROM cloudops.knowledge_chunk"
                     " WHERE source_kind = 'sharepoint' AND (embedding IS NOT NULL"
                     "   OR title IS NULL)") == [(0,)]
    # Raw before parsed; content beside its page.
    assert raw_first(raw.events)
    assert sum(r.endswith(CONTENT_SUFFIX) for r in raw.pages) == sum(
        len(s["content"]) for s in SITES)
    # Each site's delta link, from its committed final page; a watermark per site.
    cursors = dict(rows(dsn, "SELECT scope_key, cursor FROM staging.ingest_cursor"
                             " WHERE source_system = 'sharepoint'"))
    assert set(cursors) == {f"{FIXTURE_GRAPH}|{s['site_id']}" for s in SITES}
    assert all("$deltatoken=" in c for c in cursors.values())
    assert rows(dsn, "SELECT count(*) FROM staging.ingest_watermark"
                     " WHERE source_system = 'sharepoint'") == [(3,)]


class _FailsFinalPage(ConnectorActivities):
    @activity.defn(name="process")
    async def process(self, req: ProcessRequest) -> PageOutcome:
        if req.final:
            raise RuntimeError("normaliser defect")
        return await super().process(req)


async def test_the_delta_link_is_persisted_only_by_a_committed_final_page(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    fake = FakeSharePoint.from_fixtures(page_size=3)
    report = await _ingest(env, _FailsFinalPage(db, MemoryRawStore(), _source(fake)))
    assert report.status == "dead_lettered"
    assert {s.status for s in report.scopes} == {"dead_lettered"}
    assert rows(dsn, "SELECT count(*) FROM cloudops.document") != [(0,)], "earlier pages landed"
    assert rows(dsn, "SELECT count(*) FROM staging.ingest_cursor"
                     " WHERE source_system = 'sharepoint'") == [(0,)]
    # The next run starts each site from the beginning again, and finishes.
    before = len(fake.requests)
    again = await _ingest(env, ConnectorActivities(db, MemoryRawStore(), _source(fake)))
    assert again.status == "succeeded"
    first_calls = [str(r.url) for r in fake.requests[before:]
                   if r.url.path.endswith("/drive/root/delta") and "$skiptoken" not in str(r.url)]
    assert len(first_calls) == 3
    assert not any("$deltatoken" in u for u in first_calls)


async def test_a_second_run_reads_the_delta_link_and_applies_edits_and_deletions(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    fake = FakeSharePoint.from_fixtures(page_size=3)
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await _ingest(env, acts)
    untouched = _chunks(dsn, PROJECTS)
    cursor = rows(dsn, "SELECT cursor FROM staging.ingest_cursor WHERE scope_key = %s",
                  f"{FIXTURE_GRAPH}|{OPS}")[0][0]
    fake.apply_changes(OPS)
    before = len(fake.requests)
    second = await _ingest(env, acts)
    assert second.status == "succeeded"
    ops_calls = [str(r.url) for r in fake.requests[before:]
                 if r.url.path == f"/v1.0/sites/{OPS}/drive/root/delta"]
    assert ops_calls[0] == cursor, "the persisted delta link, not a full sync"

    ops = next(s for s in SITES if s["site_id"] == OPS)
    edited, gone, added = ops["changes"]
    chunks, docs = _chunks(dsn, OPS), _docs(dsn, OPS)
    edited_ref = mapping.source_ref(OPS, edited["id"])
    assert chunks[edited_ref] == sorted(mapping.chunk(ops["change_content"][edited["id"]]))
    assert "Revised after the October incident review." in "".join(chunks[edited_ref])
    assert docs[edited["id"]][5]["retentionLabel"] == {"name": "Operational - 7 years"}
    # The deletion: its chunks are gone and its document is marked deleted.
    assert mapping.source_ref(OPS, gone["id"]) not in chunks
    assert docs[gone["id"]][4] is not None
    assert docs[gone["id"]][3] == 0
    assert chunks[mapping.source_ref(OPS, added["id"])] == sorted(mapping.chunk(
        ops["change_content"][added["id"]]))
    assert _chunks(dsn, PROJECTS) == untouched
    assert ("retentionLabel", "added") in rows(
        dsn, "SELECT field, drift FROM staging.schema_field WHERE source_system = 'sharepoint'"
             " AND drift IS NOT NULL")
    assert second.counts.records == 3, "only the changes, nothing re-read"


async def test_withdrawn_consent_removes_every_chunk_of_that_site_and_no_other(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    raw, fake = MemoryRawStore(), FakeSharePoint.from_fixtures(page_size=3)
    acts = ConnectorActivities(db, raw, _source(fake))
    await _ingest(env, acts)
    others = {site: _chunks(dsn, site) for site in (PROJECTS, CRL_OPS)}
    assert _chunks(dsn, OPS)
    assert all(others.values())

    fake.revoke(OPS)
    report = await _ingest(env, acts)
    assert {s.scope: s.status for s in report.scopes}[OPS] == "revoked"
    assert report.status == "succeeded", "a revoked site is not a failed run"
    assert _chunks(dsn, OPS) == {}
    assert all(d[4] is not None and d[3] == 0 for d in _docs(dsn, OPS).values())
    assert rows(dsn, "SELECT consent, revoked_at IS NOT NULL FROM cloudops.sharepoint_site"
                     " WHERE site_id = %s", OPS) == [("revoked", True)]
    assert {site: _chunks(dsn, site) for site in others} == others
    assert rows(dsn, "SELECT cursor FROM staging.ingest_cursor WHERE scope_key = %s",
                f"{FIXTURE_GRAPH}|{OPS}") == [("",)], "a re-grant starts with a full sync"

    # A revoked site is no longer read, and replaying its bronze writes nothing.
    before = len(fake.requests)
    third = await _ingest(env, acts)
    assert OPS not in {s.scope for s in third.scopes}
    assert not any(OPS in str(r.url) for r in fake.requests[before:])
    day = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ENTITY, day, day)])
    assert replay.status == "succeeded"
    assert _chunks(dsn, OPS) == {}
    assert {site: _chunks(dsn, site) for site in others} == others


async def test_replay_reprocesses_bronze_without_refetching_or_bringing_back_deletions(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    raw, fake = MemoryRawStore(), FakeSharePoint.from_fixtures(page_size=3)
    acts = ConnectorActivities(db, raw, _source(fake))
    await _ingest(env, acts)
    fake.apply_changes(OPS)
    await _ingest(env, acts)
    snapshot = {s["site_id"]: (_chunks(dsn, s["site_id"]), _docs(dsn, s["site_id"]))
                for s in SITES}
    cursors = rows(dsn, "SELECT scope_key, cursor FROM staging.ingest_cursor ORDER BY 1")
    marks = rows(dsn, "SELECT scope_key, watermark FROM staging.ingest_watermark ORDER BY 1")
    requests = len(fake.requests)
    # Bronze holds both runs: the first sync (with the files as they were) and
    # the changes. Replaying all of it must leave the newest state in place.
    with psycopg.connect(dsn, autocommit=True) as cx:
        cx.execute("DELETE FROM cloudops.knowledge_chunk WHERE starts_with(source_ref, %s)",
                   (mapping.site_prefix(CRL_OPS),))
    day = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ENTITY, day, day)])
    assert replay.status == "succeeded"
    assert len(fake.requests) == requests, "replay never touches Graph"
    for site, (chunks, docs) in snapshot.items():
        assert _chunks(dsn, site) == chunks, site
        assert {k: v[:5] for k, v in _docs(dsn, site).items()} == {
            k: v[:5] for k, v in docs.items()}
    assert rows(dsn, "SELECT scope_key, cursor FROM staging.ingest_cursor ORDER BY 1") == cursors
    assert rows(dsn, "SELECT scope_key, watermark FROM staging.ingest_watermark"
                     " ORDER BY 1") == marks


# --- isolation --------------------------------------------------------------------------------


@pytest.mark.isolation
async def test_one_customers_chunks_and_documents_are_invisible_to_another(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    fake = FakeSharePoint.from_fixtures()
    await _ingest(env, ConnectorActivities(db, MemoryRawStore(), _source(fake)))
    nwl, crl = tenant_id("NWL"), tenant_id("CRL")
    app = Database.create(mapped.app_dsn, pool_size=1, max_overflow=0)
    count = text("SELECT count(*) FROM cloudops.knowledge_chunk WHERE source_kind = 'sharepoint'"
                 " AND starts_with(source_ref, :prefix)")
    try:
        async with app.transaction(TenantScope({crl}, Role.ENGINEER, "test:crl")) as conn:
            assert await conn.scalar(count, {"prefix": mapping.site_prefix(OPS)}) == 0
            assert await conn.scalar(count, {"prefix": mapping.site_prefix(CRL_OPS)}) > 0
            assert await conn.scalar(text(
                "SELECT count(*) FROM cloudops.knowledge_chunk WHERE tenant_id = :t"),
                {"t": nwl}) == 0
            assert await conn.scalar(text(
                "SELECT count(DISTINCT tenant_id) FROM cloudops.document")) == 1
            assert await conn.scalar(text(
                "SELECT count(*) FROM cloudops.sharepoint_site WHERE site_id = :s"),
                {"s": OPS}) == 0
            # Text search inside the query is filtered too: the injection lives
            # only in Northwind's and Contoso's operations sites.
            hits = await conn.scalar(text(
                "SELECT count(DISTINCT tenant_id) FROM cloudops.knowledge_chunk"
                " WHERE body LIKE :q"), {"q": f"%{INJECTION[:40]}%"})
            assert hits == 1
        async with app.transaction(TenantScope({nwl}, Role.ENGINEER, "test:nwl")) as conn:
            assert await conn.scalar(count, {"prefix": mapping.site_prefix(CRL_OPS)}) == 0
            assert await conn.scalar(count, {"prefix": mapping.site_prefix(OPS)}) > 0
        async with app.transaction(TenantScope((), Role.ENGINEER, "test:none")) as conn:
            assert await conn.scalar(text(
                "SELECT count(*) FROM cloudops.knowledge_chunk")) == 0
        # A site is tenant-wide: an account-restricted scope sees no documents.
        async with app.transaction(TenantScope({nwl}, Role.ENGINEER, "test:acct",
                                               account_ids=[uuid.uuid4()])) as conn:
            assert await conn.scalar(text("SELECT count(*) FROM cloudops.document")) == 0
            assert await conn.scalar(text("SELECT count(*) FROM cloudops.sharepoint_site")) == 0
    finally:
        await app.dispose()
