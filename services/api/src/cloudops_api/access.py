"""Server-side scope resolution (request lifecycle, stage 2; ADR 0002).

    access = await resolver.resolve(identity)
    async with db.transaction(access.scope) as conn: ...

The only input is the authenticated ``oid``. Role, tenants and subscriptions
come from ``access.resolve_principal_v2()`` in the database, on every request,
so a revoked assignment stops working on the next request (D4). Nothing here
reads a token claim, a header or a query parameter, because nothing is passed
one.

A customer resolves only while its tenant's portal is live and its role is in
the set agreed with the customer (migration 0011); otherwise it is unknown here.

Two kinds of failure, deliberately different:

- ``NoScopeError``: a valid identity with nothing it may see. The request is
  refused with 403.
- ``ScopeDefectError``: the access data is in a state that should be
  impossible, such as a customer with two tenants, a staff member holding a
  customer role, or a subscription restriction spanning tenants. That is a
  defect, not a permission. It raises, is logged, and returns 500. It never
  degrades to a partial or guessed scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import text

from cloudops_api.auth import Identity
from cloudops_tenancy import Database, Role, TenantScope

_RESOLVE: Final = text(
    "SELECT kind, role_name, role_class, permissions, all_tenants, tenant_ids, account_ids,"
    " is_tenant_admin, landing FROM access.resolve_principal_v2(:oid)"
)

STAFF_ROLES: Final = frozenset(Role) - {Role.CUSTOMER, Role.UNRESOLVED, Role.INGEST}


class NoScopeError(Exception):
    """A valid identity that may see nothing. Safe to report as 403."""


class ScopeDefectError(Exception):
    """Access data that should be impossible. Reported as 500, never served."""


@dataclass(frozen=True, slots=True)
class Access:
    identity: Identity
    kind: Literal["staff", "customer"]
    role_name: str
    permissions: frozenset[str]
    scope: TenantScope
    # A customer administrator of its own tenant (ADR 0018, D2). Never staff.
    is_tenant_admin: bool = False
    # Where the role opens in the console; None: its first module.
    landing: str | None = None

    def allows(self, permission: str) -> bool:
        return permission in self.permissions


class ScopeResolver:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def resolve(self, identity: Identity) -> Access:
        lookup = TenantScope.for_resolution(identity.principal_id)
        async with self._db.transaction(lookup) as conn:
            rows = (await conn.execute(_RESOLVE, {"oid": identity.oid})).all()
        if not rows:
            raise NoScopeError("unknown or disabled principal")
        if len(rows) > 1:
            raise ScopeDefectError("principal resolved to more than one record")
        row: tuple[Any, ...] = tuple(rows[0])
        (kind, role_name, role_class, permissions, all_tenants, tenants, accounts,
         tenant_admin, landing) = row
        try:
            role = Role(role_class)
        except ValueError:
            raise ScopeDefectError("principal holds an unknown role class") from None
        tenant_ids = frozenset(UUID(str(t)) for t in tenants)
        account_ids = None if accounts is None else frozenset(UUID(str(a)) for a in accounts)

        if kind == "customer":
            # A customer is one tenant, always (D7). Anything else is a defect.
            if role is not Role.CUSTOMER:
                raise ScopeDefectError("customer principal holds a staff role")
            if all_tenants:
                raise ScopeDefectError("customer principal marked all-tenants")
            if len(tenant_ids) != 1:
                raise ScopeDefectError("customer principal does not resolve to exactly one tenant")
        elif kind == "staff":
            if role not in STAFF_ROLES:
                raise ScopeDefectError("staff principal holds a non-staff role")
            if not tenant_ids:
                raise NoScopeError("no tenants assigned")
        else:
            raise ScopeDefectError("unknown principal kind")

        if account_ids is not None:
            # A subscription restriction is within one tenant (D16): a flat list
            # of accounts across tenants would hide every other tenant's data.
            if len(tenant_ids) != 1:
                raise ScopeDefectError("subscription restriction spans more than one tenant")
            if not account_ids:
                raise NoScopeError("no subscriptions assigned")

        if tenant_admin and kind != "customer":
            raise ScopeDefectError("staff principal marked tenant administrator")

        scope = TenantScope(tenant_ids, role, identity.principal_id, account_ids=account_ids)
        return Access(
            identity, kind, str(role_name), frozenset(permissions), scope,
            is_tenant_admin=bool(tenant_admin), landing=None if landing is None else str(landing),
        )
