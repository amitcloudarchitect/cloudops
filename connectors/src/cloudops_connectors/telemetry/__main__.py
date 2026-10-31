"""Run the telemetry stream locally over the recorded events.

    python -m cloudops_connectors.telemetry run --fixtures

Publishes the recorded events (re-timed to the last hour, since the store
expires rows by the real clock) into the in-memory hub, which captures each one
to bronze in Azurite (``evh-metrics-capture``) as it is published; consumes them
through the real pipeline into the local ClickHouse, checkpointing after each
stored batch; flushes the run figures; prints them.

There is no Event Hubs path from the command line yet: the Azure telemetry
store is LLD open item L1, so without ``--fixtures`` this refuses. The SDK
binding is ``eventhub.consume``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob.aio import ContainerClient

from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_connectors.core.cli import AZURITE, NoCredentialPathError
from cloudops_connectors.core.raw import BlobRawStore
from cloudops_connectors.telemetry.hub import Capture, InMemoryHub, MemoryCheckpointer
from cloudops_connectors.telemetry.pipeline import TelemetryPipeline
from cloudops_connectors.telemetry.resolve import Resolver
from cloudops_tenancy import Database
from cloudops_tenancy.telemetry import TelemetryStore

CAPTURE_CONTAINER = "evh-metrics-capture"


async def _run() -> int:
    settings = load_settings(os.environ, require={Component.DATABASE_INGEST, Component.TELEMETRY,
                                                  Component.BLOB})
    if settings.environment is not Environment.LOCAL:
        raise ConfigurationError("--fixtures is local-only")
    cfg = settings.telemetry
    async with AsyncExitStack() as stack:
        db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=2)
        stack.push_async_callback(db.dispose)
        store = TelemetryStore.create(cfg.url, cfg.database, cfg.username,
                                      settings.secrets.get(cfg.password_secret))
        stack.push_async_callback(store.aclose)
        container = ContainerClient.from_connection_string(AZURITE, CAPTURE_CONTAINER)
        stack.push_async_callback(container.close)
        with suppress(ResourceExistsError):
            await container.create_container()
        hub = InMemoryHub(8, capture=Capture(BlobRawStore(container)))
        # Local profile only: the generator re-times the recorded events, and
        # devdata is a development package, never imported on a deployed path.
        from cloudops_devdata.telemetry_fixtures import build

        anchor = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=5)
        events, _ = build(anchor)
        for e in events:
            await hub.publish(e["body"].encode(), e["partition_key"])
        pipeline = TelemetryPipeline(db, store, Resolver(db))
        counts = await hub.consume(pipeline, MemoryCheckpointer())
        await pipeline.flush()
        print(json.dumps(asdict(counts), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_connectors.telemetry")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--fixtures", action="store_true", help="publish the recorded events (local)")
    args = parser.parse_args(argv)
    try:
        if not args.fixtures:
            raise NoCredentialPathError(
                "telemetry has no Azure path yet: the telemetry store is LLD open item L1. "
                "Run with --fixtures in the local profile.")
        return asyncio.run(_run())
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
