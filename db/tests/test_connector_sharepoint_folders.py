"""SharePoint folders granted on their own (ADR 0028), end to end: the generic
connector workflow on a time-skipping Temporal server, the migrated database, and
the recorded library served by the fake Graph through the real client.

Ground truth is the fixture (``cloudops_devdata.graph_fixtures.sharepoint_folders``):
a folder per customer, named as the customer. One resolves exactly, one only
through normalisation, one to nobody.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    ReplayRequest,
    RunReport,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.graph.client import GraphClient
from cloudops_connectors.sharepoint import mapping
from cloudops_connectors.sharepoint.fake import (
    DOWNLOAD,
    FIXTURE_GRAPH,
    FakeSharePoint,
    folder_scope,
    load_folders,
)
from cloudops_connectors.sharepoint.source import SharePointSource
from cloudops_devdata.graph_fixtures import INJECTION, aliases
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

ENTITY = mapping.FOLDER_ENTITY
LIB = load_folders()
DRIVE = LIB["drive_id"]
NWL, CRL, PHARMA = LIB["folders"]
SCOPES = {f["name"]: folder_scope(DRIVE, f["folder"]["id"]) for f in LIB["folders"]}


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
    with psycopg.connect(mapped.admin_dsn, autocommit=True) as cx:
        cx.execute("DELETE FROM cloudops.knowledge_chunk WHERE source_kind = 'sharepoint'")
        for table in ("cloudops.document", "cloudops.sharepoint_folder",
                      "staging.tenant_alias_quarantine"):
            cx.execute(f"DELETE FROM {table}")
        for table in ("staging.ingest_cursor", "staging.ingest_watermark",
                      "staging.schema_field"):
            cx.execute(f"DELETE FROM {table} WHERE source_system = 'sharepoint'")
    database = Database.create(mapped.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _source(fake: FakeSharePoint) -> SharePointSource:
    client = GraphClient(FIXTURE_GRAPH, transport=fake.transport(), sleep=no_sleep)
    return SharePointSource(client, folder_scopes=sorted(SCOPES.values()),
                            entities=frozenset({ENTITY}))


async def _ingest(env: WorkflowEnvironment, acts: ConnectorActivities) -> RunReport:
    (report,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    return report


def _docs(dsn: str, folder: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    return {r[0]: r[1:] for r in rows(
        dsn, "SELECT item_id, tenant_id, name, extraction, chunk_count, deleted_at, site_id"
             " FROM cloudops.document WHERE folder_id = %s", SCOPES[folder["name"]])}


def _chunks(dsn: str, folder: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item, body in rows(dsn, "SELECT d.item_id, c.body FROM cloudops.knowledge_chunk c"
                                " JOIN cloudops.document d ON d.id = c.document_id"
                                " WHERE d.folder_id = %s ORDER BY c.body",
                           SCOPES[folder["name"]]):
        out.setdefault(item, []).append(body)
    return out


def _downloads(fake: FakeSharePoint, since: int = 0) -> list[str]:
    return [r.url.path.rsplit("/", 1)[1] for r in fake.requests[since:]
            if f"{r.url.scheme}://{r.url.host}" == DOWNLOAD]


async def test_each_folder_lands_under_the_tenant_its_name_maps_to(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    raw, fake = MemoryRawStore(), FakeSharePoint.from_fixtures(page_size=3)
    report = await _ingest(env, ConnectorActivities(db, raw, _source(fake)))
    assert report.status == "succeeded"
    # The unmapped folder is not a scope: quarantined by name, nothing of it read.
    assert sorted(s.scope for s in report.scopes) == sorted([SCOPES[NWL["name"]],
                                                             SCOPES[CRL["name"]]])
    assert rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                     " WHERE source_system = 'sharepoint'") == [(PHARMA["name"], 1)]
    # Its name was read; nothing below it was listed or downloaded.
    pharma = PHARMA["folder"]["id"]
    assert [r.url.path for r in fake.requests if pharma in r.url.path] == [
        f"/v1.0/drives/{DRIVE}/items/{pharma}"]
    assert not set(_downloads(fake)) & {i["id"] for i in PHARMA["items"]}
    assert _docs(dsn, PHARMA) == {}

    for folder, code in ((NWL, "NWL"), (CRL, "CRL")):
        docs, chunks = _docs(dsn, folder), _chunks(dsn, folder)
        assert set(docs) == {i["id"] for i in folder["items"]}
        for item in folder["items"]:
            tenant, name, extraction, count, deleted, site = docs[item["id"]]
            assert (tenant, name, deleted, site) == (tenant_id(code), item["name"], None,
                                                     LIB["site_id"])
            if "folder" in item:
                assert (extraction, count) == ("folder", 0)
            elif item["id"] in folder["content"]:
                assert extraction == "text"
                assert chunks[item["id"]] == sorted(mapping.chunk(folder["content"][item["id"]]))
            else:
                assert (extraction, count) == ("unsupported", 0)
        assert rows(dsn, "SELECT tenant_id, name, consent, site_id FROM cloudops.sharepoint_folder"
                         " WHERE item_id = %s", folder["folder"]["id"]) == [
            (tenant_id(code), folder["name"], "granted", LIB["site_id"])]
    # Text is data: stored verbatim, once per mapped folder, nothing acted on it.
    assert rows(dsn, "SELECT count(*) FROM cloudops.knowledge_chunk WHERE body LIKE %s",
                f"%{INJECTION}%") == [(2,)]
    assert raw_first(raw.events)
    # Each folder's tenant is its cursor (for a revocation), with a watermark.
    assert dict(rows(dsn, "SELECT scope_key, cursor FROM staging.ingest_cursor"
                          " WHERE source_system = 'sharepoint'")) == {
        f"{FIXTURE_GRAPH}|{SCOPES[NWL['name']]}": str(tenant_id("NWL")),
        f"{FIXTURE_GRAPH}|{SCOPES[CRL['name']]}": str(tenant_id("CRL"))}


async def test_a_second_run_applies_edits_deletions_and_new_files_and_skips_unchanged_content(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    fake = FakeSharePoint.from_fixtures(page_size=3)
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await _ingest(env, acts)
    untouched = _chunks(dsn, CRL)
    fake.apply_folder_changes(NWL["folder"]["id"])
    before = len(fake.requests)
    second = await _ingest(env, acts)
    assert second.status == "succeeded"
    edited, gone, added = (NWL["changes"][k] for k in ("edit", "delete", "add"))
    # Only the changed versions are downloaded again.
    assert sorted(_downloads(fake, before)) == sorted([edited["id"], added["id"]])
    docs, chunks = _docs(dsn, NWL), _chunks(dsn, NWL)
    assert chunks[edited["id"]] == sorted(mapping.chunk(NWL["change_content"][edited["id"]]))
    assert chunks[added["id"]] == sorted(mapping.chunk(NWL["change_content"][added["id"]]))
    # Missing from the snapshot: deleted, chunks gone.
    assert gone not in chunks
    assert docs[gone][4] is not None
    assert docs[gone][3] == 0
    assert _chunks(dsn, CRL) == untouched


async def test_a_withdrawn_folder_grant_removes_that_folder_and_no_other(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    raw, fake = MemoryRawStore(), FakeSharePoint.from_fixtures(page_size=3)
    acts = ConnectorActivities(db, raw, _source(fake))
    await _ingest(env, acts)
    other = _chunks(dsn, CRL)
    assert _chunks(dsn, NWL)
    assert other

    fake.revoke_folder(NWL["folder"]["id"])
    report = await _ingest(env, acts)
    assert {s.scope: s.status for s in report.scopes}[SCOPES[NWL["name"]]] == "revoked"
    assert report.status == "succeeded", "a revoked folder is not a failed run"
    assert _chunks(dsn, NWL) == {}
    assert all(d[4] is not None and d[3] == 0 for d in _docs(dsn, NWL).values())
    assert rows(dsn, "SELECT consent, revoked_at IS NOT NULL FROM cloudops.sharepoint_folder"
                     " WHERE item_id = %s", NWL["folder"]["id"]) == [("revoked", True)]
    assert _chunks(dsn, CRL) == other

    # Replaying the folder's bronze writes nothing back.
    day = "-".join(next(r for r in raw.pages if r.startswith(f"sharepoint/{ENTITY}/"))
                   .split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ENTITY, day, day)])
    assert replay.status == "succeeded"
    assert _chunks(dsn, NWL) == {}

    # The grant comes back: the folder is read again, from the whole snapshot.
    fake.library.granted.add(NWL["folder"]["id"])  # type: ignore[union-attr]
    again = await _ingest(env, acts)
    assert {s.scope: s.status for s in again.scopes}[SCOPES[NWL["name"]]] == "succeeded"
    assert _chunks(dsn, NWL)
    assert rows(dsn, "SELECT consent FROM cloudops.sharepoint_folder WHERE item_id = %s",
                NWL["folder"]["id"]) == [("granted",)]


async def test_a_renamed_folder_that_maps_to_another_tenant_is_refused_not_moved(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    fake = FakeSharePoint.from_fixtures(page_size=3)
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await _ingest(env, acts)
    before = _docs(dsn, NWL)
    fake.rename_folder(NWL["folder"]["id"], CRL["name"])  # now names another customer
    report = await _ingest(env, acts)
    assert {s.scope: s.status for s in report.scopes}[SCOPES[NWL["name"]]] == "dead_lettered"
    assert _docs(dsn, NWL) == before, "still the first tenant's, untouched"
    assert {r[0] for r in rows(dsn, "SELECT tenant_id FROM cloudops.document"
                                    " WHERE folder_id = %s", SCOPES[NWL["name"]])} == {
        tenant_id("NWL")}
