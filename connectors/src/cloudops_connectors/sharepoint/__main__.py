"""Run the SharePoint connector: recorded fixtures, or live for granted folders.

    python -m cloudops_connectors.sharepoint register --fixtures
    python -m cloudops_connectors.sharepoint register --customer NAME --site-id ID --site-url URL
    python -m cloudops_connectors.sharepoint run --fixtures [--entity drive_item|folder_item]
    python -m cloudops_connectors.sharepoint worker            # Azure profile: the live path
    python -m cloudops_connectors.sharepoint schedule

``register`` records a site's Sites.Selected grant under its customer (refused,
and the name quarantined, when the name resolves to no tenant); ``--fixtures``
registers every fixture site. The rest is ``core/cli.py``.

The live path (ADR 0028) reads the folders in ``CLOUDOPS_SHAREPOINT_FOLDERS``, each
granted to ``app-cloudops-sharepoint`` on its own, as entity ``folder_item``. It
authenticates as that app registration with the pod's projected service account
token (a federated credential on the app; no secret anywhere), and only reads:
the app holds ListItems.SelectedOperations.Selected and each grant is ``read``.
A folder needs no ``register``: its name is the customer alias, mapped through the
customer mapping like any other source's.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import AsyncExitStack
from typing import Final

from cloudops_config import Component, ConfigurationError, DbLogin, Settings, load_settings
from cloudops_connectors.azure.auth import GRAPH_SCOPE, EntraAuth, workload_credential
from cloudops_connectors.core.cli import Live, main
from cloudops_connectors.core.sync import CUSTOMER_SYNC
from cloudops_connectors.graph.client import GRAPH, GraphClient
from cloudops_connectors.sharepoint.fake import (
    FIXTURE_GRAPH,
    FakeSharePoint,
    folder_scope,
    load_fixture,
    load_folders,
)
from cloudops_connectors.sharepoint.mapping import ENTITY, FOLDER_ENTITY, SOURCE
from cloudops_connectors.sharepoint.source import (
    SharePointSource,
    UnresolvedCustomerError,
    register_site,
)
from cloudops_tenancy import Database


def _fixtures() -> SharePointSource:
    fake = FakeSharePoint.from_fixtures()
    customers = [s["customer"] for s in load_fixture()["sites"]]
    lib = load_folders()
    scopes = [folder_scope(lib["drive_id"], f["folder"]["id"]) for f in lib["folders"]]
    return SharePointSource(GraphClient(FIXTURE_GRAPH, transport=fake.transport()), customers,
                            scopes)


def live_entities(settings: Settings) -> frozenset[str]:
    return frozenset({FOLDER_ENTITY})


def live_source(settings: Settings, stack: AsyncExitStack) -> SharePointSource:
    """The granted folders, as ``app-cloudops-sharepoint`` through workload identity."""
    identity = settings.workload_identity
    if identity is None:
        raise ConfigurationError("the live SharePoint path needs the pod's workload identity")
    config = settings.sharepoint
    credential = workload_credential(identity, client_id=config.client_id)
    stack.push_async_callback(credential.close)
    client = GraphClient(GRAPH, auth=EntraAuth(credential, GRAPH_SCOPE, GRAPH))
    stack.push_async_callback(client.aclose)
    return SharePointSource(client, folder_scopes=config.folders,
                            entities=live_entities(settings))


LIVE: Final = Live(frozenset({Component.SHAREPOINT}), live_entities, live_source)


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
    return main(SOURCE, {ENTITY: CUSTOMER_SYNC, FOLDER_ENTITY: CUSTOMER_SYNC},
                _fixtures, argv, live=LIVE)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
