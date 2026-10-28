"""Unit tests for TenantScope and the statement guard. No database needed."""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloudops_tenancy import InvalidScopeError, Role, ScopeTamperingError, TenantScope
from cloudops_tenancy._guard import check_statement

A = UUID("00000000-0000-4000-8000-00000000000a")
B = UUID("00000000-0000-4000-8000-00000000000b")


def test_setting_is_sorted_and_deterministic() -> None:
    assert TenantScope([B, A], Role.CFO, "p").tenant_ids_setting == f"{A},{B}"
    assert TenantScope([A, B], Role.CFO, "p") == TenantScope([B, A, A], Role.CFO, "p")


def test_empty_scope_is_the_empty_string() -> None:
    assert TenantScope([], Role.ENGINEER, "p").tenant_ids_setting == ""


def test_scope_is_immutable() -> None:
    scope = TenantScope([A], Role.ENGINEER, "p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.tenant_ids = frozenset({B})  # type: ignore[misc]


@pytest.mark.parametrize(
    ("tenant_ids", "role", "principal"),
    [
        (str(A), Role.CFO, "p"),  # a string is iterable; must not be split into chars
        ([str(A)], Role.CFO, "p"),  # strings, not UUIDs
        ([A], "cfo", "p"),  # role must be the enum, not its value
        ([A], Role.CFO, ""),
        ([A], Role.CFO, "   "),
        ([A], Role.CFO, "p\x00q"),
        ([A], Role.CFO, None),
    ],
)
def test_invalid_scope_is_rejected(tenant_ids: Any, role: Any, principal: Any) -> None:
    with pytest.raises(InvalidScopeError):
        TenantScope(tenant_ids, role, principal)


def test_uuid_subclass_is_rejected() -> None:
    class Sneaky(UUID):
        def __str__(self) -> str:
            return "00000000-0000-4000-8000-000000000000,00000000-0000-4000-8000-0000000000ff"

    with pytest.raises(InvalidScopeError):
        TenantScope([Sneaky(int=uuid4().int)], Role.CFO, "p")


@pytest.mark.parametrize(
    "sql",
    [
        "SET app.tenant_ids = 'x'",
        "set local app.role = 'cfo'",
        "SET ROLE postgres",
        "SET SESSION AUTHORIZATION postgres",
        "  RESET ALL",
        "DISCARD ALL",
        "SELECT set_config('app.tenant_ids', 'x', false)",
        "SELECT pg_catalog.set_config('app.role', 'cfo', true)",
        'SELECT "set_config"(\'app.role\', \'cfo\', true)',
        "SELECT 1; SET app.role = 'cfo'",
        "/* harmless */ SET app.role = 'cfo'",
        "-- harmless\nSET app.role = 'cfo'",
        "COMMIT",
        "ROLLBACK",
        "BEGIN",
        "START TRANSACTION",
        "END",
        "ABORT",
        "PREPARE TRANSACTION 'x'",
        "DO $$ BEGIN EXECUTE 'SE' || 'T app.role = ''cfo'''; END $$",
    ],
)
def test_guard_rejects_scope_and_transaction_changes(sql: str) -> None:
    with pytest.raises(ScopeTamperingError):
        check_statement(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM cloudops.resource WHERE tenant_id = ANY(%(ids)s)",
        "UPDATE cloudops.resource SET name = %(n)s WHERE id = %(id)s",
        "INSERT INTO cloudops.work_item (state) VALUES ('reset')",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT current_setting('app.role', true)",
    ],
)
def test_guard_allows_ordinary_statements(sql: str) -> None:
    check_statement(sql)


# --- account scope (ADR 0002, D16) ---------------------------------------------


def test_unrestricted_accounts_are_all() -> None:
    assert TenantScope([uuid4()], Role.ENGINEER, "p").account_ids_setting == "all"


def test_restricted_accounts_are_sorted_and_empty_means_none() -> None:
    a, b = sorted([uuid4(), uuid4()], key=str)
    s = TenantScope([uuid4()], Role.CUSTOMER, "p", account_ids=[b, a])
    assert s.account_ids_setting == f"{a},{b}"
    assert TenantScope([uuid4()], Role.CUSTOMER, "p", account_ids=[]).account_ids_setting == ""


@pytest.mark.parametrize("bad", ["0b5c...", ["not-a-uuid"], [1]])
def test_invalid_account_ids_are_rejected(bad: Any) -> None:
    with pytest.raises(InvalidScopeError):
        TenantScope([uuid4()], Role.ENGINEER, "p", account_ids=bad)


def test_resolution_scope_has_no_tenants_and_no_usable_role() -> None:
    s = TenantScope.for_resolution("user:x")
    assert s.tenant_ids == frozenset()
    assert s.role is Role.UNRESOLVED
