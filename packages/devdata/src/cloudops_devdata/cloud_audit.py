"""Cloud Services audit checks for the local console (ADR 0038).

The audit connector's own source, bound to the synthetic subscription the audit
fixtures describe, run page by page over the made-up fixtures (the activities the
workflow would run, without Temporal). Its tenant is whichever the synthetic
dataset gives that subscription. Local only: ``seed.run`` refuses Azure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import FIXTURE_HOST, FakeAzure
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.identity import Binding
from cloudops_connectors.core.ingest import ConnectorActivities, FetchRequest, ProcessRequest
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_tenancy import Database

# The subscription the audit fixtures (azure/fixtures/audit) are written for.
SUBSCRIPTION: Final = "713eb261-d613-543b-bfa9-f71bb3e74b14"
CREDENTIAL: Final = "5eed0000-0000-4000-8000-00000000a0d1"
SINCE: Final = "2026-01-01T00:00:00+00:00"


async def _nothing(_: float) -> None:
    return None


async def seed(ingest: Database, tenant: UUID) -> int:
    """Run the five audit entities once; the number of checks written."""
    fake = FakeAzure.from_fixtures()
    client = AzureClient(FIXTURE_HOST, transport=fake.transport(), sleep=_nothing,
                         allowed=frozenset({SUBSCRIPTION}))
    source = AzureSource(client, entities=mapping.AUDIT,
                         binding=Binding(tenant, frozenset({SUBSCRIPTION})), credential=CREDENTIAL)
    acts = ConnectorActivities(ingest, MemoryRawStore(), source)
    started = datetime.now(UTC).isoformat()
    written = 0
    try:
        for entity in sorted(mapping.AUDIT):
            run_id = f"seed-audit-{entity}"
            refs: list[str] = []
            cursor: str | None = None
            while True:
                page = await acts.fetch_page(FetchRequest(run_id, entity, "", SINCE, cursor,
                                                          len(refs)))
                refs.append(page.raw_ref)
                cursor = page.next_cursor
                if cursor is None:
                    break
            for i, ref in enumerate(refs):
                outcome = await acts.process(ProcessRequest(
                    entity, ref, run_id, started, advance=True, final=i == len(refs) - 1,
                    scope=""))
                written += outcome.counts.written
    finally:
        await client.aclose()
    return written
