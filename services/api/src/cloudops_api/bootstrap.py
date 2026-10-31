"""Create the first platform administrator (ADR 0002, D14).

    python -m cloudops_api.bootstrap --display-name "Platform admin" [--once]

The administrator is an Entra object id supplied at deployment as the secret
``platform-admin-oid`` (.env.local locally, Key Vault in Azure), never written
in code. ``access.bootstrap_admin`` refuses if an active administrator already
exists, so running this twice, or later, cannot add a second one. Every other
principal is created by an administrator, through the access functions.

``--once`` is for a deployment that runs this on every release (the Argo CD hook in
infra/gitops/base/api, ADR 0017): an administrator already existing means the work
is done, so it exits 0. It is the same refusal; nothing is added either way.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_config import Component, ConfigurationError, DbLogin, load_settings
from cloudops_tenancy import Database, TenantScope

ALREADY_EXISTS = "a platform administrator already exists"


def already_exists(exc: BaseException) -> bool:
    """The database's refusal because an administrator exists, and nothing else."""
    if not isinstance(exc, DBAPIError):
        return False
    sqlstate = getattr(exc.orig, "sqlstate", None)
    return sqlstate == "42501" and ALREADY_EXISTS in str(exc.orig)


async def bootstrap(db: Database, oid: UUID, display_name: str, *, once: bool = False) -> bool:
    """Create the administrator; True if created. With ``once``, False if one exists."""
    try:
        async with db.transaction(TenantScope.for_resolution(f"bootstrap:{oid}")) as conn:
            await conn.execute(
                text("SELECT access.bootstrap_admin(:oid, :name)"),
                {"oid": oid, "name": display_name},
            )
    except DBAPIError as exc:
        if once and already_exists(exc):
            return False
        raise
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_api.bootstrap")
    parser.add_argument("--display-name", default="Platform administrator")
    parser.add_argument("--once", action="store_true",
                        help="exit 0 if an administrator already exists")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(os.environ, require={Component.DATABASE_APP})
        oid = UUID(settings.secrets.get("platform-admin-oid"))
    except (ConfigurationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    db = Database.create(settings.database_dsn(DbLogin.APP), pool_size=1)

    async def run() -> bool:
        try:
            return await bootstrap(db, oid, args.display_name, once=args.once)
        finally:
            await db.dispose()

    try:
        created = asyncio.run(run())
    except Exception as exc:  # the database's refusal is the message
        print(f"bootstrap refused: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 1
    print("platform administrator created" if created
          else "a platform administrator already exists; nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
