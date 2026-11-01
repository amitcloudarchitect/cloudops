"""A granted folder, read whole (ADR 0028).

A customer's folder is granted to ``app-cloudops-sharepoint`` on its own, under
ListItems.SelectedOperations.Selected. Graph's delta works only at a drive's root,
which the app cannot read, so each run reads the folder whole: the folder item,
then ``/children`` of it and of every folder below it, every page. What was stored
before and is missing now was deleted.

The bronze page for a run is the envelope ``walk`` returns: every Graph response
in the order it was read, each body kept as the exact text Graph sent, with the
path it answered. Nothing in it is parsed or changed before it is stored.

A snapshot is all or nothing. Any failure part-way raises, and a folder larger
than ``MAX_ITEMS`` or deeper than ``MAX_DEPTH`` is refused rather than read in
part: half a snapshot would mark the other half deleted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from cloudops_connectors.graph.client import GraphClient
from cloudops_connectors.sharepoint import mapping
from cloudops_connectors.sharepoint.client import children_path, item_path

MAX_ITEMS: Final = 5000
MAX_DEPTH: Final = 12


class SnapshotTooLargeError(ValueError):
    """The folder holds more than the connector reads in one snapshot. Not retried."""


class MalformedSnapshotError(ValueError):
    """The page is not a folder snapshot."""


def split_scope(scope: str) -> tuple[str, str]:
    """``<drive id>/<item id>`` -> (drive id, item id)."""
    drive, sep, item = scope.partition("/")
    if not sep or not drive or not item or "/" in item:
        raise ValueError("a folder scope is <drive id>/<item id>")
    return drive, item


async def walk(client: GraphClient, drive: str, folder: str) -> bytes:
    """The folder and everything below it, as the envelope bronze stores."""
    responses: list[dict[str, str]] = []
    path = item_path(drive, folder)
    responses.append({"path": path, "body": (await client.get(path)).decode("utf-8")})
    pending: list[tuple[str, int]] = [(folder, 0)]
    count = 0
    while pending:
        parent, depth = pending.pop(0)
        url: str | None = children_path(drive, parent)
        while url is not None:
            body = await client.get(url)
            responses.append({"path": url, "body": body.decode("utf-8")})
            for item in mapping.items(body):
                count += 1
                if count > MAX_ITEMS:
                    raise SnapshotTooLargeError(f"folder holds more than {MAX_ITEMS} items")
                if isinstance(item.get("folder"), dict) and isinstance(item.get("id"), str):
                    if depth + 1 > MAX_DEPTH:
                        raise SnapshotTooLargeError(f"folder is deeper than {MAX_DEPTH}")
                    pending.append((item["id"], depth + 1))
            url = mapping.next_link(body)
    envelope = {"drive_id": drive, "folder_id": folder, "responses": responses}
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Snapshot:
    drive_id: str
    folder_id: str
    folder: dict[str, Any]
    items: list[dict[str, Any]]  # everything below the folder, in the order read

    @property
    def site_id(self) -> str | None:
        ref = self.folder.get("parentReference")
        site = ref.get("siteId") if isinstance(ref, dict) else None
        return site if isinstance(site, str) and site else None


def parse(body: bytes) -> Snapshot:
    try:
        data = json.loads(body)
        responses = data["responses"]
        drive, folder_id = str(data["drive_id"]), str(data["folder_id"])
        folder = json.loads(responses[0]["body"])
        items: list[dict[str, Any]] = []
        for r in responses[1:]:
            items.extend(mapping.items(str(r["body"]).encode("utf-8")))
    except (ValueError, KeyError, TypeError, IndexError):
        raise MalformedSnapshotError("not a folder snapshot") from None
    if not isinstance(folder, dict) or folder.get("id") != folder_id:
        raise MalformedSnapshotError("the snapshot's first response is not its folder")
    return Snapshot(drive, folder_id, folder, items)
