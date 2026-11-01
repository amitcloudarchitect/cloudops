"""Purge offboarded tenants whose purge a platform administrator asked for (ADR 0038).

    python -m cloudops_connectors.lifecycle purge    # run waiting purges once, print them

In Azure the ``azure`` connector's worker runs the same loop every minute beside its
own (``core/cli.py``): it holds the bronze container the stored credentials' pages
land in (ADR 0027).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import asdict

from cloudops_config import Component, ConfigurationError, DbLogin, load_settings
from cloudops_connectors.core.cli import bronze_container
from cloudops_connectors.lifecycle.purge import BlobBronzePages, TenantPurge
from cloudops_observability import TelemetryConfigError
from cloudops_observability.process import start
from cloudops_tenancy import Database


async def _main() -> int:
    settings = load_settings(os.environ, require={Component.DATABASE_INGEST, Component.BLOB})
    async with AsyncExitStack() as stack:
        db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=2)
        stack.push_async_callback(db.dispose)
        container = bronze_container(settings)
        stack.push_async_callback(container.close)
        for report in await TenantPurge(db, [BlobBronzePages(container)]).run():
            print(json.dumps(asdict(report), indent=2, sort_keys=True, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_connectors.lifecycle")
    parser.add_argument("command", choices=["purge"])
    parser.parse_args(argv)
    try:
        telemetry = start("connector-lifecycle", os.environ)
        try:
            return asyncio.run(_main())
        finally:
            telemetry.shutdown()
    except (ConfigurationError, TelemetryConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
