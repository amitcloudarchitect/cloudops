"""Helpers shared by the schema and isolation tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import psycopg
import pytest

from cloudops_tenancy import Role, TenantScope

REPO_ROOT = Path(__file__).resolve().parents[2]

F = TypeVar("F", bound=Callable[..., Any])


def scope(*tenants: Any, role: Role = Role.ENGINEER) -> TenantScope:
    return TenantScope([t.id for t in tenants], role, "principal:isolation-suite")


@contextmanager
def raw_scope(
    dsn: str, tenant_ids: str, role: str, account_ids: str | None = "all"
) -> Iterator[psycopg.Connection[Any]]:
    """A connection with scope set by hand, for cases the tenancy package
    deliberately cannot express (unknown roles, malformed scope, planner
    settings). Transaction-local, rolled back on exit. ``account_ids=None``
    leaves app.account_ids unset."""
    with psycopg.connect(dsn) as cx:
        cx.execute(
            "SELECT set_config('app.tenant_ids', %s, true), set_config('app.role', %s, true)",
            (tenant_ids, role),
        )
        if account_ids is not None:
            cx.execute("SELECT set_config('app.account_ids', %s, true)", (account_ids,))
        try:
            yield cx
        finally:
            cx.rollback()


def pending(component: str) -> Callable[[F], F]:
    """Mark the part of an LLD isolation case that needs an unbuilt component.

    Strict xfail. The stub body calls ``stub()``: while the component is absent
    it fails and is reported as expected; once the component exists it passes,
    strict xfail turns that into a suite failure, and it stays red until a real
    test replaces the stub.
    """

    def decorate(fn: F) -> F:
        marked = pytest.mark.xfail(strict=True, reason=f"pending: {component}")(fn)
        marked.__doc__ = (fn.__doc__ or "") + f"\n\nPending: {component}."
        return marked  # type: ignore[no-any-return]

    return decorate


def stub(built: Callable[[], bool], component: str) -> None:
    if built():
        return  # XPASS(strict) -> fails: replace this stub with the real test
    pytest.fail(f"{component} does not exist yet")


def has_python(*globs: str) -> Callable[[], bool]:
    return lambda: any(any(REPO_ROOT.glob(g)) for g in globs)

