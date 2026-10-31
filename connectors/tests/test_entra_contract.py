"""Contract tests for the Entra ID connector against recorded fixtures. No
database, no Temporal server: mapping, secrets kept out, delta and snapshot
paging per directory, and the fixtures themselves."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from temporalio.exceptions import ApplicationError

from cloudops_connectors.entra import mapping
from cloudops_connectors.entra.client import ROLE_ASSIGNMENTS, USERS_DELTA
from cloudops_connectors.entra.fake import FIXTURE_GRAPH, FakeEntra, load_fixture
from cloudops_connectors.entra.source import EntraSource
from cloudops_connectors.graph.client import GraphClient
from cloudops_devdata.graph_fixtures import ROOT, UNKNOWN_DIRECTORY_NAME, build

T0 = datetime(2026, 1, 1, tzinfo=UTC)
DIRS = load_fixture()["directories"]
NWL = DIRS[0]["directory_id"]


async def _no_sleep(_: float) -> None:
    return None


def _source(fake: FakeEntra) -> EntraSource:
    return EntraSource({d: GraphClient(FIXTURE_GRAPH, transport=fake.transport(d),
                                       sleep=_no_sleep) for d in fake.directories})


async def _pages(source: EntraSource, entity: str, scope: str,
                 cursor: str | None = None) -> list[bytes]:
    out = []
    while True:
        body = (await source.fetch(entity, scope, T0, cursor)).body
        out.append(body)
        info = await source.inspect(None, entity, scope, "r", body, cursor)  # type: ignore[arg-type]
        if info.next_cursor is None:
            return out
        cursor = info.next_cursor


# --- mapping ---------------------------------------------------------------------------


def test_a_user_maps_minimally_and_keeps_the_rest_in_raw_extra() -> None:
    u = mapping.user({"id": "u1", "displayName": "Ana Ruiz", "userPrincipalName": "ana@x.example",
                      "accountEnabled": False, "userType": "Member",
                      "createdDateTime": "2025-01-02T03:04:05Z", "employeeType": "Contractor"})
    assert (u.object_id, u.display_name, u.user_principal_name) == ("u1", "Ana Ruiz",
                                                                    "ana@x.example")
    assert u.account_enabled is False
    assert u.user_type == "Member"
    assert not u.removed
    assert u.created_at == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert u.raw_extra == {"employeeType": "Contractor"}


def test_secret_bearing_fields_are_never_kept() -> None:
    u = mapping.user({"id": "u1", "passwordProfile": {"password": "x"},
                      "passwordPolicies": "DisablePasswordExpiration", "jobTitle": "Ops"})
    assert u.raw_extra == {"jobTitle": "Ops"}


def test_a_removed_user_needs_only_its_id() -> None:
    u = mapping.user({"id": "u1", "@removed": {"reason": "deleted"}})
    assert u.removed
    assert u.display_name is None


@pytest.mark.parametrize("record", [{}, {"id": ""}, {"id": "u1", "createdDateTime": "today"},
                                    {"id": "u1", "createdDateTime": "2025-01-02T03:04:05"}])
def test_a_user_without_its_keys_is_malformed(record: dict[str, object]) -> None:
    with pytest.raises(mapping.MalformedRecordError):
        mapping.user(record)


def test_an_assignment_takes_its_role_name_from_the_expanded_definition() -> None:
    a = mapping.assignment({
        "id": "a1", "principalId": "u1", "roleDefinitionId": "r1", "directoryScopeId": "/",
        "appScopeId": None,
        "roleDefinition": {"id": "r1", "displayName": "Global Administrator", "isBuiltIn": True},
    })
    assert (a.assignment_id, a.principal_object_id, a.role_definition_id) == ("a1", "u1", "r1")
    assert a.role_name == "Global Administrator"
    assert a.directory_scope_id == "/"
    assert a.raw_extra == {"appScopeId": None,
                           "roleDefinition": {"id": "r1", "isBuiltIn": True}}


def test_an_assignment_without_a_principal_is_malformed() -> None:
    with pytest.raises(mapping.MalformedRecordError, match="principalId"):
        mapping.assignment({"id": "a1", "roleDefinitionId": "r1", "directoryScopeId": "/"})


# --- the fixtures ----------------------------------------------------------------------


def test_committed_fixtures_equal_the_generator() -> None:
    for name, body in build().items():
        assert (ROOT / name).read_bytes() == body, name


def test_the_fixtures_cover_every_case_and_hold_no_secret() -> None:
    raw = (ROOT / "entra/fixtures/directories.json").read_text()
    assert "password" not in raw.lower()
    assert "secret" not in raw.lower()
    users = [u for d in DIRS for u in d["users"]]
    assert {k for u in users for k in u} <= {*mapping.USER_SELECT, "employeeType"}
    assert any("id" not in u for u in users), "a malformed user"
    assert any("employeeType" in u for u in users), "an unmapped field"
    assert any("@removed" in u for d in DIRS for u in d["user_changes"])
    assert any("principalId" not in a for d in DIRS for a in d["role_assignments"])
    assert [d["code"] for d in DIRS if d["name"] == UNKNOWN_DIRECTORY_NAME] == [None]


# --- paging per directory ------------------------------------------------------------------


async def test_the_users_delta_pages_to_a_delta_link_then_reports_only_changes() -> None:
    fake = FakeEntra.from_fixtures(page_size=3)
    source = _source(fake)
    pages = await _pages(source, mapping.USER, NWL)
    assert len(pages) > 1
    assert fake.requests[0].url.params["$select"] == ",".join(mapping.USER_SELECT)
    assert sum(len(mapping.records(p)) for p in pages) == len(DIRS[0]["users"])
    link = mapping.delta_link(pages[-1])
    assert link is not None
    assert all(mapping.delta_link(p) is None for p in pages[:-1])
    fake.apply_user_changes(NWL)
    changed = await _pages(source, mapping.USER, NWL, link)
    records = [r for p in changed for r in mapping.records(p)]
    assert [r["id"] for r in records] == [u["id"] for u in DIRS[0]["user_changes"]]
    assert sum("@removed" in r for r in records) == 1


async def test_role_assignments_are_read_whole_with_their_definitions() -> None:
    fake = FakeEntra.from_fixtures(page_size=2)
    pages = await _pages(_source(fake), mapping.ROLE_ASSIGNMENT, NWL)
    assert len(pages) == 3
    assert all(r.url.params["$expand"] == "roleDefinition" for r in fake.requests)
    records = [r for p in pages for r in mapping.records(p)]
    assert len(records) == len(DIRS[0]["role_assignments"])
    assert all(mapping.delta_link(p) is None for p in pages), "a snapshot has no delta"


async def test_each_directory_is_read_with_its_own_client() -> None:
    fake = FakeEntra.from_fixtures(page_size=100)
    source = _source(fake)
    for d in DIRS:
        (page,) = await _pages(source, mapping.USER, d["directory_id"])
        assert [r.get("id") for r in mapping.records(page)] == [u.get("id") for u in d["users"]]
    assert await source.scopes(None, mapping.USER) == sorted(  # type: ignore[arg-type]
        d["directory_id"] for d in DIRS)
    with pytest.raises(ValueError, match="not configured"):
        await source.fetch(mapping.USER, "00000000-0000-0000-0000-000000000000", T0, None)


async def test_paths_are_the_documented_graph_endpoints() -> None:
    assert USERS_DELTA.startswith("/v1.0/users/delta?$select=id,")
    assert ROLE_ASSIGNMENTS == ("/v1.0/roleManagement/directory/roleAssignments"
                                "?$expand=roleDefinition")


async def test_a_long_retry_after_goes_to_temporal_whole() -> None:
    fake = FakeEntra.from_fixtures()
    fake.throttle.next, fake.throttle.seconds = 1, "1200"
    with pytest.raises(ApplicationError) as caught:
        await _source(fake).fetch(mapping.USER, NWL, T0, None)
    assert caught.value.next_retry_delay is not None
    assert caught.value.next_retry_delay.total_seconds() == 1200


def test_all_directories_share_one_graph_endpoint() -> None:
    with pytest.raises(ValueError, match="same Graph endpoint"):
        EntraSource({"a": GraphClient("https://one.example"),
                     "b": GraphClient("https://two.example")})
