"""Customer estates, read with each customer's own stored credential (ADR 0027).

    python -m cloudops_connectors.customer worker   # checks, workers, schedules
    python -m cloudops_connectors.customer check    # run waiting checks once, print them

Needs ``CLOUDOPS_CUSTOMER_CREDENTIALS=stored_key`` and, in Azure, a deployment that
is not ``prd`` (ADR 0003, guardrail 4: ``cloudops_config`` refuses it). Secrets are
read by name from the store: Key Vault through the CSI mount in Azure, ``.env.local``
on a laptop (``customer-<code>-azure-secret`` is ``CUSTOMER_<CODE>_AZURE_SECRET``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_config.probes import serve_probes
from cloudops_config.temporal import connect
from cloudops_connectors.core.cli import bronze_container
from cloudops_connectors.core.raw import BlobRawStore
from cloudops_connectors.customer.worker import CustomerWorker, report
from cloudops_observability import TelemetryConfigError
from cloudops_observability.process import start, temporal_options
from cloudops_tenancy import Database


async def _main(command: str) -> int:
    settings = load_settings(os.environ, require={
        Component.DATABASE_INGEST, Component.BLOB, Component.TEMPORAL,
        Component.CUSTOMER_ACCESS})
    async with AsyncExitStack() as stack:
        db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=4)
        stack.push_async_callback(db.dispose)
        container = bronze_container(settings)
        stack.push_async_callback(container.close)
        raw = BlobRawStore(container)
        if command == "check":
            worker = CustomerWorker(db, raw, None, settings.secrets)
            for r in await worker.run_checks():
                print(json.dumps(report(r), indent=2, sort_keys=True))
            return 0
        temporal = await connect(settings.temporal, **temporal_options())
        worker = CustomerWorker(db, raw, temporal, settings.secrets)

        async def temporal_ready() -> None:
            if not await temporal.service_client.check_health():
                raise ConnectionError("temporal unhealthy")

        async def bronze_ready() -> None:
            await container.get_container_properties()

        local = settings.environment is Environment.LOCAL
        probes = await serve_probes(
            {"database": db.ping, "temporal": temporal_ready, "bronze": bronze_ready},
            host="127.0.0.1" if local else "0.0.0.0",  # noqa: S104 - the kubelet probes the pod IP
            port=int(os.environ.get("CLOUDOPS_PROBE_PORT", "8080")),
        )

        async def close() -> None:
            probes.close()
            await probes.wait_closed()

        stack.push_async_callback(close)
        print("customer worker serving")
        await worker.serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_connectors.customer")
    parser.add_argument("command", choices=["worker", "check"])
    args = parser.parse_args(argv)
    try:
        telemetry = start("connector-customer", os.environ)
        try:
            return asyncio.run(_main(args.command))
        finally:
            telemetry.shutdown()
    except (ConfigurationError, TelemetryConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
