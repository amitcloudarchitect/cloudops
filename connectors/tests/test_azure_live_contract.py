"""Contract tests for the Azure connector's live path (ADR 0023): the workload
identity's Entra token on every request, to the right audience and only to its
own host, against the recorded fixtures with the fakes demanding a bearer
token. No database, no Temporal server, no Azure."""

from __future__ import annotations

import argparse
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from cloudops_config import (
    AzureCostConfig,
    Component,
    ConfigurationError,
    Environment,
    Settings,
    WorkloadIdentity,
)
from cloudops_config.secrets import EnvFileSecretStore
from cloudops_connectors.azure import __main__ as entry
from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.auth import (
    ARM_SCOPE,
    STORAGE_SCOPE,
    EntraAuth,
    ForeignHostError,
    workload_credential,
)
from cloudops_connectors.azure.blob import BlobClient
from cloudops_connectors.azure.client import ARM, AzureClient
from cloudops_connectors.azure.fake import (
    BILLING_DAY,
    BLOB_HOST,
    FIXTURE_HOST,
    FakeAzure,
    FakeBlobStore,
    load_fixture,
)
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core import cli
from cloudops_connectors.core.ingest import ConnectorActivities, FetchRequest
from cloudops_connectors.core.raw import MemoryRawStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)
TOKEN = "eyJ0eXAiOiJKV1QifQ.platform-ingest.sig"


@dataclass
class _Token:
    token: str


@dataclass
class FakeCredential:
    """``WorkloadIdentityCredential``'s async ``get_token``: records the scopes asked."""

    token: str = TOKEN
    scopes: list[tuple[str, ...]] = field(default_factory=list)

    async def get_token(self, *scopes: str) -> _Token:
        self.scopes.append(scopes)
        return _Token(self.token)


async def _no_sleep(_: float) -> None:
    return None


def _live(azure: FakeAzure, blobs: FakeBlobStore, credential: FakeCredential) -> AzureSource:
    """The live path's wiring (``__main__.live_source``) over the fakes."""
    return AzureSource(
        AzureClient(FIXTURE_HOST, auth=EntraAuth(credential, ARM_SCOPE, FIXTURE_HOST),
                    transport=azure.transport(), sleep=_no_sleep),
        exports=BlobClient(BLOB_HOST, auth=EntraAuth(credential, STORAGE_SCOPE, BLOB_HOST),
                           transport=blobs.transport(), sleep=_no_sleep),
        export_roots=blobs.roots, entities=entry.LIVE_ENTITIES,
        today=lambda: date.fromisoformat(BILLING_DAY))


def _fakes() -> tuple[FakeAzure, FakeBlobStore]:
    azure, blobs = FakeAzure.from_fixtures(), FakeBlobStore.from_fixtures()
    azure.bearer = blobs.bearer = TOKEN
    return azure, blobs


async def _run(source: AzureSource, entity: str, scope: str) -> MemoryRawStore:
    raw = MemoryRawStore()
    acts = ConnectorActivities(None, raw, source)  # type: ignore[arg-type]
    env, cursor, page = ActivityEnvironment(), None, 0
    while True:
        result = await env.run(acts.fetch_page, FetchRequest(
            "run-1", entity, scope, T0.isoformat(), cursor, page))
        page += 1
        cursor = result.next_cursor
        if cursor is None:
            return raw


# --- the token ---------------------------------------------------------------------------


async def test_cost_totals_are_read_with_the_management_token_on_every_request() -> None:
    azure, blobs = _fakes()
    credential = FakeCredential()
    raw = await _run(_live(azure, blobs, credential), mapping.COST_TOTAL, "")
    listed = len(json.loads(load_fixture("subscriptions.json"))["value"])
    assert len(azure.requests) == 1 + listed  # the listing, then one query each
    assert {r.headers["Authorization"] for r in azure.requests} == {f"Bearer {TOKEN}"}
    assert set(credential.scopes) == {(ARM_SCOPE,)}
    kinds = [raw.metadata[r]["kind"] for r in raw.pages]
    assert kinds.count("cost_query") == listed
    assert not any(TOKEN.encode() in body for body in raw.pages.values())
    assert not any(TOKEN in json.dumps(m) for m in raw.metadata.values())


