"""audit.action_log is append-only: for the application by privilege, for the
owner by trigger."""

from __future__ import annotations

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database

from .conftest import Seeded
from .support import raw_scope, scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


async def test_application_can_append(app_db: Database, seeded: Seeded) -> None:
    async with app_db.transaction(scope(seeded.a)) as conn:
        await conn.execute(
            text(
                "INSERT INTO audit.action_log (tenant_id, actor, action)"
                " VALUES (:t, 'user:a', 'view')"
            ),
            {"t": seeded.a.id},
        )
        count = await conn.scalar(
            text("SELECT count(*) FROM audit.action_log WHERE action = 'view'")
        )
    assert count == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.action_log SET outcome = 'succeeded'",
        "DELETE FROM audit.action_log",
        "TRUNCATE audit.action_log",
    ],
)
@pytest.mark.parametrize("login", ["app_dsn", "ingest_dsn"])
def test_application_roles_cannot_rewrite_history(
    seeded: Seeded, login: str, statement: str
) -> None:
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        raw_scope(getattr(seeded, login), str(seeded.a.id), "platform_admin") as cx,
    ):
        cx.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.action_log SET outcome = 'succeeded'",
        "DELETE FROM audit.action_log",
        "TRUNCATE audit.action_log",
    ],
)
def test_even_the_owner_cannot_rewrite_history(seeded: Seeded, statement: str) -> None:
    """Scoped so the owner can see rows (RLS is FORCEd for it too); the trigger
    still refuses."""
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"),
        raw_scope(seeded.migrator_dsn, str(seeded.a.id), "platform_admin") as cx,
    ):
        cx.execute(statement)
