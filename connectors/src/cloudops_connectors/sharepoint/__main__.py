"""Run the SharePoint connector (fixtures only until credentials exist).

    python -m cloudops_connectors.sharepoint register --fixtures
    python -m cloudops_connectors.sharepoint register --customer NAME --site-id ID --site-url URL
    python -m cloudops_connectors.sharepoint run --fixtures
    python -m cloudops_connectors.sharepoint schedule

``register`` records a site's Sites.Selected grant under its customer (refused,
and the name quarantined, when the name resolves to no tenant); ``--fixtures``
registers every fixture site. The rest is ``core/cli.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import timedelta

from cloudops_config import Component, ConfigurationError, DbLogin, load_settings
from cloudops_connectors.core.cli import main
from cloudops_connectors.graph.client import GraphClient
from cloudops_connectors.sharepoint.fake import FIXTURE_GRAPH, FakeSharePoint, load_fixture
from cloudops_connectors.sharepoint.mapping import ENTITY, SOURCE
from cloudops_connectors.sharepoint.source import (
    SharePointSource,
    UnresolvedCustomerError,
    register_site,
)
from cloudops_tenancy import Database


def _fixtures() -> SharePointSource:
    fake = FakeSharePoint.from_fixtures()
    customers = [s["customer"] for s in load_fixture()["sites"]]
    return SharePointSource(GraphClient(FIXTURE_GRAPH, transport=fake.transport()), customers)


async def _register(args: argparse.Namespace) -> int:
    settings = load_settings(os.environ, require={Component.DATABASE_INGEST})
    if args.fixtures:
        wanted = [(s["customer"], s["site_id"], s["site_url"]) for s in load_fixture()["sites"]]
    elif args.customer and args.site_id and args.site_url:
        wanted = [(args.customer, args.site_id, args.site_url)]
    else:
        print("error: --fixtures, or all of --customer, --site-id and --site-url",
              file=sys.stderr)
        return 2
    db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=1)
    status = 0
    try:
        for customer, site_id, site_url in wanted:
            try:
                await register_site(db, customer, site_id, site_url)
                print(f"registered {site_url}")
            except UnresolvedCustomerError:
                print(f"refused {site_url}: its customer is not mapped; quarantined for review",
                      file=sys.stderr)
                status = 1
    finally:
        await db.dispose()
    return status


def _main(argv: list[str]) -> int:
    if argv[:1] == ["register"]:
        parser = argparse.ArgumentParser(prog=f"python -m cloudops_connectors.{SOURCE} register")
        parser.add_argument("--fixtures", action="store_true")
        parser.add_argument("--customer")
        parser.add_argument("--site-id")
        parser.add_argument("--site-url")
        try:
            return asyncio.run(_register(parser.parse_args(argv[1:])))
        except ConfigurationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return main(SOURCE, {ENTITY: timedelta(days=1)}, _fixtures, argv)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
