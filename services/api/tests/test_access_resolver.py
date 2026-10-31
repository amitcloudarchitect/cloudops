"""The staff branch of scope resolution with no tenant (ADR 0002 D9; ADR 0017,
decision 12). No database: the resolver's one query is answered by a stub, so the
case a seeded test database cannot hold (no tenant at all) is covered."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloudops_api.access import NoScopeError, ScopeResolver
from cloudops_api.auth import Identity
from cloudops_tenancy import Role

OID = UUID("7014d608-c3fc-428d-86f0-a4f53dbc3a45")


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Db:
    """Answers resolve_principal_v2 with one fixed row."""

    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    @asynccontextmanager
    async def transaction(self, scope: object) -> AsyncIterator[_Conn]:
        yield _Conn(self._row)


class _Conn:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    async def execute(self, *_: object) -> _Rows:
        return _Rows([self._row])


def _row(*, all_tenants: bool, tenants: list[UUID], role_class: str = "platform_admin",
         kind: str = "staff") -> tuple[Any, ...]:
    return (kind, "Platform administrator", role_class, ["access.manage"], all_tenants,
            tenants, None, False, None)


def _identity() -> Identity:
    return Identity(oid=OID)


async def test_an_all_tenant_administrator_with_no_tenant_yet_gets_an_empty_scope() -> None:
    access = await ScopeResolver(_Db(_row(all_tenants=True, tenants=[]))).resolve(_identity())  # type: ignore[arg-type]
    assert access.scope.tenant_ids == frozenset()
    assert access.scope.role is Role.PLATFORM_ADMIN
    assert access.allows("access.manage")


async def test_staff_with_named_tenants_and_none_granted_are_still_refused() -> None:
    db = _Db(_row(all_tenants=False, tenants=[], role_class="engineer"))
    with pytest.raises(NoScopeError):
        await ScopeResolver(db).resolve(_identity())  # type: ignore[arg-type]


async def test_an_all_tenant_administrator_sees_the_tenants_that_exist() -> None:
    t = uuid4()
    access = await ScopeResolver(_Db(_row(all_tenants=True, tenants=[t]))).resolve(_identity())  # type: ignore[arg-type]
    assert access.scope.tenant_ids == frozenset({t})
