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

Folders granted on their own (ADR 0028, ``folders.json``) are served by drive and
item id: the item, its ``/children`` in ``$skiptoken`` pages, and ``/content``. Only
a granted folder and what is below it answer; anything else is 403
``accessDenied``, as Graph answers an app without a grant on it. ``revoke_folder``
withdraws a folder's grant; ``apply_folder_changes`` makes the fixture's second-run
changes (an edit, a deletion, a new file).
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Final

import httpx

from cloudops_connectors.graph.fake import DeltaLog, Throttle, error, respond, snapshot_page

FIXTURE_GRAPH: Final = "https://graph.fixture.example"
DOWNLOAD: Final = "https://download.fixture.example"
_DELTA: Final = re.compile(r"^/v1\.0/sites/([^/]+)/drive/root/delta$")
_CONTENT: Final = re.compile(r"^/v1\.0/sites/([^/]+)/drive/items/([^/]+)/content$")
_DOWNLOAD: Final = re.compile(r"^/(\d+)/([^/]+)$")
_DRIVE_ITEM: Final = re.compile(r"^/v1\.0/drives/([^/]+)/items/([^/]+)(/children|/content)?$")
_FOLDER_DOWNLOAD: Final = "folders"


def load_fixture(name: str = "sites.json") -> dict[str, Any]:
    data = resources.files("cloudops_connectors.sharepoint").joinpath(
        f"fixtures/{name}").read_bytes()
    loaded: dict[str, Any] = json.loads(data)
    return loaded


def load_folders() -> dict[str, Any]:
    return load_fixture("folders.json")


def folder_scope(drive_id: str, item_id: str) -> str:
    return f"{drive_id}/{item_id}"


@dataclass
class FakeLibrary:
    """One drive holding customer folders, each granted on its own."""

    drive_id: str
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    content: dict[str, bytes] = field(default_factory=dict)
    granted: set[str] = field(default_factory=set)
    changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    change_content: dict[str, bytes] = field(default_factory=dict)

    def parent(self, item_id: str) -> str | None:
        ref = self.items[item_id].get("parentReference")
        return ref.get("id") if isinstance(ref, dict) else None

    def reachable(self, item_id: str) -> bool:
        """Granted, or below a granted folder."""
        seen: str | None = item_id
        while seen is not None and seen in self.items:
            if seen in self.granted:
                return True
            seen = self.parent(seen)
        return False

    def children(self, item_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(i) for k, i in self.items.items() if self.parent(k) == item_id]


@dataclass
class FakeSite:
    site_id: str
    log: DeltaLog = field(default_factory=DeltaLog)
    content: dict[str, bytes] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    change_content: dict[str, bytes] = field(default_factory=dict)
    revoked: bool = False


class FakeSharePoint:
    def __init__(self, fixture: dict[str, Any], *, page_size: int = 4,
                 folders: dict[str, Any] | None = None) -> None:
        self.page_size = page_size
        self.sites: dict[str, FakeSite] = {}
        self.throttle = Throttle()
        self.expire_tokens = False
        self.requests: list[httpx.Request] = []
        self.library: FakeLibrary | None = None
        if folders is not None:
            self.library = lib = FakeLibrary(folders["drive_id"])
            for spec in folders["folders"]:
                fid = spec["folder"]["id"]
                lib.items[fid] = copy.deepcopy(spec["folder"])
                for item in spec["items"]:
                    lib.items[item["id"]] = copy.deepcopy(item)
                lib.content.update({k: v.encode() for k, v in spec["content"].items()})
                lib.granted.add(fid)
                lib.changes[fid] = copy.deepcopy(spec["changes"])
                lib.change_content.update(
                    {k: v.encode() for k, v in spec["change_content"].items()})
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
        return cls(load_fixture(), folders=load_folders(), **kwargs)

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

    def _lib(self) -> FakeLibrary:
        if self.library is None:
            raise ValueError("no folders fixture")
        return self.library

    def apply_folder_changes(self, folder_id: str) -> None:
        """The fixture's second-run changes in one folder."""
        lib = self._lib()
        changes = lib.changes[folder_id]
        del lib.items[changes["delete"]]
        for item in (changes["edit"], changes["add"]):
            lib.items[item["id"]] = copy.deepcopy(item)
            if item["id"] in lib.change_content:
                lib.content[item["id"]] = lib.change_content[item["id"]]

    def revoke_folder(self, folder_id: str) -> None:
        self._lib().granted.discard(folder_id)

    def rename_folder(self, folder_id: str, name: str) -> None:
        self._lib().items[folder_id]["name"] = name

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
        if (m := _DRIVE_ITEM.match(path)) is not None:
            return self._drive_item(m.group(1), m.group(2), m.group(3), url.params)
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

    def _drive_item(self, drive: str, item_id: str, what: str | None,
                    params: httpx.QueryParams) -> httpx.Response:
        lib = self.library
        if lib is None or drive != lib.drive_id:
            return error(404, "itemNotFound", "The resource could not be found.")
        if item_id not in lib.items:
            return error(404, "itemNotFound", "The resource could not be found.")
        if not lib.reachable(item_id):
            return error(403, "accessDenied", "Access denied")
        if what is None:
            return respond(200, {"@odata.context": f"{FIXTURE_GRAPH}/v1.0/$metadata",
                                 **copy.deepcopy(lib.items[item_id])})
        if what == "/children":
            path = f"/v1.0/drives/{drive}/items/{item_id}/children"
            top = int(params.get("$top", str(self.page_size)))
            return respond(200, snapshot_page(FIXTURE_GRAPH, path, lib.children(item_id),
                                              params, min(top, self.page_size), "$top="
                                              f"{top}&"))
        if item_id not in lib.content:
            return error(404, "itemNotFound", "The resource could not be found.")
        return httpx.Response(302, headers={
            "Location": f"{DOWNLOAD}/{_FOLDER_DOWNLOAD}/{item_id}?tempauth=fixture"})

    def _download(self, path: str) -> httpx.Response:
        if path.startswith(f"/{_FOLDER_DOWNLOAD}/") and self.library is not None:
            item = path.rsplit("/", 1)[1]
            lib = self.library
            if item not in lib.content or item not in lib.items or not lib.reachable(item):
                return httpx.Response(404)
            return httpx.Response(200, headers={"Content-Type": "application/octet-stream"},
                                  content=lib.content[item])
        m = _DOWNLOAD.match(path)
        if m is None:
            return httpx.Response(404)
        site = list(self.sites.values())[int(m.group(1))]
        body = site.content.get(m.group(2))
        if body is None or site.revoked:
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "application/octet-stream"},
                              content=body)
