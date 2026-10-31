"""A fake Entra ID (Microsoft Graph) serving recorded fixtures through httpx.

    fake = FakeEntra.from_fixtures()
    client = GraphClient(FIXTURE_GRAPH, transport=fake.transport(directory_id))

One transport per directory, as each directory has its own token. It answers
the users delta (``@odata.nextLink`` pages, a final ``@odata.deltaLink``,
``@removed`` for deletions) and the role assignments with their definitions
expanded, paged by ``$skiptoken``; plus 429 with ``Retry-After``. Tests change
a directory between runs (``apply_user_changes``, ``unassign``).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Final

import httpx

from cloudops_connectors.graph.fake import DeltaLog, Throttle, error, respond, snapshot_page

FIXTURE_GRAPH: Final = "https://graph.fixture.example"
_USERS: Final = "/v1.0/users/delta"
_ASSIGNMENTS: Final = "/v1.0/roleManagement/directory/roleAssignments"


def load_fixture() -> dict[str, Any]:
    data = resources.files("cloudops_connectors.entra").joinpath(
        "fixtures/directories.json").read_bytes()
    loaded: dict[str, Any] = json.loads(data)
    return loaded


@dataclass
class FakeDirectory:
    directory_id: str
    users: DeltaLog = field(default_factory=DeltaLog)
    assignments: list[dict[str, Any]] = field(default_factory=list)
    user_changes: list[dict[str, Any]] = field(default_factory=list)


class FakeEntra:
    def __init__(self, fixture: dict[str, Any], *, page_size: int = 3) -> None:
        self.page_size = page_size
        self.directories: dict[str, FakeDirectory] = {}
        self.throttle = Throttle()
        self.requests: list[httpx.Request] = []
        for spec in fixture["directories"]:
            d = FakeDirectory(spec["directory_id"])
            for n, user in enumerate(spec["users"]):
                d.users.put(copy.deepcopy(user), key=None if "id" in user else f"no-id-{n}")
            d.assignments = copy.deepcopy(spec["role_assignments"])
            d.user_changes = copy.deepcopy(spec["user_changes"])
            self.directories[d.directory_id] = d

    @classmethod
    def from_fixtures(cls, **kwargs: int) -> FakeEntra:
        return cls(load_fixture(), **kwargs)

    def apply_user_changes(self, directory_id: str) -> None:
        """The fixture's second-delta changes: a disabled account, a deletion, a new guest."""
        d = self.directories[directory_id]
        for user in d.user_changes:
            if "@removed" in user:
                d.users.remove(user["id"], copy.deepcopy(user))
            else:
                d.users.put(copy.deepcopy(user))

    def unassign(self, directory_id: str, assignment_id: str) -> None:
        d = self.directories[directory_id]
        d.assignments = [a for a in d.assignments if a.get("id") != assignment_id]

    def transport(self, directory_id: str) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            return self._handle(directory_id, request)

        return httpx.MockTransport(handle)

    def _handle(self, directory_id: str, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        throttled = self.throttle.take()
        if throttled is not None:
            return throttled
        d = self.directories.get(directory_id)
        if d is None:
            return error(401, "InvalidAuthenticationToken", "Access token validation failure.")
        url = request.url
        if url.path == _USERS:
            return respond(200, d.users.page(FIXTURE_GRAPH, _USERS, url.params, self.page_size))
        if url.path == _ASSIGNMENTS:
            return respond(200, snapshot_page(FIXTURE_GRAPH, _ASSIGNMENTS, d.assignments,
                                              url.params, self.page_size,
                                              "$expand=roleDefinition&"))
        return error(404, "Request_ResourceNotFound", "Unsupported path.")
