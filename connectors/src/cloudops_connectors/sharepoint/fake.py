"""A fake SharePoint (Microsoft Graph) serving recorded fixtures through httpx.

    fake = FakeSharePoint.from_fixtures()
    client = GraphClient(FIXTURE_GRAPH, transport=fake.transport())

It answers what the connector asks, as Graph does: a drive's delta with
``@odata.nextLink`` pages and a final ``@odata.deltaLink``; ``/content`` with a
302 to a pre-authenticated download URL; 429 with ``Retry-After``; 403
``accessDenied`` for a site whose consent was withdrawn (``revoke``); 404
``itemNotFound`` for a site it does not have; 410 ``resyncRequired`` for an
expired delta token. Tests change the drive between runs (``apply_changes``,
``put_file``, ``delete``).
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Final

import httpx

from cloudops_connectors.graph.fake import DeltaLog, Throttle, error, respond

FIXTURE_GRAPH: Final = "https://graph.fixture.example"
DOWNLOAD: Final = "https://download.fixture.example"
_DELTA: Final = re.compile(r"^/v1\.0/sites/([^/]+)/drive/root/delta$")
_CONTENT: Final = re.compile(r"^/v1\.0/sites/([^/]+)/drive/items/([^/]+)/content$")
_DOWNLOAD: Final = re.compile(r"^/(\d+)/([^/]+)$")


def load_fixture() -> dict[str, Any]:
    data = resources.files("cloudops_connectors.sharepoint").joinpath(
        "fixtures/sites.json").read_bytes()
    loaded: dict[str, Any] = json.loads(data)
    return loaded


@dataclass
class FakeSite:
    site_id: str
    log: DeltaLog = field(default_factory=DeltaLog)
    content: dict[str, bytes] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    change_content: dict[str, bytes] = field(default_factory=dict)
    revoked: bool = False


class FakeSharePoint:
    def __init__(self, fixture: dict[str, Any], *, page_size: int = 4) -> None:
        self.page_size = page_size
        self.sites: dict[str, FakeSite] = {}
        self.throttle = Throttle()
        self.expire_tokens = False
        self.requests: list[httpx.Request] = []
        for spec in fixture["sites"]:
            site = FakeSite(spec["site_id"])
            for n, item in enumerate(spec["delta"]):
                site.log.put(copy.deepcopy(item), key=None if "id" in item else f"no-id-{n}")
            site.content = {k: v.encode() for k, v in spec["content"].items()}
            site.change_content = {k: v.encode() for k, v in spec["change_content"].items()}
            site.changes = copy.deepcopy(spec["changes"])
            self.sites[site.site_id] = site

    @classmethod
    def from_fixtures(cls, **kwargs: int) -> FakeSharePoint:
        return cls(load_fixture(), **kwargs)

    # --- what tests do between runs -------------------------------------------------

    def apply_changes(self, site_id: str) -> None:
        """The fixture's second-delta changes: an edit, a deletion, a new file."""
        site = self.sites[site_id]
        for item in site.changes:
            if "deleted" in item:
                site.log.remove(item["id"], copy.deepcopy(item))
            else:
                site.log.put(copy.deepcopy(item))
            if item["id"] in site.change_content:
                site.content[item["id"]] = site.change_content[item["id"]]

    def put_file(self, site_id: str, item: dict[str, Any], text: str) -> None:
        site = self.sites[site_id]
        site.log.put(copy.deepcopy(item))
        site.content[item["id"]] = text.encode()

    def delete(self, site_id: str, item_id: str) -> None:
        self.sites[site_id].log.remove(item_id, {"id": item_id, "deleted": {"state": "deleted"}})

    def revoke(self, site_id: str) -> None:
        """The site's Sites.Selected grant is withdrawn."""
        self.sites[site_id].revoked = True

    # --- the API --------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        throttled = self.throttle.take()
        if throttled is not None:
            return throttled
        url = request.url
        if f"{url.scheme}://{url.host}" == DOWNLOAD:
            return self._download(url.path)
        path = url.path
        if (m := _DELTA.match(path)) is not None:
            site = self._site(m.group(1))
            if isinstance(site, httpx.Response):
                return site
            if self.expire_tokens and "$deltatoken" in url.params:
                return error(410, "resyncRequired", "Resync required.")
            return respond(200, site.log.page(FIXTURE_GRAPH, path, url.params, self.page_size))
        if (m := _CONTENT.match(path)) is not None:
            site = self._site(m.group(1))
            if isinstance(site, httpx.Response):
                return site
            if m.group(2) not in site.content or site.log.live(m.group(2)) is None:
                return error(404, "itemNotFound", "The resource could not be found.")
            index = list(self.sites).index(site.site_id)
            return httpx.Response(302, headers={
                "Location": f"{DOWNLOAD}/{index}/{m.group(2)}?tempauth=fixture"})
        return error(404, "invalidRequest", "Unsupported path.")

    def _site(self, raw: str) -> FakeSite | httpx.Response:
        site = self.sites.get(raw)
        if site is None:
            return error(404, "itemNotFound", "Requested site could not be found")
        if site.revoked:
            return error(403, "accessDenied", "Access denied")
        return site

    def _download(self, path: str) -> httpx.Response:
        m = _DOWNLOAD.match(path)
        if m is None:
            return httpx.Response(404)
        site = list(self.sites.values())[int(m.group(1))]
        body = site.content.get(m.group(2))
        if body is None or site.revoked:
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "application/octet-stream"},
                              content=body)
