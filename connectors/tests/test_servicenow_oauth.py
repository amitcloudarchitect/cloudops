"""OAuth client credentials and the integration-user guard (ADR 0009), against a
mock instance: tokens are fetched once, refreshed before expiry and on a 401,
read the secret fresh each time, and never leak into an error."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from cloudops_connectors.servicenow.auth import ClientCredentials, TokenError
from cloudops_connectors.servicenow.client import IntegrationUserError, ServiceNowClient

BASE = "https://example.service-now.com"
SECRET = "s3cr3t-value-never-shown"


class Instance:
    """Issues numbered tokens and accepts only the latest one."""

    def __init__(self, *, refuse_token: bool = False, zone: str = "GMT") -> None:
        self.issued = 0
        self.token_forms: list[dict[str, list[str]]] = []
        self.data_auth: list[str] = []
        self.refuse_token = refuse_token
        self.zone = zone
        self.revoked: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth_token.do":
            self.token_forms.append(parse_qs(request.content.decode()))
            if self.refuse_token:
                return httpx.Response(401, json={"error": "server_error",
                                                 "error_description": "access_denied"})
            self.issued += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.issued}",
                                             "token_type": "Bearer", "expires_in": 1799})
        bearer = request.headers.get("authorization", "")
        self.data_auth.append(bearer)
        if bearer != f"Bearer tok-{self.issued}" or bearer in self.revoked:
            return httpx.Response(401, json={"error": {"message": "User is not authenticated"}})
        if request.url.path == "/api/now/table/sys_user":
            return httpx.Response(200, json={"result": [{"user_name": "svc",
                                                         "time_zone": self.zone}]})
        return httpx.Response(200, json={"result": []})


def _client(instance: Instance, clock: list[float], secret: list[str]) -> ServiceNowClient:
    auth = ClientCredentials(f"{BASE}/oauth_token.do", "client-id", lambda: secret[0],
                             clock=lambda: clock[0])
    return ServiceNowClient(BASE, auth=auth, transport=httpx.MockTransport(instance.handler))


async def test_one_token_serves_many_requests_until_near_expiry() -> None:
    instance, clock, secret = Instance(), [1000.0], [SECRET]
    client = _client(instance, clock, secret)
    for _ in range(3):
        await client.fetch("incident", datetime(2026, 10, 1, tzinfo=UTC), 0)
    assert instance.issued == 1
    assert instance.data_auth == ["Bearer tok-1"] * 3
    form = instance.token_forms[0]
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == ["client-id"]
    clock[0] += 1799 - 59  # inside the one-minute margin: refresh first
    await client.fetch("incident", datetime(2026, 10, 1, tzinfo=UTC), 0)
    assert instance.issued == 2
    assert instance.data_auth[-1] == "Bearer tok-2"
    await client.aclose()


async def test_a_rejected_token_is_refreshed_once_and_the_secret_reread() -> None:
    instance, clock, secret = Instance(), [0.0], [SECRET]
    client = _client(instance, clock, secret)
    await client.fetch("incident", datetime(2026, 10, 1, tzinfo=UTC), 0)
    instance.revoked.add("Bearer tok-1")  # revoked server-side, still "fresh" here
    secret[0] = "rotated-secret"  # rotated in Key Vault meanwhile
    await client.fetch("incident", datetime(2026, 10, 1, tzinfo=UTC), 0)
    assert instance.issued == 2
    assert instance.token_forms[-1]["client_secret"] == ["rotated-secret"]
    assert instance.data_auth[-2:] == ["Bearer tok-1", "Bearer tok-2"]
    await client.aclose()


async def test_a_refused_token_names_the_code_and_never_the_secret() -> None:
    client = _client(Instance(refuse_token=True), [0.0], [SECRET])
    with pytest.raises(TokenError) as info:
        await client.fetch("incident", datetime(2026, 10, 1, tzinfo=UTC), 0)
    message = str(info.value)
    assert "HTTP 401" in message
    assert "server_error" in message
    assert SECRET not in message
    assert "client-id" not in message
    await client.aclose()


async def test_a_persistent_401_is_raised_not_looped() -> None:
    instance = Instance()

    def always_401(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth_token.do":
            return instance.handler(request)
        return httpx.Response(401, json={})

    auth = ClientCredentials(f"{BASE}/oauth_token.do", "c", lambda: SECRET)
    client = ServiceNowClient(BASE, auth=auth, transport=httpx.MockTransport(always_401))
    with pytest.raises(httpx.HTTPStatusError) as info:
        await client.fetch("incident", datetime(2026, 10, 1, tzinfo=UTC), 0)
    assert info.value.response.status_code == 401
    assert instance.issued == 2  # the first token and exactly one refresh
    assert "tok-" not in str(info.value)
    await client.aclose()


@pytest.mark.parametrize("zone", ["GMT", "UTC", "Etc/UTC"])
async def test_a_utc_integration_user_is_accepted(zone: str) -> None:
    client = _client(Instance(zone=zone), [0.0], [SECRET])
    assert await client.check_integration_user() == "svc"
    await client.aclose()


@pytest.mark.parametrize("zone", ["", "America/Los_Angeles", "Europe/Amsterdam"])
async def test_any_other_time_zone_is_refused(zone: str) -> None:
    client = _client(Instance(zone=zone), [0.0], [SECRET])
    with pytest.raises(IntegrationUserError, match="GMT"):
        await client.check_integration_user()
    await client.aclose()


async def test_an_ambiguous_user_lookup_is_refused() -> None:
    def two_users(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"result": [
            {"user_name": "a", "time_zone": "GMT"}, {"user_name": "b", "time_zone": "GMT"}]}))

    client = ServiceNowClient(BASE, transport=httpx.MockTransport(two_users))
    with pytest.raises(IntegrationUserError, match="identify"):
        await client.check_integration_user()
    await client.aclose()


def test_the_token_endpoint_must_be_https() -> None:
    with pytest.raises(ValueError, match="https"):
        ClientCredentials("http://example.service-now.com/oauth_token.do", "c", lambda: SECRET)


def test_a_sync_client_cannot_send_requests_without_credentials() -> None:
    auth = ClientCredentials(f"{BASE}/oauth_token.do", "c", lambda: SECRET)
    with httpx.Client(auth=auth, transport=httpx.MockTransport(lambda r: httpx.Response(200))) \
            as client, pytest.raises(RuntimeError, match="async only"):
        client.get(f"{BASE}/api/now/table/incident")


class _Streamed(httpx.AsyncByteStream):
    """A body that has to be read, as over a real connection (MockTransport
    responses are otherwise pre-read, which hid exactly this bug)."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._body


async def test_a_streamed_token_response_is_read_before_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth_token.do":
            body = json.dumps({"access_token": "tok-s", "expires_in": 1799}).encode()
            return httpx.Response(200, stream=_Streamed(body))
        assert request.headers["authorization"] == "Bearer tok-s"
        return httpx.Response(200, json={"result": []})

    auth = ClientCredentials(f"{BASE}/oauth_token.do", "c", lambda: SECRET)
    client = ServiceNowClient(BASE, auth=auth, transport=httpx.MockTransport(handler))
    await client.fetch("incident", datetime(2026, 10, 1, tzinfo=UTC), 0)
    await client.aclose()
