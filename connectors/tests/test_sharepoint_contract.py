"""Contract tests for the SharePoint connector and the shared Graph client against
recorded fixtures. No database, no Temporal server: mapping, chunking, delta
paging, Retry-After, consent withdrawal, bronze ordering, and the fixtures."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    FetchRequest,
    ScopeRevokedError,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.graph.client import (
    ForeignLinkError,
    GraphClient,
    GraphError,
    GraphThrottledError,
    retry_after,
    retry_later,
)
from cloudops_connectors.sharepoint import mapping
from cloudops_connectors.sharepoint.client import content_path, delta_path
from cloudops_connectors.sharepoint.fake import FIXTURE_GRAPH, FakeSharePoint, load_fixture
from cloudops_connectors.sharepoint.source import CONTENT_SUFFIX, SharePointSource, content_ref
from cloudops_devdata.graph_fixtures import INJECTION, ROOT, build

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SITES = load_fixture()["sites"]
OPS = SITES[0]["site_id"]


class _Clock:
    """A clock that sleeping advances, so waits are measured, not slept."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def _client(fake: FakeSharePoint, clock: _Clock | None = None, **kwargs: float) -> GraphClient:
    clock = clock or _Clock()
    return GraphClient(FIXTURE_GRAPH, transport=fake.transport(), sleep=clock.sleep,
                       clock=clock, **kwargs)  # type: ignore[arg-type]


def _item(**fields: object) -> dict[str, object]:
    return {"id": "01ABC", "name": "runbook.md", "eTag": '"{X},1"', "cTag": '"c:{X},1"',
            "size": 120,
            "lastModifiedDateTime": "2026-10-01T10:00:00Z", "webUrl": "https://x/runbook.md",
            "file": {"mimeType": "text/markdown", "hashes": {"quickXorHash": "abc="}},
            "parentReference": {"driveId": "b!x"}, **fields}


async def _drain(client: GraphClient, url: str) -> tuple[list[dict[str, object]], str]:
    items: list[dict[str, object]] = []
    while True:
        body = await client.get(url)
        items.extend(mapping.items(body))
        nxt = mapping.next_link(body)
        if nxt is None:
            link = mapping.delta_link(body)
            assert link is not None
            return items, link
        url = nxt


# --- mapping ---------------------------------------------------------------------------


def test_a_text_file_maps_and_keeps_unmapped_fields_in_raw_extra() -> None:
    item = mapping.normalise(_item())
    assert (item.item_id, item.name, item.extraction) == ("01ABC", "runbook.md", "text")
    assert item.modified_at == datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert (item.mime_type, item.size, item.etag) == ("text/markdown", 120, '"{X},1"')
    assert item.wants_content
    assert not item.deleted
    assert item.raw_extra == {"cTag": '"c:{X},1"', "parentReference": {"driveId": "b!x"},
                              "file": {"hashes": {"quickXorHash": "abc="}}}


@pytest.mark.parametrize(
    ("fields", "extraction"),
    [({"file": {"mimeType": "application/pdf"}}, "unsupported"),
     ({"file": {"mimeType": "text/plain"}, "size": mapping.MAX_TEXT_BYTES + 1}, "unsupported"),
     ({"file": None, "folder": {"childCount": 2}}, "folder"),
     ({"file": None, "root": {}, "folder": {}}, "folder")],
)
def test_only_small_text_and_markdown_are_extracted(
    fields: dict[str, object], extraction: str
) -> None:
    assert mapping.normalise(_item(**fields)).extraction == extraction


def test_a_tombstone_needs_only_its_id() -> None:
    item = mapping.normalise({"id": "01DEL", "deleted": {"state": "deleted"}})
    assert item.deleted
    assert not item.wants_content


@pytest.mark.parametrize("broken", [{"id": None}, {"id": ""}, {"name": None},
                                    {"lastModifiedDateTime": "2026-10-01T10:00:00"},
                                    {"lastModifiedDateTime": "last week"}])
