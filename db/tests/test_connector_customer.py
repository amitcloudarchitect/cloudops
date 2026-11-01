"""A customer's own credential, end to end (ADR 0027, ADR 0003): the ordinary
connector workflow over an Azure source bound to one stored credential, on the
synthetic dataset, with the recorded responses served through the real client.

The case that matters is the dev subscription: the platform's own (the internal
tenant's, through its alias and the workload identity, ADR 0023) and also the
first customer's (through its app registration). Both runs read the same
subscription; each lands in its own tenant, neither supersedes the other, every
call the bound client makes names only the credential's subscription, and each
tenant reconciles on its own figures, never on the two added together.
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
    """A mapped subscription, its tenant made internal, and a customer tenant."""
    truth = daily_expectations()
    sub = sorted(s for s, code in truth.tenant.items() if code is not None)[0]
    internal = tenant_id(str(truth.tenant[sub]))
    with psycopg.connect(synthetic_module.admin_dsn, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.tenant SET internal = true WHERE id = %s", (internal,))
        cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code)"
                   " VALUES (%s, 'Azure Sponsorship', 'AZSPONSOR')", (CUSTOMER,))
    return sub, internal


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
    base = f"https://c{uuid.uuid4().hex[:8]}.management.example"
    client = AzureClient(base, transport=fake.transport(), sleep=no_sleep,
                         allowed=frozenset({sub}))
    return AzureSource(client, today=lambda: date.fromisoformat(BILLING_DAY),
                       entities=frozenset({mapping.ENTITY, mapping.COST_DAILY,
                                           mapping.COST_TOTAL}),
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


async def test_one_subscription_two_tenants_neither_supersedes_the_other(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic,
    shared: tuple[str, UUID],
) -> None:
    dsn = synthetic_module.admin_dsn
    sub, internal = shared
    fake, raw = FakeAzure.from_fixtures(), MemoryRawStore()  # one bronze, as in Azure
    for source in (_platform(fake), _customer(fake, sub), _platform(fake)):
        start = len(fake.requests)
        (report,) = await run(env, ConnectorActivities(db, raw, source),
                              ConnectorIngest.run, [IngestRequest(mapping.COST_DAILY, SINCE)])
        assert report.status == "succeeded"
        if source.instance.endswith(CREDENTIAL):
            assert _named(fake, start) == {sub}  # the listing names none; the rest only it
    by_tenant = _cost_by_tenant(dsn, sub)
    assert set(by_tenant) == {internal, CUSTOMER}
    assert by_tenant[internal] == by_tenant[CUSTOMER]
    assert by_tenant[CUSTOMER]
    # The customer's statement survives the platform's newer one, and the reverse.
    assert rows(dsn, "SELECT a.tenant_id, a.shared_with_platform FROM cloudops.cloud_account a"
                     " WHERE a.native_id = %s ORDER BY a.tenant_id = %s", sub, CUSTOMER) == [
        (internal, True), (CUSTOMER, True)]
    # The customer reads nothing else: no other subscription's cost under it.
    assert rows(dsn, "SELECT DISTINCT a.native_id FROM cloudops.cost_fact f JOIN"
                     " cloudops.cloud_account a ON a.id = f.account_id"
                     " WHERE f.tenant_id = %s", CUSTOMER) == [(sub,)]


async def test_a_newer_platform_statement_never_supersedes_the_customers(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic,
    shared: tuple[str, UUID],
) -> None:
    """Two schedules, one subscription: the platform's statement may be swapped in
    after the customer's run fetched its own. A statement is superseded only by
    a newer one of its own tenant (``AzureDaily._statement``)."""
    dsn = synthetic_module.admin_dsn
    sub, internal = shared
    fake, raw = FakeAzure.from_fixtures(), MemoryRawStore()
    (report,) = await run(env, ConnectorActivities(db, raw, _platform(fake)),
                          ConnectorIngest.run, [IngestRequest(mapping.COST_DAILY, SINCE)])
    assert report.status == "succeeded"
    for meta in raw.metadata.values():  # the platform's statements: newer than anything
        if "last_modified" in meta:
            meta["last_modified"] = "2099-01-01T00:00:00+00:00"
    (report,) = await run(env, ConnectorActivities(db, raw, _customer(fake, sub)),
                          ConnectorIngest.run, [IngestRequest(mapping.COST_DAILY, SINCE)])
    assert report.status == "succeeded"
    by_tenant = _cost_by_tenant(dsn, sub)
    assert by_tenant.get(CUSTOMER)
    assert by_tenant[CUSTOMER] == by_tenant[internal]


async def test_each_tenant_reconciles_on_its_own_figures(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic,
    shared: tuple[str, UUID],
) -> None:
    dsn = synthetic_module.admin_dsn
    sub, internal = shared
    fake, raw = FakeAzure.from_fixtures(), MemoryRawStore()
    for source in (_platform(fake), _customer(fake, sub)):
        reports = await run(env, ConnectorActivities(db, raw, source),
                            ConnectorIngest.run, [IngestRequest(mapping.COST_DAILY, SINCE),
                                                  IngestRequest(mapping.COST_TOTAL, SINCE)])
        assert {r.status for r in reports} == {"succeeded"}
    totals = rows(dsn, "SELECT b.tenant_id, b.amount, b.currency FROM cloudops.billing_total b"
                       " JOIN cloudops.cloud_account a ON a.id = b.account_id"
                       " WHERE a.native_id = %s AND b.kind = 'provider_total'"
                       " ORDER BY b.tenant_id = %s", sub, CUSTOMER)
    assert [t[0] for t in totals] == [internal, CUSTOMER]
    assert totals[0][1:] == totals[1][1:]  # the same provider figure, once per tenant
    await jobs.reconcile(db, date.fromisoformat(BILLING_DAY))
    checks = rows(dsn, "SELECT r.tenant_id, r.ledger_amount, r.reference_amount, r.status,"
                       " r.reason FROM cloudops.cost_reconciliation r JOIN cloudops.cloud_account"
                       " a ON a.id = r.account_id WHERE a.native_id = %s"
                       " AND r.kind = 'provider_total' AND r.invoice_period = %s"
                       " ORDER BY r.tenant_id = %s", sub, BILLING_DAY[:7], CUSTOMER)
    assert [c[0] for c in checks] == [internal, CUSTOMER]
    internal_check, customer_check = checks
    # Each ledger is its own tenant's rows only: equal, and never the two summed.
    assert internal_check[1:] == customer_check[1:]
    own = sum((Decimal(r[3]) for r in _cost_by_tenant(dsn, sub)[CUSTOMER]), Decimal(0))
    assert Decimal(customer_check[1]) == own


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
