"""Create the first platform administrator (ADR 0002, D14).

    python -m cloudops_api.bootstrap --display-name "Platform admin"

The administrator is an Entra object id supplied at deployment as the secret
``platform-admin-oid`` (.env.local locally, Key Vault in Azure), never written
in code. ``access.bootstrap_admin`` refuses if an active administrator already
exists, so running this twice, or later, cannot add a second one. Every other
principal is created by an administrator, through the access functions.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from uuid import UUID

from sqlalchemy import text

from cloudops_config import Component, ConfigurationError, DbLogin, load_settings
from cloudops_tenancy import Database, TenantScope


async def bootstrap(db: Database, oid: UUID, display_name: str) -> None:
    async with db.transaction(TenantScope.for_resolution(f"bootstrap:{oid}")) as conn:
        await conn.execute(
            text("SELECT access.bootstrap_admin(:oid, :name)"), {"oid": oid, "name": display_name}
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_api.bootstrap")
    parser.add_argument("--display-name", default="Platform administrator")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(os.environ, require={Component.DATABASE_APP})
        oid = UUID(settings.secrets.get("platform-admin-oid"))
    except (ConfigurationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    db = Database.create(settings.database_dsn(DbLogin.APP), pool_size=1)

    async def run() -> None:
        try:
            await bootstrap(db, oid, args.display_name)
        finally:
            await db.dispose()

    try:
        asyncio.run(run())
    except Exception as exc:  # the database's refusal is the message
        print(f"bootstrap refused: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 1
    print("platform administrator created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
