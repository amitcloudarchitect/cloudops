"""Contract tests for folder snapshots (ADR 0028) against the recorded library: the
walk, the envelope bronze stores, the snapshot limits, withdrawn grants, content
read once per version, and the paths. No database, no Temporal server."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from cloudops_connectors.core.ingest import ScopeRevokedError
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.graph.client import GraphClient, GraphError
from cloudops_connectors.sharepoint import folders, mapping
from cloudops_connectors.sharepoint.client import children_path, item_content_path, item_path
from cloudops_connectors.sharepoint.fake import (
    DOWNLOAD,
    FIXTURE_GRAPH,
    FakeSharePoint,
    folder_scope,
    load_folders,
)
from cloudops_connectors.sharepoint.source import SharePointSource, folder_content_ref
from cloudops_devdata.graph_fixtures import build

T0 = datetime(2026, 1, 1, tzinfo=UTC)
LIB = load_folders()
DRIVE = LIB["drive_id"]
NWL = LIB["folders"][0]
SCOPE = folder_scope(DRIVE, NWL["folder"]["id"])


async def _no_sleep(_: float) -> None:
    return None


def _client(fake: FakeSharePoint) -> GraphClient:
    return GraphClient(FIXTURE_GRAPH, transport=fake.transport(), sleep=_no_sleep)


def _source(fake: FakeSharePoint) -> SharePointSource:
    return SharePointSource(_client(fake), folder_scopes=[SCOPE],
                            entities=frozenset({mapping.FOLDER_ENTITY}))


def test_the_fixture_is_what_the_generator_builds() -> None:
    from importlib import resources

    committed = resources.files("cloudops_connectors.sharepoint").joinpath(
        "fixtures/folders.json").read_bytes()
    assert committed == build()["sharepoint/fixtures/folders.json"]


def test_paths_address_a_drive_item() -> None:
    assert item_path("b!a-b_c", "01ABC") == "/v1.0/drives/b!a-b_c/items/01ABC"
    assert children_path("b!x", "01A") == "/v1.0/drives/b!x/items/01A/children?$top=200"
    assert item_content_path("b!x", "01A") == "/v1.0/drives/b!x/items/01A/content"
    assert item_path("b!x", "../../sites") == "/v1.0/drives/b!x/items/..%2F..%2Fsites"


@pytest.mark.parametrize("scope", ["", "drive", "/item", "drive/", "a/b/c"])
def test_a_folder_scope_is_a_drive_and_an_item(scope: str) -> None:
    with pytest.raises(ValueError, match="drive id"):
        folders.split_scope(scope)


async def test_the_walk_reads_every_level_and_keeps_every_body_verbatim() -> None:
    fake = FakeSharePoint.from_fixtures(page_size=2)
    body = await folders.walk(_client(fake), DRIVE, NWL["folder"]["id"])
    envelope = json.loads(body)
    snap = folders.parse(body)
    assert snap.folder["name"] == NWL["name"]
    assert snap.site_id == LIB["site_id"]
    assert sorted(i["id"] for i in snap.items) == sorted(i["id"] for i in NWL["items"])
    # Pages of two: the top folder's three children took two, its nextLink followed.
    assert sum("skiptoken" in r["path"] for r in envelope["responses"]) == 1
    for r in envelope["responses"]:
        assert isinstance(r["body"], str)
        json.loads(r["body"])  # each is Graph's own JSON text, unparsed in the envelope
    # Only the folder and what is below it were asked for.
    assert all(NWL["folder"]["id"] in r.url.path or any(
        i["id"] in r.url.path for i in NWL["items"]) for r in fake.requests)


async def test_a_folder_too_large_or_too_deep_is_refused_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSharePoint.from_fixtures()
    monkeypatch.setattr(folders, "MAX_ITEMS", 3)
    with pytest.raises(folders.SnapshotTooLargeError, match="more than 3"):
        await folders.walk(_client(fake), DRIVE, NWL["folder"]["id"])
    monkeypatch.setattr(folders, "MAX_ITEMS", 5000)
    monkeypatch.setattr(folders, "MAX_DEPTH", 0)
    with pytest.raises(folders.SnapshotTooLargeError, match="deeper"):
        await folders.walk(_client(fake), DRIVE, NWL["folder"]["id"])


def test_a_page_that_is_not_a_snapshot_is_refused() -> None:
    for body in (b"{}", b"[]", b'{"drive_id": "d", "folder_id": "f", "responses": []}',
                 json.dumps({"drive_id": "d", "folder_id": "f",
                             "responses": [{"path": "/", "body": '{"id": "other"}'}]}).encode()):
        with pytest.raises(folders.MalformedSnapshotError):
            folders.parse(body)


async def test_the_library_answers_only_inside_a_granted_folder() -> None:
    fake = FakeSharePoint.from_fixtures()
    client = _client(fake)
    runbooks = NWL["items"][0]
    assert json.loads(await client.get(item_path(DRIVE, runbooks["id"])))["name"] == "Runbooks"
    fake.revoke_folder(NWL["folder"]["id"])
    for path in (item_path(DRIVE, runbooks["id"]), children_path(DRIVE, runbooks["id"])):
        with pytest.raises(GraphError) as denied:
            await client.get(path)
        assert (denied.value.status, denied.value.code) == (403, "accessDenied")


@pytest.mark.parametrize("withdraw", ["revoke", "delete"])
async def test_a_withdrawn_or_deleted_folder_is_a_revoked_scope(withdraw: str) -> None:
    fake = FakeSharePoint.from_fixtures()
    if withdraw == "revoke":
        fake.revoke_folder(NWL["folder"]["id"])
    else:
        del fake.library.items[NWL["folder"]["id"]]  # type: ignore[union-attr]
    with pytest.raises(ScopeRevokedError):
        await _source(fake).fetch(mapping.FOLDER_ENTITY, SCOPE, T0, None)


async def test_content_is_downloaded_once_per_version_and_stored_by_version() -> None:
    fake = FakeSharePoint.from_fixtures()
    source, raw = _source(fake), MemoryRawStore()
    fetched = await source.fetch(mapping.FOLDER_ENTITY, SCOPE, T0, None)
    await raw.put("sharepoint/folder_item/2026/01/01/r1-00000.json", fetched.body, {})
    info = await source.inspect(raw, mapping.FOLDER_ENTITY, SCOPE,
                                "sharepoint/folder_item/2026/01/01/r1-00000.json",
                                fetched.body, None)
    assert info.next_cursor is None, "a snapshot is one page"
    assert info.records == len(NWL["items"])
    downloads = [r for r in fake.requests if f"{r.url.scheme}://{r.url.host}" == DOWNLOAD]
    assert len(downloads) == len(NWL["content"])
    for item in NWL["items"]:
        if item["id"] in NWL["content"]:
            ref = folder_content_ref(SCOPE, item["id"], mapping.version_key(item))
            assert await raw.get(ref) == NWL["content"][item["id"]].encode()
            assert not ref.startswith("sharepoint/folder_item/"), "never listed as a page"

    before = len(fake.requests)
    await source.inspect(raw, mapping.FOLDER_ENTITY, SCOPE,
                         "sharepoint/folder_item/2026/01/02/r2-00000.json", fetched.body, None)
    assert not [r for r in fake.requests[before:]
                if f"{r.url.scheme}://{r.url.host}" == DOWNLOAD], "nothing changed, nothing read"
