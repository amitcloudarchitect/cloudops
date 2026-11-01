"""The customer selector's helper (ADR 0002): ``tenant_id`` narrows the scope the
principal resolved to, never widens it, and a tenant outside it looks absent."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

import pytest

from cloudops_api.access import Access
from cloudops_api.auth import Identity
from cloudops_api.pipeline import ApiError, narrow
from cloudops_tenancy import Role, TenantScope

A, B, C = uuid4(), uuid4(), uuid4()


def _access(kind: Literal["staff", "customer"], tenants: set[UUID], role: Role,
            accounts: frozenset[UUID] | None = None) -> Access:
    identity = Identity(uuid4())
    scope = TenantScope(tenants, role, identity.principal_id, account_ids=accounts)
    return Access(identity, kind, "test", frozenset(), scope)


def test_no_tenant_id_is_the_whole_scope() -> None:
    access = _access("staff", {A, B}, Role.DELIVERY_LEAD)
    assert narrow(access, None) is access.scope


def test_a_tenant_in_scope_narrows_to_it() -> None:
    access = _access("staff", {A, B}, Role.DELIVERY_LEAD)
    scope = narrow(access, A)
    assert scope.tenant_ids == {A}
    assert scope.role is access.scope.role
    assert scope.principal_id == access.scope.principal_id


@pytest.mark.parametrize("tenant", [C, uuid4()])
def test_a_tenant_outside_scope_is_not_found(tenant: UUID) -> None:
    access = _access("staff", {A, B}, Role.DELIVERY_LEAD)
    with pytest.raises(ApiError) as raised:
        narrow(access, tenant)
    assert (raised.value.status, raised.value.code) == (404, "not_found")


def test_a_customer_gets_only_its_own_tenant() -> None:
    access = _access("customer", {A}, Role.CUSTOMER)
    assert narrow(access, None).tenant_ids == {A}
    assert narrow(access, A).tenant_ids == {A}
    with pytest.raises(ApiError) as raised:
        narrow(access, B)
    assert (raised.value.status, raised.value.code) == (404, "not_found")


def test_a_subscription_restriction_is_kept() -> None:
    accounts = frozenset({uuid4()})
    access = _access("customer", {A}, Role.CUSTOMER, accounts)
    assert narrow(access, A).account_ids == accounts
