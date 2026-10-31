"""Run the Azure connector (fixtures only until credentials exist).

    python -m cloudops_connectors.azure run --fixtures [--entity resource|cost_export|...]
    python -m cloudops_connectors.azure schedule

See ``core/cli.py``. The billing fixtures were recorded on ``BILLING_DAY``, so
the fixture source runs its billing entities as of that day.
"""

from __future__ import annotations

from datetime import date, timedelta

from cloudops_connectors.azure.blob import BlobClient
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import (
    BILLING_ACCOUNT,
    BILLING_DAY,
    BLOB_HOST,
    FIXTURE_HOST,
    FakeAzure,
    FakeBlobStore,
)
from cloudops_connectors.azure.mapping import COST_EXPORT, COST_TOTAL, ENTITY, INVOICE, SOURCE
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.cli import main


def _fixtures() -> AzureSource:
    fake = FakeAzure.from_fixtures()
    blobs = FakeBlobStore.from_fixtures()
    return AzureSource(AzureClient(FIXTURE_HOST, transport=fake.transport()),
                       exports=BlobClient(BLOB_HOST, transport=blobs.transport()),
                       export_roots=blobs.roots, billing_account=BILLING_ACCOUNT,
                       today=lambda: date.fromisoformat(BILLING_DAY))


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {ENTITY: timedelta(minutes=15),
                                   COST_EXPORT: timedelta(days=1),
                                   COST_TOTAL: timedelta(days=1),
                                   INVOICE: timedelta(days=1)}, _fixtures))
