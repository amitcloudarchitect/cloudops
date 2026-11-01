"""SharePoint through Microsoft Graph: the paths the connector reads.

    client = GraphClient(auth=..., transport=...)
    body = await client.get(delta_path(site_id))     # first sync; then the deltaLink
    data = await client.download(content_path(site_id, item_id))

Only sites granted to ``app-cloudops-sharepoint`` under Sites.Selected answer;
any other answers 403 ``accessDenied`` (LLD, App registrations).

A folder granted on its own (ListItems.SelectedOperations.Selected, ADR 0028) is
read by drive and item id: the folder, its children, and a file's content.

    body = await client.get(item_path(drive, folder))
    body = await client.get(children_path(drive, folder))   # then @odata.nextLink
    data = await client.download(item_content_path(drive, item)) The paging,
Retry-After and concurrency rules are ``graph/client.py``'s.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

from cloudops_connectors.graph.client import GraphClient

__all__ = [
    "GraphClient", "children_path", "content_path", "delta_path", "item_content_path",
    "item_path",
]

CHILDREN_PAGE: Final = 200


def _site(site_id: str) -> str:
    # Site ids are "host,site-guid,web-guid": commas and dots stay as they are.
    return quote(site_id, safe=",.-")


def delta_path(site_id: str) -> str:
    return f"/v1.0/sites/{_site(site_id)}/drive/root/delta"


def content_path(site_id: str, item_id: str) -> str:
    return f"/v1.0/sites/{_site(site_id)}/drive/items/{quote(item_id, safe='!-_')}/content"


def _drive_item(drive_id: str, item_id: str) -> str:
    # Drive ids are "b!" and URL-safe base64; item ids are alphanumeric.
    return f"/v1.0/drives/{quote(drive_id, safe='!-_')}/items/{quote(item_id, safe='!-_')}"


def item_path(drive_id: str, item_id: str) -> str:
    return _drive_item(drive_id, item_id)


def children_path(drive_id: str, item_id: str) -> str:
    return f"{_drive_item(drive_id, item_id)}/children?$top={CHILDREN_PAGE}"


def item_content_path(drive_id: str, item_id: str) -> str:
    return f"{_drive_item(drive_id, item_id)}/content"
