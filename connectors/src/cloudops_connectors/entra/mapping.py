"""Entra ID users and directory role assignments -> canonical rows.

Read for access control and audit evidence (HLD), with ``app-cloudops-graph``'s
read-only permissions (LLD, App registrations).

- ``user``: the users delta (``/users/delta``), selecting only what the
  platform needs: display name, UPN, enabled, user type and creation time.
  ``@removed`` marks a deleted user. Anything else Graph sends is kept in
  ``raw_extra``, except fields that can carry a secret (``NEVER_STORED``),
  which are dropped: Graph never returns ``passwordProfile``, and if it ever
  did, the platform must not keep it.
- ``role_assignment``: ``/roleManagement/directory/roleAssignments`` with the
  role definition expanded, read whole every run. The definition's display
  name is the role name; the rest of the definition, and every unmapped field,
  is kept in ``raw_extra``.

A record without the fields that key it is rejected and counted; it stays in
bronze. Names and UPNs are data, never instruction (CLAUDE.md rule 4).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

SOURCE: Final = "entra"
USER: Final = "user"
ROLE_ASSIGNMENT: Final = "role_assignment"
ENTITIES: Final = frozenset({USER, ROLE_ASSIGNMENT})

USER_SELECT: Final = ("id", "displayName", "userPrincipalName", "accountEnabled", "userType",
                      "createdDateTime")
USER_MAPPED: Final = frozenset({*USER_SELECT, "@removed"})
USER_EXPECTED: Final = frozenset({"id"})  # @removed entries carry nothing else
ASSIGNMENT_MAPPED: Final = frozenset({
    "id", "principalId", "roleDefinitionId", "directoryScopeId", "roleDefinition",
})
ASSIGNMENT_EXPECTED: Final = frozenset({"id", "principalId", "roleDefinitionId",
                                        "directoryScopeId"})
NEVER_STORED: Final = frozenset({"passwordProfile", "passwordPolicies"})


class MalformedRecordError(ValueError):
    """A record without the fields that key it. Kept in bronze, counted, skipped."""


@dataclass(frozen=True)
class User:
    object_id: str
    removed: bool
    display_name: str | None
    user_principal_name: str | None
    account_enabled: bool | None
    user_type: str | None
    created_at: datetime | None
    raw_extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Assignment:
    assignment_id: str
    principal_object_id: str
    role_definition_id: str
    role_name: str | None
    directory_scope_id: str
    raw_extra: dict[str, Any] = field(default_factory=dict)


def page(body: bytes) -> dict[str, Any]:
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("value"), list):
        raise ValueError("not a Graph collection page")
    return data


def records(body: bytes) -> list[dict[str, Any]]:
    return [r if isinstance(r, dict) else {} for r in page(body)["value"]]


def next_link(body: bytes) -> str | None:
    link = page(body).get("@odata.nextLink")
    return link if isinstance(link, str) and link else None


def delta_link(body: bytes) -> str | None:
    link = page(body).get("@odata.deltaLink")
    return link if isinstance(link, str) and link else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _time(value: Any, name: str) -> datetime | None:
    raw = _text(value)
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedRecordError(f"{name} is not an ISO date-time") from None
    if moment.tzinfo is None:
        raise MalformedRecordError(f"{name} has no time zone")
    return moment.astimezone(UTC)


def _extra(record: Mapping[str, Any], mapped: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in mapped and k not in NEVER_STORED}


def user(record: Mapping[str, Any]) -> User:
    object_id = _text(record.get("id"))
    if object_id is None:
        raise MalformedRecordError("id is required")
    enabled = record.get("accountEnabled")
    return User(
        object_id=object_id,
        removed=isinstance(record.get("@removed"), Mapping),
        display_name=_text(record.get("displayName")),
        user_principal_name=_text(record.get("userPrincipalName")),
        account_enabled=enabled if isinstance(enabled, bool) else None,
        user_type=_text(record.get("userType")),
        created_at=_time(record.get("createdDateTime"), "createdDateTime"),
        raw_extra=_extra(record, USER_MAPPED),
    )


def assignment(record: Mapping[str, Any]) -> Assignment:
    keys = {k: _text(record.get(k)) for k in ("id", "principalId", "roleDefinitionId",
                                              "directoryScopeId")}
    missing = sorted(k for k, v in keys.items() if v is None)
    if missing:
        raise MalformedRecordError(f"{', '.join(missing)} required")
    definition = record.get("roleDefinition")
    extra = _extra(record, ASSIGNMENT_MAPPED)
    role_name = None
    if isinstance(definition, Mapping):
        role_name = _text(definition.get("displayName"))
        rest = {k: v for k, v in definition.items() if k != "displayName"}
        if rest:
            extra["roleDefinition"] = rest
    return Assignment(
        assignment_id=str(keys["id"]), principal_object_id=str(keys["principalId"]),
        role_definition_id=str(keys["roleDefinitionId"]), role_name=role_name,
        directory_scope_id=str(keys["directoryScopeId"]), raw_extra=extra,
    )
