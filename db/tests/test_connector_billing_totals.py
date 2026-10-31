"""Provider totals and invoices end to end, for AWS (Cost Explorer, Invoicing)
and Azure (Cost Management query, Billing invoices): the generic connector
workflow on a time-skipping Temporal server, the migrated database (a module of
its own: the synthetic tenants, aliases, accounts and inventory, and no cost),
and the recorded responses served by the fakes through the real clients.

Ground truth comes from ``cloudops_devdata.aws_fixtures.billing_expectations``
and ``azure_fixtures.billing_expectations``: every figure, its customer (or
quarantine), and what the line items say for the same account and period. The
line items are ingested too, so the drift reconciliation will see is checked
against ``cost_fact`` itself: within 0.5% everywhere but one account per
provider, in two currencies each.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.aws import mapping as aws_mapping
from cloudops_connectors.aws.billing_source import PayerBilling
from cloudops_connectors.aws.client import (
    ConfigClient,
    CostExplorerClient,
    InvoicingClient,
    S3Client,
)
from cloudops_connectors.aws.fake import (
    AGGREGATOR,
    BILLING_DAY,
    REGION,
    FakeConfig,
    FakeCostExplorer,
    FakeInvoicing,
    FakeS3,
)
from cloudops_connectors.aws.source import AwsSource
from cloudops_connectors.azure import mapping as az_mapping
from cloudops_connectors.azure.blob import BlobClient
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import BILLING_ACCOUNT, FakeAzure, FakeBlobStore
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    ReplayRequest,
    Source,
    scope_key,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_devdata import aws_fixtures, azure_fixtures
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

DAY = date.fromisoformat(BILLING_DAY)
PAYERS = ("900000000001", "900000000002")
_TOTALS = ("SELECT a.provider, a.native_id, b.kind, b.invoice_period, b.as_of, b.amount,"
           " b.currency, b.document_ref, b.tenant_id, b.source_system, b.raw_extra"
           " FROM cloudops.billing_total b JOIN cloudops.cloud_account a ON a.id = b.account_id")
_LEDGER = ("SELECT sum(f.unblended_cost) FROM cloudops.cost_fact f"
           " JOIN cloudops.cloud_account a ON a.id = f.account_id"
           " WHERE a.native_id = %s AND f.invoice_period = %s AND f.usage_date <= %s")


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    _clear(synthetic_module.admin_dsn)
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _clear(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as cx:
        for table in ("cloudops.billing_total", "cloudops.cost_fact", "staging.cost_fact_load",
                      "staging.cur_assembly", "staging.tenant_alias_quarantine",
                      "staging.resource_quarantine", "staging.schema_field",
                      "staging.ingest_dead_letter"):
            cx.execute(f"DELETE FROM {table}")


def _instance() -> str:
    return f"https://t{uuid.uuid4().hex[:8]}.example"  # its own watermarks


def _aws(ces: dict[str, FakeCostExplorer] | None = None,
         invs: dict[str, FakeInvoicing] | None = None, s3: FakeS3 | None = None) -> AwsSource:
    ces = ces or {p: FakeCostExplorer.from_fixtures(p) for p in PAYERS}
    invs = invs or {p: FakeInvoicing.from_fixtures(p) for p in PAYERS}
    s3 = s3 or FakeS3.from_fixtures()
    return AwsSource(
        ConfigClient(REGION, AGGREGATOR, transport=FakeConfig.from_fixtures().transport(),
                     sleep=no_sleep),
        S3Client(s3.bucket, REGION, transport=s3.transport(), sleep=no_sleep), s3.reports,
        instance=_instance(),
        billing=[PayerBilling(p, CostExplorerClient(p, transport=ces[p].transport(),
                                                    sleep=no_sleep),
                              InvoicingClient(p, transport=invs[p].transport(), sleep=no_sleep))
                 for p in PAYERS],
        today=lambda: DAY)


def _azure(fake: FakeAzure | None = None, blobs: FakeBlobStore | None = None) -> AzureSource:
    blobs = blobs or FakeBlobStore.from_fixtures()
    return AzureSource(
        AzureClient(_instance(), transport=(fake or FakeAzure.from_fixtures()).transport(),
                    sleep=no_sleep),
        exports=BlobClient("https://exports.example", transport=blobs.transport(),
                           sleep=no_sleep),
        export_roots=blobs.roots, billing_account=BILLING_ACCOUNT, today=lambda: DAY)


def _stored(dsn: str, provider: str) -> dict[tuple[str, str, str, date], tuple[Any, ...]]:
    out: dict[tuple[str, str, str, date], tuple[Any, ...]] = {}
    for r in rows(dsn, _TOTALS + " WHERE a.provider = %s", provider):
        key = (r[1], r[2], r[3], r[4])
        assert key not in out
        out[key] = r
    return out


def _mark(dsn: str, source: Source, entity: str, scope: str) -> Any:
    found = rows(dsn, "SELECT watermark FROM staging.ingest_watermark"
                      " WHERE source_system = %s AND entity = %s AND scope_key = %s",
                 source.source, entity, scope_key(source.instance, scope))
    return found[0][0] if found else None


async def _ingest(env: WorkflowEnvironment, acts: ConnectorActivities, *entities: str
                  ) -> list[Any]:
    return await run(env, acts, ConnectorIngest.run, [IngestRequest(e, SINCE) for e in entities])


def _drift(reference: Decimal, ledger: Decimal) -> Decimal:
    return abs(reference - ledger) / ledger


# --- AWS --------------------------------------------------------------------------------------


async def test_aws_totals_and_invoices_land_where_the_ground_truth_says(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    raw, s3 = MemoryRawStore(), FakeS3.from_fixtures()
    source = _aws(s3=s3)
    acts = ConnectorActivities(db, raw, source)
    await _ingest(env, acts, aws_mapping.CUR)  # the line items, to reconcile against
    s3.advance()
    _, total, invoice = await _ingest(env, acts, aws_mapping.CUR, aws_mapping.COST_TOTAL,
                                      aws_mapping.INVOICE)
    truth = aws_fixtures.billing_expectations()
    mapped = [e for e in truth if e.tenant_code is not None]
    held = [e for e in truth if e.tenant_code is None]
    assert (total.status, invoice.status) == ("succeeded", "succeeded")
    assert total.counts.written + invoice.counts.written == len(mapped)
    assert total.counts.quarantined + invoice.counts.quarantined == len(held)

    stored = _stored(dsn, "aws")
    assert set(stored) == {(e.account, e.kind, e.invoice_period, e.as_of) for e in mapped}
    for e in mapped:
        r = stored[(e.account, e.kind, e.invoice_period, e.as_of)]
        assert (r[5], r[6], r[7]) == (e.amount, e.currency, e.document_ref)
        assert isinstance(r[5], Decimal)
        assert (r[8], r[9]) == (tenant_id(e.tenant_code or ""), "aws")
    assert {r[6] for r in stored.values()} == {"USD", "EUR"}

    # What reconciliation will see: cost_fact against the reference, per account.
    for e in mapped:
        ((ledger,),) = rows(dsn, _LEDGER, e.account, e.invoice_period, e.as_of)
        assert ledger == e.ledger, e
        assert (_drift(e.amount, ledger) > Decimal("0.005")) == (
            e.tenant_code == "FAB" and e.kind == "provider_total"), e

    # The unregistered account: quarantined with every sighting, nothing written.
    assert dict(rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                          " WHERE source_system = 'aws' AND alias = %s",
                     aws_fixtures.UNREGISTERED))[aws_fixtures.UNREGISTERED] >= len(held)
    assert raw_first(raw.events)
    for p in PAYERS:
        assert _mark(dsn, source, aws_mapping.COST_TOTAL, p) == datetime(2026, 10, 27,
                                                                         tzinfo=UTC)
    assert _mark(dsn, source, aws_mapping.INVOICE, PAYERS[0]) == datetime(2026, 10, 2,
                                                                          tzinfo=UTC)
    assert _mark(dsn, source, aws_mapping.INVOICE, PAYERS[1]) is None, "nothing issued"


async def test_aws_rereads_and_replays_rewrite_the_same_rows_and_a_new_figure_updates(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    raw = MemoryRawStore()
    ces = {p: FakeCostExplorer.from_fixtures(p) for p in PAYERS}
    source = _aws(ces=ces)
    acts = ConnectorActivities(db, raw, source)
    await _ingest(env, acts, aws_mapping.COST_TOTAL, aws_mapping.INVOICE)
    snapshot = rows(dsn, "SELECT id, amount, currency FROM cloudops.billing_total ORDER BY id")
    marks = [_mark(dsn, source, aws_mapping.COST_TOTAL, p) for p in PAYERS]
    await _ingest(env, acts, aws_mapping.COST_TOTAL, aws_mapping.INVOICE)
    assert rows(dsn, "SELECT id, amount, currency FROM cloudops.billing_total ORDER BY id"
                ) == snapshot, "the same day read again: the same rows"
    requests = sum(len(c.requests) for c in ces.values())
    on = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run,
                          [ReplayRequest(aws_mapping.COST_TOTAL, on, on)])
    assert replay.status == "succeeded"
    assert sum(len(c.requests) for c in ces.values()) == requests, "replay never fetches"
    assert rows(dsn, "SELECT id, amount, currency FROM cloudops.billing_total ORDER BY id"
                ) == snapshot
    assert [_mark(dsn, source, aws_mapping.COST_TOTAL, p) for p in PAYERS] == marks

    # Cost Explorer revises its estimate for the same day: the row is updated.
    usd = ces[PAYERS[0]]
    group = usd.responses[f"2026-10-01/{BILLING_DAY}"]["ResultsByTime"][0]["Groups"][0]
    group["Metrics"]["UnblendedCost"]["Amount"] = "1.234567"
    await _ingest(env, acts, aws_mapping.COST_TOTAL)
    assert rows(dsn, "SELECT b.amount, count(*) OVER () FROM cloudops.billing_total b"
                     " JOIN cloudops.cloud_account a ON a.id = b.account_id"
                     " WHERE a.native_id = %s AND b.kind = 'provider_total'",
                group["Keys"][0]) == [(Decimal("1.234567"), 1)]


async def test_a_response_that_does_not_parse_dead_letters_in_bronze(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    ces = {p: FakeCostExplorer.from_fixtures(p) for p in PAYERS}
    ces[PAYERS[0]].responses[f"2026-10-01/{BILLING_DAY}"] = {"ResultsByTime": "maintenance"}
    raw = MemoryRawStore()
    source = _aws(ces=ces)
    (report,) = await _ingest(env, ConnectorActivities(db, raw, source), aws_mapping.COST_TOTAL)
    assert {s.scope: s.status for s in report.scopes} == {
        PAYERS[0]: "dead_lettered", PAYERS[1]: "succeeded"}, "one payer's bad page stops only it"
    ((ref,),) = rows(dsn, "SELECT raw_ref FROM staging.ingest_dead_letter"
                          " WHERE source_system = 'aws' AND entity = 'cost_total'")
    assert b"maintenance" in raw.pages[ref], "the page is kept in bronze as it came"
    assert _mark(dsn, source, aws_mapping.COST_TOTAL, PAYERS[0]) is None
    assert rows(dsn, "SELECT DISTINCT currency FROM cloudops.billing_total") == [("EUR",)]


# --- Azure ------------------------------------------------------------------------------------


async def test_azure_totals_and_invoices_land_where_the_ground_truth_says(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    raw, blobs = MemoryRawStore(), FakeBlobStore.from_fixtures()
    blobs.advance()
    source = _azure(blobs=blobs)
    acts = ConnectorActivities(db, raw, source)
    _, total, invoice = await _ingest(env, acts, az_mapping.COST_EXPORT, az_mapping.COST_TOTAL,
                                      az_mapping.INVOICE)
    truth = azure_fixtures.billing_expectations()
    mapped = [e for e in truth if e.tenant_code is not None]
    held = [e for e in truth if e.tenant_code is None]
    assert (total.status, invoice.status) == ("succeeded", "succeeded")
    assert total.counts.written + invoice.counts.written == len(mapped)
    assert total.counts.quarantined + invoice.counts.quarantined == len(held)

    stored = _stored(dsn, "azure")
    assert set(stored) == {(e.subscription, e.kind, e.invoice_period, e.as_of) for e in mapped}
    for e in mapped:
        r = stored[(e.subscription, e.kind, e.invoice_period, e.as_of)]
        assert (r[5], r[6], r[7]) == (e.amount, e.currency, e.document_ref)
        assert (r[8], r[9]) == (tenant_id(e.tenant_code or ""), "azure")
    assert {r[6] for r in stored.values()} == {"EUR", "GBP"}
    for e in mapped:
        ((ledger,),) = rows(dsn, _LEDGER, e.subscription, e.invoice_period, e.as_of)
        assert ledger == e.ledger, e
        assert (_drift(e.amount, ledger) > Decimal("0.005")) == (
            e.tenant_code == "CRL" and e.kind == "provider_total"), e

    assert rows(dsn, "SELECT occurrence_count FROM staging.tenant_alias_quarantine"
                     " WHERE source_system = 'azure' AND alias = %s",
                azure_fixtures.UNREGISTERED)[0][0] >= len(held)
    assert raw_first(raw.events)
    assert _mark(dsn, source, az_mapping.COST_TOTAL, "") == datetime(2026, 10, 28, tzinfo=UTC)
    assert _mark(dsn, source, az_mapping.INVOICE, "") == datetime(2026, 10, 6, tzinfo=UTC)


async def test_azure_rerun_and_replay_rewrite_the_same_rows(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    raw, fake = MemoryRawStore(), FakeAzure.from_fixtures()
    source = _azure(fake)
    acts = ConnectorActivities(db, raw, source)
    await _ingest(env, acts, az_mapping.COST_TOTAL, az_mapping.INVOICE)
    snapshot = rows(dsn, "SELECT id, amount, currency FROM cloudops.billing_total ORDER BY id")
    assert snapshot
    await _ingest(env, acts, az_mapping.COST_TOTAL, az_mapping.INVOICE)
    requests = len(fake.requests)
    on = "-".join(next(iter(raw.pages)).split("/")[2:5])
    for entity in (az_mapping.COST_TOTAL, az_mapping.INVOICE):
        (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(entity, on, on)])
        assert replay.status == "succeeded"
    assert len(fake.requests) == requests
    assert rows(dsn, "SELECT id, amount, currency FROM cloudops.billing_total ORDER BY id"
                ) == snapshot


# --- isolation --------------------------------------------------------------------------------


@pytest.mark.isolation
async def test_an_account_never_writes_a_total_under_another_customer(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    """If Fabrikam's AWS account's alias names Northwind, the account is still
    Fabrikam's: the page fails and dead-letters, and no figure for it is written
    under either customer."""
    dsn = synthetic_module.admin_dsn
    fab_acct = next(e.account for e in aws_fixtures.billing_expectations()
                    if e.tenant_code == "FAB")
    fab, nwl = tenant_id("FAB"), tenant_id("NWL")
    with psycopg.connect(dsn, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.tenant_alias SET tenant_id = %s"
                   " WHERE source_system = 'aws' AND alias = %s", (nwl, fab_acct))
    try:
        source = _aws()
        (report,) = await _ingest(env, ConnectorActivities(db, MemoryRawStore(), source),
                                  aws_mapping.COST_TOTAL)
        assert {s.scope: s.status for s in report.scopes} == {
            PAYERS[0]: "dead_lettered", PAYERS[1]: "succeeded"}
        assert rows(dsn, "SELECT count(*) FROM cloudops.billing_total b"
                         " JOIN cloudops.cloud_account a ON a.id = b.account_id"
                         " WHERE a.native_id = %s", fab_acct) == [(0,)]
        assert rows(dsn, "SELECT count(*) FROM cloudops.billing_total"
                         " WHERE tenant_id = ANY(%s)", [fab, nwl]) == [(0,)]
        assert _mark(dsn, source, aws_mapping.COST_TOTAL, PAYERS[0]) is None
    finally:
        with psycopg.connect(dsn, autocommit=True) as cx:
            cx.execute("UPDATE cloudops.tenant_alias SET tenant_id = %s"
                       " WHERE source_system = 'aws' AND alias = %s", (fab, fab_acct))


@pytest.mark.isolation
async def test_one_customers_totals_are_invisible_to_another_and_to_other_accounts(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    await _ingest(env, ConnectorActivities(db, MemoryRawStore(), _azure()),
                  az_mapping.COST_TOTAL, az_mapping.INVOICE)
    await _ingest(env, ConnectorActivities(db, MemoryRawStore(), _aws()),
                  aws_mapping.COST_TOTAL, aws_mapping.INVOICE)
    nwl, crl = tenant_id("NWL"), tenant_id("CRL")
    nwl_accounts = {r[0]: r[1] for r in rows(
        dsn, "SELECT a.native_id, a.id FROM cloudops.cloud_account a"
             " WHERE a.tenant_id = %s AND EXISTS (SELECT 1 FROM cloudops.billing_total b"
             " WHERE b.account_id = a.id)", nwl)}
    assert len(nwl_accounts) == 2, "Northwind has an AWS and an Azure figure"
    app = Database.create(synthetic_module.app_dsn, pool_size=1, max_overflow=0)
    count = text("SELECT count(*) FROM cloudops.billing_total WHERE tenant_id = :t")
    try:
        async with app.transaction(TenantScope({crl}, Role.FINOPS, "test:crl")) as conn:
            assert await conn.scalar(count, {"t": nwl}) == 0
            assert await conn.scalar(count, {"t": crl}) > 0
            assert {str(r[0]) for r in await conn.execute(text(
                "SELECT DISTINCT currency FROM cloudops.billing_total"))} == {"GBP"}
        async with app.transaction(TenantScope((), Role.FINOPS, "test:none")) as conn:
            assert await conn.scalar(text("SELECT count(*) FROM cloudops.billing_total")) == 0
        # A subscription-limited scope sees that account's figures only.
        one = next(iter(nwl_accounts.values()))
        async with app.transaction(TenantScope({nwl}, Role.FINOPS, "test:acct",
                                               account_ids=[one])) as conn:
            seen = {r[0] for r in await conn.execute(text(
                "SELECT DISTINCT account_id FROM cloudops.billing_total"))}
            assert seen == {one}
    finally:
        await app.dispose()
