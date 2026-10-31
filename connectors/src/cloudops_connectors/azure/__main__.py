"""Run the Azure connector (fixtures only until credentials exist).

    python -m cloudops_connectors.azure run --fixtures
    python -m cloudops_connectors.azure schedule

See ``core/cli.py``.
"""

from __future__ import annotations

from datetime import timedelta

from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import FIXTURE_HOST, FakeAzure
from cloudops_connectors.azure.mapping import ENTITY, SOURCE
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.cli import main


def _fixtures() -> AzureSource:
    fake = FakeAzure.from_fixtures()
    return AzureSource(AzureClient(FIXTURE_HOST, transport=fake.transport()))


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {ENTITY: timedelta(minutes=15)}, _fixtures))
