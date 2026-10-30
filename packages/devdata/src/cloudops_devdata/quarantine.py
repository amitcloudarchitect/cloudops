"""Hold a company name in the alias quarantine, as a connector would. Local only.

    CLOUDOPS_ENV=local python -m cloudops_devdata.quarantine --alias "Acme BV" --count 7

For the console's journeys (step 14b): each makes the held name it maps, so a
run never uses up the seeded quarantine. Writes as the ingest login through
packages/tenancy, with the same upsert the connector uses (a name seen again
adds to its count).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from sqlalchemy import text

from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_tenancy import Database, Role, TenantScope

_HOLD = text(
    "INSERT INTO staging.tenant_alias_quarantine (source_system, alias, occurrence_count,"
    " first_seen, last_seen) VALUES (:source, :alias, :count, now() - interval '1 day', now())"
    " ON CONFLICT (source_system, alias) DO UPDATE"
    " SET occurrence_count = staging.tenant_alias_quarantine.occurrence_count"
    "                        + EXCLUDED.occurrence_count, last_seen = now()"
)


async def hold(source: str, alias: str, count: int) -> None:
    settings = load_settings(os.environ, require={Component.DATABASE_INGEST})
    if settings.environment is not Environment.LOCAL:
        raise ConfigurationError("holding a name by hand is local-only")
    db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=1)
    try:
        async with db.transaction(TenantScope((), Role.INGEST, "devdata:quarantine")) as conn:
            await conn.execute(_HOLD, {"source": source, "alias": alias, "count": count})
    finally:
        await db.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_devdata.quarantine")
    parser.add_argument("--source", default="servicenow")
    parser.add_argument("--alias", required=True)
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args(argv)
    if args.count < 1 or not args.alias.strip():
        print("error: a name and a positive count", file=sys.stderr)
        return 2
    try:
        asyncio.run(hold(args.source, args.alias, args.count))
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
