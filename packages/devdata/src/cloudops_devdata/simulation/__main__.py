"""The end-to-end simulation for one test customer, "Azure Sponsorship".

    python -m cloudops_devdata.simulation write [--out build/simulation]
    python -m cloudops_devdata.simulation load                 # local profile only
    python -m cloudops_devdata.simulation qualys run --fixtures

``write`` renders the ServiceNow import files and the Qualys recorded responses
(no database, no credential). ``load`` puts the platform side in the local
database through ``packages/tenancy``. ``qualys`` runs the real step 15 Qualys
connector over the simulation's findings (its arguments are the connector's,
``core/cli.py``). All three take ``--estate`` (the Azure portal's VM export for
the subscription, a local file that is never committed), ``--anchor`` (the last
day of history, default yesterday UTC) and ``--seed`` (default 11); the same
three give the same output.
``docs/simulation/README.md`` is the walkthrough.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_devdata.seed import SeedError
from cloudops_devdata.simulation import load as loader
from cloudops_devdata.simulation import qualys
from cloudops_devdata.simulation.model import (
    DEFAULT_ESTATE,
    SHORT_CODE,
    Simulation,
    build,
    load_estate,
)
from cloudops_devdata.simulation.rows import platform_rows
from cloudops_devdata.simulation.servicenow import import_files
from cloudops_tenancy import Database

# Findings carry no tenant id: rendering them needs one only to build the rows.
_ANY_TENANT = UUID(int=0)


def write(sim: Simulation, out: Path) -> list[Path]:
    written = []
    sn = out / "servicenow"
    sn.mkdir(parents=True, exist_ok=True)
    for name, body in import_files(sim).items():
        path = sn / name
        path.write_text(body, encoding="utf-8", newline="")
        written.append(path)
    q = out / "qualys"
    q.mkdir(parents=True, exist_ok=True)
    (q / "host_detection.xml").write_bytes(
        qualys.detections(sim, platform_rows(sim, _ANY_TENANT)))
    (q / "knowledge_base.xml").write_bytes(qualys.knowledge_base())
    written += [q / "host_detection.xml", q / "knowledge_base.xml"]
    return written


async def _load(sim: Simulation) -> None:
    settings = load_settings(
        os.environ, require={Component.DATABASE_INGEST, Component.DATABASE_APP})
    if settings.environment is not Environment.LOCAL:
        raise SeedError("the simulation loads into the local database only")
    ingest = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=1)
    app = Database.create(settings.database_dsn(DbLogin.APP), pool_size=1)
    try:
        tid = await loader.find_tenant(ingest)
        if tid is None:
            admin = UUID(settings.secrets.get("platform-admin-oid"))
            tid = await loader.onboard(app, ingest, admin)
            print(f"  onboarded {SHORT_CODE}: {tid}")
        else:
            print(f"  found {SHORT_CODE}: {tid}")
        data = await loader.load(ingest, sim, tid)
        others = [t for t in await _all(ingest) if t != tid]
        await loader.verify(app, tid, data, others[0] if others else None)
        print("  " + ", ".join(f"{len(r)} {t.split('.')[1]}"
                               for t, r in data.by_table().items() if r))
        print("  isolation check: only the tenant's own scope sees its rows")
        if gap := await loader.fx_gap(ingest, sim):
            print(f"  warning: {gap} days without a USD to EUR rate; margin shows not "
                  "captured until they exist (`make seed` with the same --anchor loads them)")
    finally:
        await ingest.dispose()
        await app.dispose()


async def _all(ingest: Database) -> list[UUID]:
    from sqlalchemy import text

    async with ingest.transaction(loader._scope([])) as conn:
        return list(await conn.scalar(text("SELECT cloudops.finops_tenants()")) or [])


def _qualys(sim: Simulation, argv: list[str]) -> int:
    from cloudops_connectors.core.cli import main
    from cloudops_connectors.qualys.client import QualysClient
    from cloudops_connectors.qualys.fake import FIXTURE_HOST, FakeQualys
    from cloudops_connectors.qualys.mapping import ENTITY, SOURCE
    from cloudops_connectors.qualys.source import QualysSource

    detections = qualys.detections(sim, platform_rows(sim, _ANY_TENANT))
    kb = qualys.knowledge_base()

    def factory() -> QualysSource:
        fake = FakeQualys(detections, kb)
        return QualysSource(QualysClient(FIXTURE_HOST, transport=fake.transport()))

    return main(SOURCE, {ENTITY: timedelta(days=1)}, factory, argv)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_devdata.simulation")
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    parser.add_argument("--anchor", type=date.fromisoformat, default=yesterday,
                        help="last day of history (default: yesterday, UTC)")
    parser.add_argument("--seed", type=int, default=11, help="random seed (default 11)")
    parser.add_argument("--estate", type=Path, default=DEFAULT_ESTATE,
                        help="the Azure portal's VM export for the subscription (CSV; local "
                             f"only, never committed; default {DEFAULT_ESTATE})")
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write", help="write the ServiceNow import files and Qualys responses")
    w.add_argument("--out", type=Path, default=Path("build/simulation"))
    sub.add_parser("load", help="load the platform side into the local database")
    q = sub.add_parser("qualys", help="run the Qualys connector over the simulation's findings")
    q.add_argument("connector_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        sim = build(args.anchor, args.seed, load_estate(args.estate))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command == "write":
            for path in write(sim, args.out):
                print(f"  {path}")
        elif args.command == "load":
            print(f"loading the simulation: {sim.start} .. {sim.anchor}, seed {sim.seed}")
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined,unused-ignore]
            asyncio.run(_load(sim))
        else:
            return _qualys(sim, list(args.connector_args))
    except (ConfigurationError, SeedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
