"""The first administrator's bootstrap (ADR 0002 D14; --once, ADR 0017).

The session's database already has an administrator (conftest), so every call here
meets the refusal: plain, it is an error; with ``once``, it is "already done". Either
way no second administrator appears.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from sqlalchemy.exc import DBAPIError

from cloudops_api.bootstrap import already_exists, bootstrap
from cloudops_tenancy import Database

from .conftest import Seeded


def _admins(seeded: Seeded) -> int:
    """Counted as the superuser: the application role cannot read the table."""
    with psycopg.connect(seeded.admin_dsn) as cx:
        row = cx.execute(
            "SELECT count(*) FROM access.principal p JOIN access.role_definition r"
            " ON r.id = p.role_id WHERE r.role_class = 'platform_admin'").fetchone()
    assert row is not None
    return int(row[0])


async def test_a_second_administrator_is_refused(
    app_db: Database, seeded: Seeded, principals: object
) -> None:
    before = _admins(seeded)
    assert before >= 1
    with pytest.raises(DBAPIError) as refused:
        await bootstrap(app_db, uuid4(), "second")
    assert already_exists(refused.value)
    assert _admins(seeded) == before


async def test_once_treats_an_existing_administrator_as_done(
    app_db: Database, seeded: Seeded, principals: object
) -> None:
    before = _admins(seeded)
    assert await bootstrap(app_db, uuid4(), "second", once=True) is False
    assert _admins(seeded) == before


def test_only_that_refusal_counts_as_done() -> None:
    class OrigError(Exception):
        sqlstate = "42501"

    assert not already_exists(DBAPIError("SELECT 1", {}, OrigError("permission denied for table")))
    assert not already_exists(ValueError("a platform administrator already exists"))
