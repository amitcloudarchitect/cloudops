"""Contract tests for the CoreStack connector (ADR 0028) against recorded fixtures:
the request allowlist, the token, paging, the snapshot envelope, and the mapping.
No database, no Temporal server."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from cloudops_connectors.corestack import mapping
from cloudops_connectors.corestack.client import (
    ALLOWED,
    CoreStackClient,
    RefusedRequestError,
    TokenError,
    allowed,
)
from cloudops_connectors.corestack.fake import (
    FIXTURE_ACCESS_KEY,
    FIXTURE_HOST,
    FIXTURE_SECRET,
    FakeCoreStack,
    load_fixture,
)
from cloudops_devdata.corestack_fixtures import INJECTION, build

T0 = datetime(2026, 1, 1, tzinfo=UTC)
FIXTURE = load_fixture()
NWL = FIXTURE["tenants"][0]["id"]


async def _no_sleep(_: float) -> None:
    return None


def _client(fake: FakeCoreStack, *, secret: str = FIXTURE_SECRET, page_size: int = 4,
            reads: list[int] | None = None) -> CoreStackClient:
    def read() -> str:
        if reads is not None:
            reads.append(1)
        return secret

    return CoreStackClient(FIXTURE_HOST, lambda: FIXTURE_ACCESS_KEY, read,
                           transport=fake.transport(), page_size=page_size, min_interval=0.0,
                           sleep=_no_sleep)


def test_the_fixture_is_what_the_generator_builds() -> None:
    from importlib import resources

    committed = resources.files("cloudops_connectors.corestack").joinpath(
        "fixtures/recommendations.json").read_bytes()
    assert committed == build()[0]


@pytest.mark.parametrize(("method", "path", "ok"), [
    ("POST", "/v1/auth/tokens", True),
    ("GET", "/recommendations/69b6555c5a21a9cc93915990/service_account/list", True),
    ("GET", "/recommendations/69b6555c5a21a9cc93915990/get/6ae276811b26e8ecd72a3280", True),
    ("GET", "/v1/auth/tokens", False),
    ("POST", "/recommendations/t/service_account/list", False),
    ("DELETE", "/recommendations/t/get/r", False),
    ("PUT", "/recommendations/t/get/r", False),
    ("POST", "/recommendations/t/get/r/remediate", False),
    ("GET", "/recommendations/t/get/r/../../users", False),
    ("GET", "/v1/users", False),
])
def test_only_the_token_and_two_reads_are_allowed(method: str, path: str, ok: bool) -> None:
    assert allowed(method, path) is ok
    assert len(ALLOWED) == 3


async def test_a_request_outside_the_allowlist_is_never_sent() -> None:
    fake = FakeCoreStack.from_fixtures()
    client = _client(fake)
    with pytest.raises(RefusedRequestError):
        await client._send("DELETE", f"/recommendations/{NWL}/get/x")
    with pytest.raises(RefusedRequestError):
        await client.recommendation(NWL, "../../admin", 1)
    assert fake.requests == []


async def test_one_token_is_reused_and_a_401_fetches_one_new_one() -> None:
    fake, reads = FakeCoreStack.from_fixtures(), list[int]()
    client = _client(fake, reads=reads)
    await client.recommendations(NWL, 1)
    await client.recommendations(NWL, 2)
    assert reads == [1], "the secret is read for a token request, not for every call"
    fake.expire_tokens()
    await client.recommendations(NWL, 1)
    assert reads == [1, 1]
    token_posts = [r for r in fake.requests if r.method == "POST"]
    assert len(token_posts) == 2
    for r in fake.requests:
        if r.method == "GET":
            assert r.headers["X-Auth-Token"]
            assert FIXTURE_SECRET not in str(r.url)
            assert "secret" not in str(r.headers).lower()


async def test_a_refused_token_names_no_credential() -> None:
    client = _client(FakeCoreStack.from_fixtures(), secret="wrong-secret-value")
    with pytest.raises(TokenError) as refused:
        await client.recommendations(NWL, 1)
    assert "wrong-secret-value" not in str(refused.value)
    assert FIXTURE_ACCESS_KEY not in str(refused.value)


async def test_a_429_waits_and_tries_again() -> None:
    fake = FakeCoreStack.from_fixtures()
    fake.throttle_next = 2
    body = await _client(fake).recommendations(NWL, 1)
    assert json.loads(body)["recommendations"]


async def test_the_snapshot_holds_every_page_and_no_token() -> None:
    fake = FakeCoreStack.from_fixtures()
    body = await mapping.snapshot(_client(fake), NWL, T0)
    envelope = json.loads(body)
    snap = mapping.parse(body)
    want = FIXTURE["recommendations"][NWL]
    assert [r["id"] for r in snap.recommendations] == [r["id"] for r in want]
    for rec, recorded in zip(snap.recommendations, want, strict=True):
        assert rec["resources"] == recorded["detail"]["resources"]
        assert rec["description"] == recorded["detail"]["description"]
        assert rec["tenant_name"] == recorded["tenant_name"]
    assert snap.fetched_at == T0
    assert sum(r["kind"] == "list" for r in envelope["responses"]) == -(-len(want) // 4)
    many = next(r for r in want if len(r["detail"]["resources"]) > 4)
    assert sum(r["path"].endswith(many["id"]) for r in envelope["responses"]) == 2
    for token in fake.tokens:
        assert token.encode() not in body
    assert FIXTURE_SECRET.encode() not in body


async def test_a_server_ignoring_the_page_number_is_read_once() -> None:
    fake = FakeCoreStack.from_fixtures()

    def same_page(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return fake._handle(request)
        items = [{"id": "a", "tenant_name": "x"}]
        return httpx.Response(200, json={"page_count": 9, "recommendations": items,
                                         "resources": []})

    client = CoreStackClient(FIXTURE_HOST, lambda: FIXTURE_ACCESS_KEY, lambda: FIXTURE_SECRET,
                             transport=httpx.MockTransport(same_page), min_interval=0.0)
    snap = mapping.parse(await mapping.snapshot(client, NWL, T0))
    assert [r["id"] for r in snap.recommendations] == ["a"]


async def test_a_snapshot_too_large_is_refused_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mapping, "MAX_REQUESTS", 3)
    with pytest.raises(mapping.SnapshotTooLargeError):
        await mapping.snapshot(_client(FakeCoreStack.from_fixtures()), NWL, T0)


def _rec(**over: Any) -> dict[str, Any]:
    rec = {
        "id": "rec1", "tenant_id": "t", "tenant_name": "Contoso", "classification": "Security",
        "sub_classification": "Standards", "impact": "high", "status": "open",
        "name": "Storage should restrict network access", "policy_name": "Storage rules",
        "source": "policy", "created_at": "2026-10-28 22:26:09.822000",
        "cloud_account_id": "69b664d0209f1144512cc482", "description": INJECTION,
        "resources": [
            {"id": "r1", "resource_id": "/subscriptions/3F6B2D1E-8C4A-4E7B-9D2F-6A1C5E8B7D40/"
             "resourcegroups/rg/providers/microsoft.storage/storageaccounts/st1",
             "resource_name": "st1", "resource": "Storage_Accounts", "status": "open",
             "location": "westeurope", "resourcegroup": "rg"},
            {"id": "r2", "resource_id": "arn:aws:s3:eu-west-1:123456789012:bucket/b",
             "resource_name": "b", "resource": "S3", "status": "resolved"},
        ],
    }
    rec.update(over)
    return rec


def test_a_recommendation_is_one_finding_per_resource() -> None:
    first, second = mapping.findings(_rec())
    assert (first.finding_id, second.finding_id) == ("rec1:r1", "rec1:r2")
    assert first.table == "security"
    assert first.category == "security"
    assert (first.provider, first.account_native_id) == (
        "azure", "3f6b2d1e-8c4a-4e7b-9d2f-6a1c5e8b7d40")
    assert (second.provider, second.account_native_id) == ("aws", "123456789012")
    assert (first.status, second.status) == ("open", "resolved")
    assert first.detected_at == datetime(2026, 10, 28, 22, 26, 9, 822000, tzinfo=UTC)
    assert first.severity == "high"
    # Unmapped fields kept; the description stays data.
    assert first.raw_extra["cloud_account_id"] == "69b664d0209f1144512cc482"
    assert first.raw_extra["description"] == INJECTION
    assert first.raw_extra["resource_location"] == "westeurope"
    (lone,) = mapping.findings(_rec(resources=[]))
    assert (lone.finding_id, lone.account_native_id) == ("rec1", None)


@pytest.mark.parametrize(("classification", "table", "category"), [
    ("Cost", "cost", "standards"), ("Security", "security", "security"),
    ("Compliance", "security", "compliance"), ("Operation", None, None),
])
def test_where_a_recommendation_goes(
    classification: str, table: str | None, category: str | None
) -> None:
    found = mapping.findings(_rec(classification=classification))
    if table is None:
        assert found == []
    else:
        assert {(f.table, f.category) for f in found} == {(table, category)}


@pytest.mark.parametrize("over", [
    {"impact": "critical"}, {"status": "snoozed"}, {"id": None}, {"tenant_name": " "},
    {"created_at": "yesterday"}, {"resources": [{"resource_id": "/subscriptions/x"}]},
])
def test_what_corestack_does_not_send_is_rejected_not_guessed(over: dict[str, Any]) -> None:
    with pytest.raises(mapping.MalformedRecordError):
        mapping.findings(_rec(**over))


def test_a_dismissed_recommendation_is_dismissed_for_every_resource() -> None:
    assert {f.status for f in mapping.findings(_rec(status="dismissed"))} == {"dismissed"}