def test_an_item_without_its_keys_is_malformed(broken: dict[str, object]) -> None:
    with pytest.raises(mapping.MalformedRecordError):
        mapping.normalise(_item(**broken))


def test_chunks_follow_paragraphs_never_exceed_the_limit_and_keep_the_text() -> None:
    paragraphs = [f"Paragraph {n}: " + "word " * (n * 40) for n in range(1, 12)]
    text = "\r\n\r\n".join(paragraphs)
    chunks = mapping.chunk(text, limit=500)
    assert chunks == mapping.chunk(text, limit=500), "deterministic"
    assert all(0 < len(c) <= 500 for c in chunks)
    assert len(chunks) > 3
    assert " ".join(" ".join(chunks).split()) == " ".join(" ".join(paragraphs).split())
    assert mapping.chunk("one\n\ntwo") == ["one\n\ntwo"], "small paragraphs share a chunk"
    assert mapping.chunk("") == []
    assert mapping.chunk("\n\n  \n") == []
    assert mapping.chunk("x" * 1200, limit=500) == ["x" * 500, "x" * 500, "x" * 200]


def test_hostile_text_is_kept_verbatim() -> None:
    assert mapping.chunk(INJECTION) == [INJECTION]


def test_binary_or_non_utf8_content_is_not_text() -> None:
    assert mapping.decode(b"\xef\xbb\xbfhello") == "hello"
    assert mapping.decode(b"\xff\xfe\x00h") is None
    assert mapping.decode(b"abc\x00def") is None


# --- the fixtures ----------------------------------------------------------------------


def test_committed_fixtures_equal_the_generator() -> None:
    for name, body in build().items():
        assert (ROOT / name).read_bytes() == body, name


def test_the_fixtures_cover_every_case_the_connector_must_handle() -> None:
    items = [i for s in SITES for i in s["delta"]]
    changes = [i for s in SITES for i in s["changes"]]
    assert any("id" not in i for i in items), "a malformed record"
    assert any("folder" in i and "root" not in i for i in items)
    assert any("root" in i for i in items)
    assert any(i.get("file", {}).get("mimeType") not in mapping.TEXT_MIME
               for i in items if "file" in i), "an unsupported file"
    assert any("deleted" in i for i in changes), "a deletion in the second delta"
    assert any(set(i) - mapping.MAPPED - set(items[3]) for i in changes), "a new field"
    assert any(INJECTION in t for s in SITES for t in s["content"].values())
    assert len({s["code"] for s in SITES}) == 2
    assert len(SITES) == 3


# --- the Graph client: paging, limits, links -----------------------------------------------


async def test_a_first_delta_pages_to_a_delta_link_then_reports_only_changes() -> None:
    fake = FakeSharePoint.from_fixtures(page_size=2)
    client = _client(fake)
    items, link = await _drain(client, delta_path(OPS))
    assert len(items) == len(SITES[0]["delta"])
    assert len(fake.requests) > 2, "paged by nextLink"
    assert all(r.url.host == "graph.fixture.example" for r in fake.requests)
    again, _ = await _drain(client, link)
    assert again == [], "nothing changed"
    fake.apply_changes(OPS)
    changed, _ = await _drain(client, link)
    assert [i["id"] for i in changed] == [i["id"] for i in SITES[0]["changes"]]
    assert any("deleted" in i for i in changed)


async def test_429_waits_exactly_what_retry_after_asks_then_succeeds() -> None:
    fake = FakeSharePoint.from_fixtures()
    fake.throttle.next, fake.throttle.seconds = 2, "17"
    clock = _Clock()
    body = await _client(fake, clock).get(delta_path(OPS))
    assert mapping.items(body)
    assert clock.waits == [17.0, 17.0]


