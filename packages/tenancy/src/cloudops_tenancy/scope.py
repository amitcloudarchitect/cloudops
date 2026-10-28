"""The resolved tenant scope a database transaction runs under.

A ``TenantScope`` is a value, not a lookup. It is resolved server-side from the
authenticated principal (Phase 3) and passed explicitly to
``Database.transaction``. Nothing in this package reads scope from a context
variable, a request object, a header or a token claim — if the caller does not
hand a scope over, there is no scope.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from cloudops_tenancy.errors import InvalidScopeError


class Role(enum.StrEnum):
    """Platform roles that row-level security policies key on via ``app.role``.

    Values are the strings the policies compare against, so renaming one is a
    database migration, not a refactor. These are role *classes*: platform
    admins define named roles at runtime (ADR 0002, D13), and each maps to one
    class here. Every customer role maps to ``CUSTOMER``, which no commercial
    policy admits.
    """

    CEO = "ceo"
    CFO = "cfo"
    FINOPS = "finops"
    SECOPS = "secops"
    DELIVERY_LEAD = "delivery_lead"
    COMMERCIAL = "commercial"
    AUDITOR = "auditor"
    PLATFORM_ADMIN = "platform_admin"
    ENGINEER = "engineer"
    CUSTOMER = "customer"
    # Only for the transaction that resolves who a principal is: no tenants,
    # no role any policy admits. It can call the resolver function and nothing else.
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class TenantScope:
    """Tenants, role and principal a single transaction is allowed to act as.

    An empty ``tenant_ids`` is valid and means "no tenant": every tenant-scoped
    table returns zero rows. It never means "all tenants".

    ``account_ids`` narrows the scope to some cloud accounts (subscriptions) of
    those tenants (ADR 0002, D16). ``None`` means every account of the scoped
    tenants; an empty collection means no account at all.
    """

    tenant_ids: frozenset[UUID]
    role: Role
    principal_id: str
    account_ids: frozenset[UUID] | None

    def __init__(
        self,
        tenant_ids: Iterable[UUID],
        role: Role,
        principal_id: str,
        account_ids: Iterable[UUID] | None = None,
    ) -> None:
        ids = _uuid_set(tenant_ids, "tenant_ids")
        accounts = None if account_ids is None else _uuid_set(account_ids, "account_ids")
        if type(role) is not Role:
            raise InvalidScopeError("role must be a Role member")
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise InvalidScopeError("principal_id must be a non-empty string")
        if not principal_id.isprintable():
            raise InvalidScopeError("principal_id must not contain control characters")
        object.__setattr__(self, "tenant_ids", ids)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "principal_id", principal_id)
        object.__setattr__(self, "account_ids", accounts)

    @classmethod
    def for_resolution(cls, principal_id: str) -> TenantScope:
        """The scope used to look a principal up: no tenants, no usable role."""
        return cls((), Role.UNRESOLVED, principal_id)

    @property
    def tenant_ids_setting(self) -> str:
        """``app.tenant_ids`` as ``cloudops.current_tenant_ids()`` parses it.

        Comma-separated, sorted so the same scope always produces the same
        string. Empty scope is the empty string, which the function maps to an
        empty array.
        """
        return ",".join(sorted(str(t) for t in self.tenant_ids))

    @property
    def account_ids_setting(self) -> str:
        """``app.account_ids`` as the account_scope policies read it.

        ``all`` for an unrestricted scope; otherwise the sorted ids, where the
        empty string means no account. Unset in the database also means no
        account, so a path that forgets this setting sees nothing.
        """
        if self.account_ids is None:
            return ACCOUNT_SCOPE_ALL
        return ",".join(sorted(str(a) for a in self.account_ids))


ACCOUNT_SCOPE_ALL = "all"


def _uuid_set(values: Iterable[UUID], name: str) -> frozenset[UUID]:
    # Strings are iterable; a bare id string would otherwise be split into
    # characters and then fail obscurely, or worse, partially succeed.
    if isinstance(values, str | bytes):
        raise InvalidScopeError(f"{name} must be a collection of UUIDs, not a string")
    ids = frozenset(values)
    if not all(type(t) is UUID for t in ids):
        raise InvalidScopeError(f"{name} must contain only uuid.UUID values")
    return ids