@pytest.mark.parametrize("root_index", [0, 1])
async def test_exports_are_read_with_the_storage_token_on_every_request(root_index: int) -> None:
    azure, blobs = _fakes()
    credential = FakeCredential()
    raw = await _run(_live(azure, blobs, credential), mapping.COST_EXPORT,
                     blobs.roots[root_index])
    assert blobs.requests
    assert not azure.requests
    assert {r.headers["Authorization"] for r in blobs.requests} == {f"Bearer {TOKEN}"}
    assert set(credential.scopes) == {(STORAGE_SCOPE,)}
    kinds = [raw.metadata[r]["kind"] for r in raw.pages]
    assert kinds[0] == "listing"
    assert {"manifest", "data"} <= set(kinds)


async def test_without_a_token_the_services_refuse_and_nothing_is_guessed() -> None:
    azure, blobs = _fakes()
    unauthenticated = AzureSource(
        AzureClient(FIXTURE_HOST, transport=azure.transport(), sleep=_no_sleep),
        exports=BlobClient(BLOB_HOST, transport=blobs.transport(), sleep=_no_sleep),
        export_roots=blobs.roots, entities=entry.LIVE_ENTITIES)
    with pytest.raises(httpx.HTTPStatusError) as arm:
        await unauthenticated.fetch(mapping.COST_TOTAL, "", T0, None)
    assert arm.value.response.status_code == 401
    with pytest.raises(httpx.HTTPStatusError) as storage:
        await unauthenticated.fetch(mapping.COST_EXPORT, blobs.roots[0], T0, None)
    assert storage.value.response.status_code == 401


async def test_a_wrong_token_is_refused_by_the_service() -> None:
    azure, blobs = _fakes()
    source = _live(azure, blobs, FakeCredential(token="another"))
    with pytest.raises(httpx.HTTPStatusError, match="401"):
        await source.fetch(mapping.COST_TOTAL, "", T0, None)


@pytest.mark.parametrize("url", [
    "https://evil.example/subscriptions",
    "http://management.azure.fixture.example/subscriptions",
    "https://management.azure.fixture.example:8443/subscriptions",
    "https://exports.blob.fixture.example/cost-exports",
])
async def test_the_token_is_never_sent_to_another_origin(url: str) -> None:
    credential = FakeCredential()
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(auth=EntraAuth(credential, ARM_SCOPE, FIXTURE_HOST),
                                 transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ForeignHostError) as refused:
            await http.get(url)
    assert not sent, "refused before sending"
    assert not credential.scopes, "refused before a token was fetched"
    assert TOKEN not in str(refused.value)


async def test_a_listing_next_link_elsewhere_is_refused_before_the_token_goes() -> None:
    azure, blobs = _fakes()
    credential = FakeCredential()
    source = _live(azure, blobs, credential)
    cursor = json.dumps([{"subs": True, "next": "https://evil.example/subscriptions?x=1"}])
    with pytest.raises(ValueError, match="away from the management endpoint"):
        await source.fetch(mapping.COST_TOTAL, "", T0, cursor)
    assert not azure.requests


def test_entra_auth_is_https_and_a_default_scope_only() -> None:
    with pytest.raises(ValueError, match="https"):
        EntraAuth(FakeCredential(), ARM_SCOPE, "http://management.azure.com")
    with pytest.raises(ValueError, match=r"/\.default"):
        EntraAuth(FakeCredential(), "https://management.azure.com/user_impersonation", ARM)


def test_entra_auth_is_async_only() -> None:
    auth = EntraAuth(FakeCredential(), ARM_SCOPE, FIXTURE_HOST)
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    with (httpx.Client(auth=auth, transport=transport) as http,
          pytest.raises(RuntimeError, match="async only")):
        http.get(f"{FIXTURE_HOST}/subscriptions")


