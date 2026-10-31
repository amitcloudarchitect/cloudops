"""SharePoint through Microsoft Graph: the paths the connector reads.

    client = GraphClient(auth=..., transport=...)
    body = await client.get(delta_path(site_id))     # first sync; then the deltaLink
    data = await client.download(content_path(site_id, item_id))

Only sites granted to ``app-cloudops-sharepoint`` under Sites.Selected answer;
any other answers 403 ``accessDenied`` (LLD, App registrations). The paging,
Retry-After and concurrency rules are ``graph/client.py``'s.
"""

from __future__ import annotations

from urllib.parse import quote

from cloudops_connectors.graph.client import GraphClient

__all__ = ["GraphClient", "content_path", "delta_path"]


def _site(site_id: str) -> str:
    # Site ids are "host,site-guid,web-guid": commas and dots stay as they are.
    return quote(site_id, safe=",.-")


def delta_path(site_id: str) -> str:
    return f"/v1.0/sites/{_site(site_id)}/drive/root/delta"


def content_path(site_id: str, item_id: str) -> str:
    return f"/v1.0/sites/{_site(site_id)}/drive/items/{quote(item_id, safe='!-_')}/content"
