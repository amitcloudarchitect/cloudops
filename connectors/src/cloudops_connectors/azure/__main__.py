"""Run the Azure connector: recorded fixtures, or live for the platform's own cost.

    python -m cloudops_connectors.azure run --fixtures [--entity resource|cost_export|...]
    python -m cloudops_connectors.azure worker            # Azure profile: the live path
    python -m cloudops_connectors.azure schedule

See ``core/cli.py``. The billing fixtures were recorded on ``BILLING_DAY``, so
the fixture source runs its billing entities as of that day.

The live path (ADR 0023) feeds the internal tenant: ``cost_total``, and line items
from either the Cost Management exports of the platform's own subscriptions
(``cost_export``) or, where the agreement allows no export, the query API's daily
cost (``cost_daily``), as ``CLOUDOPS_AZURE_COST_LINES`` says. Read with the pod's
workload identity (``azure/auth.py``). What it can reach is what stack 04b grants the
ingest identity; which tenant a subscription's cost lands in is the alias
mapping's (ADR 0016), as for every other source. ``resource`` and ``invoice``
stay fixtures-only here; a customer's subscriptions, read with its own stored
credential (ADR 0003, ADR 0027), are ``cloudops_connectors.customer``'s.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import date, timedelta
from typing import Final

from cloudops_config import Component, ConfigurationError, Settings
from cloudops_connectors.azure.auth import ARM_SCOPE, STORAGE_SCOPE, EntraAuth, workload_credential
from cloudops_connectors.azure.blob import BlobClient
from cloudops_connectors.azure.client import ARM, AzureClient
from cloudops_connectors.azure.fake import (
    BILLING_ACCOUNT,
    BILLING_DAY,
    BLOB_HOST,
    FIXTURE_HOST,
    FakeAzure,
    FakeBlobStore,
)
from cloudops_connectors.azure.mapping import (
    COST_DAILY,
    COST_EXPORT,
    COST_TOTAL,
    ENTITY,
    INVOICE,
    SOURCE,
)
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.cli import Live, main

LINES: Final = {"export": COST_EXPORT, "query": COST_DAILY}


def live_entities(settings: Settings) -> frozenset[str]:
    """``cost_total``, and the line items from wherever the configuration says."""
    return frozenset({COST_TOTAL, LINES[settings.azure_cost.lines]})


def _fixtures() -> AzureSource:
    fake = FakeAzure.from_fixtures()
    blobs = FakeBlobStore.from_fixtures()
    return AzureSource(AzureClient(FIXTURE_HOST, transport=fake.transport()),
                       exports=BlobClient(BLOB_HOST, transport=blobs.transport()),
                       export_roots=blobs.roots, billing_account=BILLING_ACCOUNT,
                       today=lambda: date.fromisoformat(BILLING_DAY))


def live_source(settings: Settings, stack: AsyncExitStack) -> AzureSource:
    """The platform's own cost, with the workload identity the profile validated."""
    identity = settings.workload_identity
    if identity is None:
        raise ConfigurationError("the live Azure path needs the pod's workload identity")
    cost = settings.azure_cost
    credential = workload_credential(identity)
    stack.push_async_callback(credential.close)
    client = AzureClient(ARM, auth=EntraAuth(credential, ARM_SCOPE, ARM))
    stack.push_async_callback(client.aclose)
    if cost.export_account_url is None:  # query: no storage to read
        return AzureSource(client, entities=live_entities(settings))
    exports = BlobClient(cost.export_account_url,
                         auth=EntraAuth(credential, STORAGE_SCOPE, cost.export_account_url))
    stack.push_async_callback(exports.aclose)
    return AzureSource(client, exports=exports, export_roots=cost.export_roots,
                       entities=live_entities(settings))


LIVE: Final = Live(frozenset({Component.AZURE_COST}), live_entities, live_source)


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {ENTITY: timedelta(minutes=15),
                                   COST_EXPORT: timedelta(days=1),
                                   COST_TOTAL: timedelta(days=1),
                                   INVOICE: timedelta(days=1),
                                   COST_DAILY: timedelta(days=1)}, _fixtures, live=LIVE))