async def test_a_retry_after_is_never_shortened_and_holds_every_request() -> None:
    """A wait longer than the client sleeps is handed to Temporal whole, and no
    request leaves the client before it has elapsed."""
    fake = FakeSharePoint.from_fixtures()
    fake.throttle.next, fake.throttle.seconds = 1, "900"
    clock = _Clock()
    client = _client(fake, clock)
    with pytest.raises(GraphThrottledError) as caught:
        await client.get(delta_path(OPS))
    assert caught.value.retry_after == 900.0
    assert clock.waits == []
    delayed = retry_later(caught.value)
    assert isinstance(delayed, ApplicationError)
    assert delayed.next_retry_delay == timedelta(seconds=900)
    assert delayed.type == "GraphThrottledError"
    assert not delayed.non_retryable
    # Another request through this client waits out the remainder first.
    clock.now += 100
    await client.get(delta_path(OPS))
    assert clock.waits == [800.0]


async def test_persistent_throttling_raises() -> None:
    fake = FakeSharePoint.from_fixtures()
    fake.throttle.next = 99
    with pytest.raises(GraphThrottledError):
        await _client(fake, max_throttle_retries=2).get(delta_path(OPS))
    assert len(fake.requests) == 3


def test_retry_after_reads_seconds_and_http_dates() -> None:
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    later = format_datetime(now + timedelta(seconds=42), usegmt=True)
    assert retry_after(httpx.Headers({"Retry-After": "5"}), now.timestamp()) == 5.0
    assert retry_after(httpx.Headers({"Retry-After": later}), now.timestamp()) == 42.0
    assert retry_after(httpx.Headers({"Retry-After": "soon"}), 0) is None
    assert retry_after(httpx.Headers({}), 0) is None


async def test_paging_links_off_the_graph_origin_are_refused() -> None:
    client = _client(FakeSharePoint.from_fixtures())
    with pytest.raises(ForeignLinkError):
        await client.get("https://evil.example/v1.0/sites/x/drive/root/delta?$skiptoken=1")
    with pytest.raises(ForeignLinkError):
        await client.get("http://graph.fixture.example/v1.0/x")


async def test_no_more_than_two_requests_are_in_flight() -> None:
    inflight, peak = 0, 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return httpx.Response(200, json={"value": []})

    client = GraphClient(FIXTURE_GRAPH, transport=httpx.MockTransport(handler))
    await asyncio.gather(*(client.get(f"/v1.0/x{n}") for n in range(8)))
    assert peak == 2


async def test_errors_carry_graphs_code_never_its_message() -> None:
    fake = FakeSharePoint.from_fixtures()
    fake.revoke(OPS)
    with pytest.raises(GraphError) as caught:
        await _client(fake).get(delta_path(OPS))
    assert (caught.value.status, caught.value.code) == (403, "accessDenied")
    assert "Access denied" not in str(caught.value)


async def test_content_follows_the_redirect_without_our_credentials() -> None:
    fake = FakeSharePoint.from_fixtures()
    iid, text = next(iter(SITES[0]["content"].items()))
    client = GraphClient(FIXTURE_GRAPH, transport=fake.transport(),
                         auth=httpx.BasicAuth("fixture", "not-a-secret"))
    assert await client.download(content_path(OPS, iid)) == text.encode()
    graph, download = fake.requests[-2:]
    assert graph.headers.get("Authorization")
    assert graph.url.host == "graph.fixture.example"
    assert download.url.host == "download.fixture.example"
    assert "Authorization" not in download.headers


# --- the source: consent, resync, raw before parse ------------------------------------------


def _source(fake: FakeSharePoint, page_size: int | None = None) -> SharePointSource:
    if page_size:
        fake.page_size = page_size
    return SharePointSource(_client(fake), [s["customer"] for s in SITES])


@pytest.mark.parametrize("missing", [False, True])
async def test_a_withdrawn_or_missing_site_is_a_revoked_scope(missing: bool) -> None:
    fake = FakeSharePoint.from_fixtures()
    if missing:
        del fake.sites[OPS]
    else:
        fake.revoke(OPS)
    with pytest.raises(ScopeRevokedError):
        await _source(fake).fetch(mapping.ENTITY, OPS, T0, None)