# --- the live source ------------------------------------------------------------------------


@pytest.mark.parametrize("entity", [mapping.ENTITY, mapping.INVOICE])
async def test_the_live_source_refuses_entities_it_does_not_serve(entity: str) -> None:
    azure, blobs = _fakes()
    source = _live(azure, blobs, FakeCredential())
    with pytest.raises(ValueError, match="unknown entity"):
        await source.fetch(entity, "", T0, None)
    assert not azure.requests
    assert not blobs.requests


def test_a_source_narrowed_to_nothing_or_to_strangers_is_refused() -> None:
    with pytest.raises(ValueError, match="entities"):
        AzureSource(AzureClient(FIXTURE_HOST), entities=frozenset())
    with pytest.raises(ValueError, match="entities"):
        AzureSource(AzureClient(FIXTURE_HOST), entities=frozenset({"cost_export", "tickets"}))


def _identity(tmp_path: Path) -> WorkloadIdentity:
    token = tmp_path / "token"
    token.write_text("federated-token", encoding="utf-8")
    return WorkloadIdentity("00000000-0000-0000-0000-000000000001",
                            "00000000-0000-0000-0000-000000000002", token, None)


def _settings(tmp_path: Path, identity: WorkloadIdentity | None) -> Settings:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    cost = AzureCostConfig("https://stcloudopsbronzedevweu.blob.core.windows.net",
                           ("cost-exports-platform/focus/cloudops-platform-dev-0a1b2c3d",))
    return Settings(Environment.AZURE, EnvFileSecretStore.from_file(env_file), identity,
                    {Component.AZURE_COST: cost})


async def test_the_live_source_is_built_on_the_profiles_workload_identity(
    tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        source = entry.live_source(_settings(tmp_path, _identity(tmp_path)), stack)
        assert source.entities == entry.LIVE_ENTITIES
        assert source.instance == ARM
        assert await source.scopes(None, mapping.COST_EXPORT) == [  # type: ignore[arg-type]
            "cost-exports-platform/focus/cloudops-platform-dev-0a1b2c3d"]


def test_the_live_source_needs_a_workload_identity(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="workload identity"):
        entry.live_source(_settings(tmp_path, None), AsyncExitStack())


async def test_the_credential_is_the_validated_identity_not_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    credential = workload_credential(_identity(tmp_path))  # builds without the variables
    await credential.close()


# --- the command line -----------------------------------------------------------------------


def _args(*entity: str) -> argparse.Namespace:
    return argparse.Namespace(entity=list(entity) or None)


_ALL = {e: timedelta(days=1) for e in mapping.ENTITIES}


def test_live_runs_and_schedules_default_to_the_live_entities() -> None:
    assert sorted(cli._entities(_args(), _ALL, entry.LIVE)) == sorted(entry.LIVE_ENTITIES)
    assert cli._entities(_args(mapping.COST_TOTAL), _ALL, entry.LIVE) == [mapping.COST_TOTAL]


@pytest.mark.parametrize("entity", [mapping.ENTITY, mapping.INVOICE])
def test_live_refuses_an_entity_it_does_not_serve(entity: str) -> None:
    with pytest.raises(ConfigurationError, match=entity):
        cli._entities(_args(mapping.COST_EXPORT, entity), _ALL, entry.LIVE)


def test_without_a_live_path_every_entity_stays_available_to_fixtures() -> None:
    assert sorted(cli._entities(_args(), _ALL, None)) == sorted(mapping.ENTITIES)


def test_a_connector_without_a_live_path_still_refuses_to_run_without_fixtures(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, None)
    called: list[Any] = []
    with pytest.raises(cli.NoCredentialPathError):
        cli._fixture_source(settings, False, "qualys", lambda: called.append(1))  # type: ignore[arg-type, return-value]
    assert not called
