"""Entra ID through Microsoft Graph: the paths the connector reads.

    client = GraphClient(auth=..., transport=...)   # one per customer directory
    body = await client.get(USERS_DELTA)             # first sync; then the deltaLink
    body = await client.get(ROLE_ASSIGNMENTS)        # every run, whole; then nextLinks

A token is issued per directory, so each directory has its own client (and
``auth``). The paging, Retry-After and concurrency rules are ``graph/client.py``'s.
"""

from __future__ import annotations

from typing import Final

from cloudops_connectors.entra.mapping import USER_SELECT

USERS_DELTA: Final = f"/v1.0/users/delta?$select={','.join(USER_SELECT)}"
ROLE_ASSIGNMENTS: Final = "/v1.0/roleManagement/directory/roleAssignments?$expand=roleDefinition"