async def test_an_expired_delta_token_resyncs_from_the_start() -> None:
    fake = FakeSharePoint.from_fixtures(page_size=100)
    source = _source(fake)
    first = await source.fetch(mapping.ENTITY, OPS, T0, None)
    link = mapping.delta_link(first.body)
    fake.expire_tokens = True
    again = await source.fetch(mapping.ENTITY, OPS, T0, link)
    assert mapping.items(again.body) == mapping.items(first.body)
    assert "$deltatoken" not in str(fake.requests[-1].url)


async def test_throttling_inside_an_activity_delays_temporals_retry() -> None:
    fake = FakeSharePoint.from_fixtures()
    fake.throttle.next, fake.throttle.seconds = 1, "3600"
    with pytest.raises(ApplicationError) as caught:
        await _source(fake).fetch(mapping.ENTITY, OPS, T0, None)
    assert caught.value.next_retry_delay == timedelta(hours=1)


async def test_the_page_is_in_bronze_before_its_content_is_fetched() -> None:
    fake = FakeSharePoint.from_fixtures(page_size=100)
    raw = MemoryRawStore()
    acts = ConnectorActivities(None, raw, _source(fake))  # type: ignore[arg-type]
    result = await ActivityEnvironment().run(acts.fetch_page, FetchRequest(
        "run-1", mapping.ENTITY, OPS, T0.isoformat(), None, 0))
    texts = SITES[0]["content"]
    refs = [content_ref(result.raw_ref, i) for i in texts]
    assert raw.events[0] == ("put", result.raw_ref)
    assert sorted(r for _, r in raw.events[1:]) == sorted(refs)
    assert all(r.endswith(CONTENT_SUFFIX) and r.startswith(result.raw_ref[:-5]) for r in refs)
    assert fake.requests[0].url.path.endswith("/drive/root/delta")
    for iid, body in texts.items():
        assert raw.pages[content_ref(result.raw_ref, iid)] == body.encode()
        meta = raw.metadata[content_ref(result.raw_ref, iid)]
        assert meta["page_ref"] == result.raw_ref
        assert meta["status"] == "200"
    # Unsupported files and folders are never downloaded.
    downloaded = {r.url.path.rsplit("/", 1)[1] for r in fake.requests
                  if r.url.host == "download.fixture.example"}
    assert downloaded == set(texts)
    assert raw.metadata[result.raw_ref]["scope"] == OPS
    assert result.next_cursor is None
    assert result.records == len(SITES[0]["delta"])
    page = json.loads(raw.pages[result.raw_ref])
    assert page["@odata.deltaLink"]


async def test_a_file_gone_before_its_download_is_recorded_not_fatal() -> None:
    fake = FakeSharePoint.from_fixtures(page_size=100)
    raw = MemoryRawStore()
    source = _source(fake)
    fetched = await source.fetch(mapping.ENTITY, OPS, T0, None)
    iid = next(iter(SITES[0]["content"]))
    fake.delete(OPS, iid)
    await raw.put("p.json", fetched.body, {})
    await source.inspect(raw, mapping.ENTITY, OPS, "p.json", fetched.body, None)
    assert raw.metadata[content_ref("p.json", iid)]["status"] == "404"


async def test_consent_withdrawn_mid_page_is_a_revoked_scope() -> None:
    fake = FakeSharePoint.from_fixtures(page_size=100)
    source = _source(fake)
    fetched = await source.fetch(mapping.ENTITY, OPS, T0, None)
    fake.revoke(OPS)
    with pytest.raises(ScopeRevokedError):
        await source.inspect(MemoryRawStore(), mapping.ENTITY, OPS, "p.json", fetched.body,
                             None)


def test_content_refs_are_deterministic_and_safe() -> None:
    assert content_ref("a/b/run-00001.json", "01ABC") == "a/b/run-00001.01ABC.content"
    odd = content_ref("a/run.json", "../../etc/passwd")
    assert odd == content_ref("a/run.json", "../../etc/passwd")
    assert "/" not in odd.removeprefix("a/")
