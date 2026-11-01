"""A customer's own credential, end to end (ADR 0027, ADR 0003): the ordinary
connector workflow over an Azure source bound to one stored credential, on the
synthetic dataset, with the recorded responses served through the real client.

The case that matters is the dev subscription: Azure Sponsorship's, its single
owner (ADR 0031), read by two feeds. The platform's own (the workload identity,
ADR 0023) reads its cost, through the alias, into its owner; Sponsorship's app
registration reads its resources and, for that subscription, no cost: one cost
feed per subscription, so nothing is counted twice. Every call the bound client
makes names only the credential's subscription, and the owner reconciles on its
one ledger.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import BILLING_DAY, FakeAzure
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.identity import Binding, TargetOutOfScopeError
from cloudops_connectors.core.ingest import ConnectorActivities, ConnectorIngest, IngestRequest
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.finops import jobs
from cloudops_devdata.azure_fixtures import daily_expectations
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, rows, run

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

CUSTOMER = uuid.UUID("0c0ffee0-0000-4000-8000-00000000a25b")
CREDENTIAL = "5ca1ab1e-0000-4000-8000-000000000001"


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(scope="module")
def shared(synthetic_module: Synthetic) -> tuple[str, UUID]:
    """A mapped subscription handed to Azure Sponsorship, its one owner: the old
    tenant's account goes, and the alias (the platform feed's route) moves."""
    truth = daily_expectations()
    sub = sorted(s for s, code in truth.tenant.items() if code is not None)[0]
    before = tenant_id(str(truth.tenant[sub]))
    with psycopg.connect(synthetic_module.admin_dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code, internal)"
                   " VALUES (%s, 'Azure Sponsorship', 'AZSPONSOR', true)", (CUSTOMER,))
        cx.execute("DELETE FROM cloudops.cloud_account WHERE native_id = %s", (sub,))
        cx.execute("UPDATE cloudops.tenant_alias SET tenant_id = %s"
                   " WHERE source_system = 'azure' AND alias = %s", (CUSTOMER, sub))
    return sub, before


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    with psycopg.connect(synthetic_module.admin_dsn, autocommit=True) as cx:
        for table in ("cloudops.cost_fact", "staging.cost_fact_load", "staging.cur_assembly",
                      "cloudops.billing_total", "staging.tenant_alias_quarantine"):
            cx.execute(f"DELETE FROM {table}")
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _platform(fake: FakeAzure) -> AzureSource:
    base = f"https://p{uuid.uuid4().hex[:8]}.management.example"
    return AzureSource(AzureClient(base, transport=fake.transport(), sleep=no_sleep),
                       today=lambda: date.fromisoformat(BILLING_DAY))


def _customer(fake: FakeAzure, sub: str) -> AzureSource:
    """As ``customer.sources`` builds it for a subscription the platform's feed reads
    the cost of: resources only."""
    base = f"https://c{uuid.uuid4().hex[:8]}.management.example"
    client = AzureClient(base, transport=fake.transport(), sleep=no_sleep,
                         allowed=frozenset({sub}))
    return AzureSource(client, today=lambda: date.fromisoformat(BILLING_DAY),
                       entities=frozenset({mapping.ENTITY}),
                       binding=Binding(CUSTOMER, frozenset({sub})), credential=CREDENTIAL)


def _named(fake: FakeAzure, start: int) -> set[str]:
    """Subscriptions named by the calls made since ``start`` (path or body)."""
    named: set[str] = set()
    for r in fake.requests[start:]:
        parts = r.url.path.split("/")
        if len(parts) > 2 and parts[1] == "subscriptions":
            named.add(parts[2])
        if r.content:
            body = json.loads(r.content)
            if isinstance(body, dict):
                named |= set(body.get("subscriptions") or [])
    return named


def _cost_by_tenant(dsn: str, sub: str) -> dict[Any, list[tuple[Any, ...]]]:
    out: dict[Any, list[tuple[Any, ...]]] = {}
    for r in rows(dsn, "SELECT f.tenant_id, f.usage_date, f.service_native,"
                       " f.charge_class::text, f.unblended_cost, f.currency"
                       " FROM cloudops.cost_fact f JOIN cloudops.cloud_account a"
                       " ON a.id = f.account_id WHERE a.native_id = %s ORDER BY 2, 3, 4", sub):
        out.setdefault(r[0], []).append(tuple(r[1:]))
    return out


async def test_one_subscription_one_owner_one_cost_feed(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic,
    shared: tuple[str, UUID],
) -> None:
    dsn = synthetic_module.admin_dsn
    sub, before = shared
    fake, raw = FakeAzure.from_fixtures(), MemoryRawStore()  # one bronze, as in Azure
    customer = _customer(fake, sub)
    for source, entity in ((_platform(fake), mapping.COST_DAILY), (customer, mapping.ENTITY)):
        start = len(fake.requests)
        (report,) = await run(env, ConnectorActivities(db, raw, source),
                              ConnectorIngest.run, [IngestRequest(entity, SINCE)])
        assert report.status == "succeeded"
        if source is customer:
            assert _named(fake, start) == {sub}  # the listing names none; the rest only it
    # The credential's source refuses cost for it before any request.
    start = len(fake.requests)
    with pytest.raises(ValueError, match="unknown entity"):
        await customer.scopes(db, mapping.COST_DAILY)
    assert len(fake.requests) == start
    # The cost is the owner's, once; no other tenant holds the subscription.
    by_tenant = _cost_by_tenant(dsn, sub)
    assert set(by_tenant) == {CUSTOMER}
    assert by_tenant[CUSTOMER]
    assert rows(dsn, "SELECT tenant_id FROM cloudops.cloud_account WHERE native_id = %s",
                sub) == [(CUSTOMER,)]
    assert before != CUSTOMER
    # The credential read nothing else: no other subscription under it.
    assert rows(dsn, "SELECT DISTINCT a.native_id FROM cloudops.cost_fact f JOIN"
                     " cloudops.cloud_account a ON a.id = f.account_id"
                     " WHERE f.tenant_id = %s", CUSTOMER) == [(sub,)]


async def test_the_owner_reconciles_on_its_one_ledger(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic,
    shared: tuple[str, UUID],
) -> None:
    dsn = synthetic_module.admin_dsn
    sub, _ = shared
    fake, raw = FakeAzure.from_fixtures(), MemoryRawStore()
    reports = await run(env, ConnectorActivities(db, raw, _platform(fake)),
                        ConnectorIngest.run, [IngestRequest(mapping.COST_DAILY, SINCE),
                                              IngestRequest(mapping.COST_TOTAL, SINCE)])
    assert {r.status for r in reports} == {"succeeded"}
    totals = rows(dsn, "SELECT b.tenant_id FROM cloudops.billing_total b"
                       " JOIN cloudops.cloud_account a ON a.id = b.account_id"
                       " WHERE a.native_id = %s AND b.kind = 'provider_total'", sub)
    assert totals == [(CUSTOMER,)]
    await jobs.reconcile(db, date.fromisoformat(BILLING_DAY))
    checks = rows(dsn, "SELECT r.tenant_id, r.ledger_amount FROM cloudops.cost_reconciliation r"
                       " JOIN cloudops.cloud_account a ON a.id = r.account_id"
                       " WHERE a.native_id = %s AND r.kind = 'provider_total'"
                       " AND r.invoice_period = %s", sub, BILLING_DAY[:7])
    assert [c[0] for c in checks] == [CUSTOMER]
    own = sum((Decimal(r[3]) for r in _cost_by_tenant(dsn, sub)[CUSTOMER]), Decimal(0))
    assert Decimal(checks[0][1]) == own


async def test_the_bound_listing_quarantines_what_the_credential_was_not_onboarded_for(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic,
    shared: tuple[str, UUID],
) -> None:
    dsn = synthetic_module.admin_dsn
    sub, _ = shared
    fake = FakeAzure.from_fixtures()
    (report,) = await run(env, ConnectorActivities(db, MemoryRawStore(), _customer(fake, sub)),
                          ConnectorIngest.run, [IngestRequest(mapping.ENTITY, SINCE)])
    assert report.status == "succeeded"
    listed = {str(s["subscriptionId"]) for s in fake.subscriptions}
    graph = [json.loads(r.content) for r in fake.requests if r.url.path.endswith("/resources")]
    assert graph
    assert all(g["subscriptions"] == [sub] for g in graph)
    quarantined = {r[0] for r in rows(dsn, "SELECT alias FROM staging.tenant_alias_quarantine"
                                           " WHERE source_system = 'azure'")}
    assert quarantined == listed - {sub}
    assert {r[0] for r in rows(dsn, "SELECT DISTINCT a.native_id FROM cloudops.resource r"
                                    " JOIN cloudops.cloud_account a ON a.id = r.account_id"
                                    " WHERE r.tenant_id = %s", CUSTOMER)} <= {sub}


async def test_a_bound_client_refuses_any_other_subscription_before_calling() -> None:
    fake = FakeAzure.from_fixtures()
    client = AzureClient("https://x.management.example", transport=fake.transport(),
                         allowed=frozenset({"00000000-0000-4000-8000-000000000001"}))
    other = "00000000-0000-4000-8000-000000000002"
    for call in (client.cost_query(other), client.resources([other], None),
                 client.cost_daily(other, date(2026, 10, 1), date(2026, 10, 2)),
                 client.role_assignments(other, other)):
        with pytest.raises(TargetOutOfScopeError):
            await call
    assert fake.requests == []
    await client.aclose()
