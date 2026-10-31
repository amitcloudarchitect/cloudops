"""The Azure ``cost_daily`` entity end to end (ADR 0023): the generic connector
workflow on a time-skipping Temporal server, the migrated database with the
synthetic tenants, aliases and accounts, and the recorded query responses served
by the fake management endpoint through the real client.

Ground truth comes from ``cloudops_devdata.azure_fixtures.daily_expectations``:
every row, in full precision, in two currencies.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import BILLING_DAY, FakeAzure
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    ProcessRequest,
    ReplayRequest,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_devdata.azure_fixtures import UNREGISTERED, daily_expectations
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

ENTITY = mapping.COST_DAILY
SIX = Decimal("0.000001")
_COST = (
    "SELECT a.native_id, f.usage_date, f.service_native, f.charge_class::text,"
    " f.unblended_cost, f.amortized_cost, f.currency, f.invoice_period, f.source_batch,"
    " f.source_system, f.tenant_id, f.resource_id"
    " FROM cloudops.cost_fact f JOIN cloudops.cloud_account a ON a.id = f.account_id"
)
Key = tuple[str, date, str, str]


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    with psycopg.connect(synthetic_module.admin_dsn, autocommit=True) as cx:
        for table in ("cloudops.cost_fact", "staging.cost_fact_load", "staging.cur_assembly",
                      "staging.tenant_alias_quarantine", "staging.schema_field",
                      "staging.ingest_dead_letter"):
            cx.execute(f"DELETE FROM {table}")
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _source(fake: FakeAzure, day: str = BILLING_DAY) -> AzureSource:
    base = f"https://t{uuid.uuid4().hex[:8]}.management.example"  # its own watermarks
    return AzureSource(AzureClient(base, transport=fake.transport(), sleep=no_sleep),
                       today=lambda: date.fromisoformat(day))


def _stored(amount: Decimal) -> Decimal:
    return amount.quantize(SIX, rounding=ROUND_HALF_UP)


def _expected(months: set[str], rows_: dict[Any, Decimal] | None = None
              ) -> dict[Key, tuple[Decimal, str, str]]:
    truth = daily_expectations()
    out: dict[Key, tuple[Decimal, str, str]] = {}
    for k, amount in (rows_ or truth.rows).items():
        month = f"{k.usage_date:%Y-%m}"
        if truth.tenant[k.subscription] is None or month not in months:
            continue
        out[(k.subscription, k.usage_date, k.service, k.charge_class)] = (
            _stored(amount), truth.currency[k.subscription], month)
    return out


def _cost(dsn: str) -> dict[Key, tuple[Decimal, str, str]]:
    out: dict[Key, tuple[Decimal, str, str]] = {}
    for r in rows(dsn, _COST):
        key = (r[0], r[1], r[2], r[3])
        assert key not in out, "one row per day, service, class and currency"
        assert r[4] == r[5], "an actual-cost query has one figure"
        assert r[9] == "azure"
        assert r[11] is None, "the query is by service: no resource"
        out[key] = (r[4], r[6], r[7])
    return out


def _restated(fake: FakeAzure, sub: str, day: int, factor: str) -> None:
    """Azure restates a subscription: one day's costs scaled, the Purchase row gone."""
    doc = json.loads(fake.daily[sub], parse_float=str)
    kept = []
    for r in doc["properties"]["rows"]:
        if r[3] == "Purchase":
            continue
        if r[1] == int(f"202610{day:02d}") and r[0] is not None:
            r[0] = f"{Decimal(r[0]) * Decimal(factor):f}"
        kept.append(r)
    doc["properties"]["rows"] = kept
    fake.daily[sub] = re.sub(r'"(-?\d+\.\d+)"', r"\1", json.dumps(doc)).encode()


def _batches(dsn: str) -> list[tuple[Any, ...]]:
    return rows(dsn, "SELECT a.native_id, f.invoice_period, count(DISTINCT f.source_batch)"
                     " FROM cloudops.cost_fact f JOIN cloudops.cloud_account a"
                     " ON a.id = f.account_id GROUP BY 1, 2")


async def test_a_run_swaps_each_subscriptions_month_whole_in_two_currencies(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    truth = daily_expectations()
    raw = MemoryRawStore()
    (report,) = await run(env, ConnectorActivities(db, raw, _source(FakeAzure.from_fixtures())),
                          ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert report.status == "succeeded"
    assert _cost(dsn) == _expected({"2026-10"})
    assert {r[0] for r in rows(dsn, "SELECT DISTINCT currency FROM cloudops.cost_fact")} == {
        "EUR", "GBP"}
    assert all(n == 1 for *_, n in _batches(dsn))
    # Each row under its subscription's tenant.
    tenants = {r[0]: r[1] for r in rows(dsn, "SELECT native_id, tenant_id"
                                             " FROM cloudops.cloud_account")}
    for r in rows(dsn, _COST):
        assert r[10] == tenants[r[0]]
    # The classes the fixtures carry, every one from the fixed rule.
    assert {r[0] for r in rows(dsn, "SELECT DISTINCT charge_class::text"
                                    " FROM cloudops.cost_fact")} == {
        "consumption", "licence", "support", "credit", "reservation", "other"}
    # The unmapped subscription: every October row quarantined, nothing written.
    unknown = sum(1 for k in truth.rows if k.subscription == UNREGISTERED
                  and k.usage_date.month == 10)
    quarantined = dict(rows(dsn, "SELECT alias, occurrence_count"
                                 " FROM staging.tenant_alias_quarantine"
                                 " WHERE source_system = 'azure'"))
    assert quarantined[UNREGISTERED] >= unknown
    assert rows(dsn, "SELECT count(*) FROM cloudops.cloud_account WHERE native_id = %s",
                UNREGISTERED) == [(0,)]
    assert report.counts.rejected == sum(truth.malformed.values())
    assert rows(dsn, "SELECT count(*) FROM staging.cost_fact_load") == [(0,)]
    assert raw_first(raw.events)
    assert all(r.startswith("azure/cost_daily/") for r in raw.pages)


async def test_pages_make_the_same_statement_as_one(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    fake = FakeAzure.from_fixtures(daily_page=17)
    (report,) = await run(env, ConnectorActivities(db, MemoryRawStore(), _source(fake)),
                          ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert report.status == "succeeded"
    assert sum(1 for r in fake.requests if "$skiptoken" in r.url.params) > 4
    assert _cost(dsn) == _expected({"2026-10"})


async def test_early_in_a_month_the_last_month_is_restated_too(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    (report,) = await run(env, ConnectorActivities(
        db, MemoryRawStore(), _source(FakeAzure.from_fixtures(), "2026-10-03")),
        ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert report.status == "succeeded"
    got = _cost(dsn)
    assert {v[2] for v in got.values()} == {"2026-09", "2026-10"}
    assert {k: v for k, v in got.items() if v[2] == "2026-09"} == {
        k: v for k, v in _expected({"2026-09"}).items()}


async def test_a_restatement_replaces_the_month_exactly_and_replay_never_restores_the_old(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    truth = daily_expectations()
    raw, fake = MemoryRawStore(), FakeAzure.from_fixtures(daily_page=40)
    source = _source(fake)
    acts = ConnectorActivities(db, raw, source)
    (first,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    nwl_sub = next(s for s, c in truth.currency.items() if c == "EUR" and s != UNREGISTERED)
    _restated(fake, nwl_sub, 7, "1.25")
    (second,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert second.status == "succeeded"
    after = _cost(dsn)
    assert not any(k[0] == nwl_sub and k[3] == "support" for k in after), "the purchase is gone"
    day7 = {k: v for k, v in after.items() if k[0] == nwl_sub and k[1] == date(2026, 10, 7)}
    before = _expected({"2026-10"})
    assert day7
    for k, (amount, _, _) in day7.items():
        assert amount == _stored(Decimal(str(before[k][0])) * Decimal("1.25")) or abs(
            amount - before[k][0] * Decimal("1.25")) <= SIX, k
    assert all(n == 1 for *_, n in _batches(dsn)), "never two statements at once"
    snapshot = _cost(dsn)

    # Replay the first run's pages: its statements are older, so nothing comes back.
    on = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ENTITY, on, on)])
    assert replay.status == "succeeded"
    assert _cost(dsn) == snapshot
    activity_env = ActivityEnvironment()
    for ref in sorted(r for r in raw.pages if first.run_id in r):
        await activity_env.run(acts.process, ProcessRequest(
            ENTITY, ref, "replay-by-hand", datetime.now(UTC).isoformat(), False))
    assert _cost(dsn) == snapshot


# --- isolation --------------------------------------------------------------------------------


@pytest.mark.isolation
async def test_a_subscription_never_writes_under_another_customer(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    """If the alias of Northwind's subscription names Contoso, the account is still
    Northwind's: its statement fails and dead-letters, nothing is written under
    either customer, and Contoso's own statement is unaffected."""
    dsn = synthetic_module.admin_dsn
    truth = daily_expectations()
    nwl_sub = next(s for s, c in truth.currency.items() if c == "EUR" and s != UNREGISTERED)
    nwl, crl = tenant_id("NWL"), tenant_id("CRL")
    # The account exists as Northwind's first.
    await run(env, ConnectorActivities(db, MemoryRawStore(), _source(FakeAzure.from_fixtures())),
              ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    with psycopg.connect(dsn, autocommit=True) as cx:
        cx.execute("DELETE FROM cloudops.cost_fact")
        cx.execute("UPDATE cloudops.tenant_alias SET tenant_id = %s"
                   " WHERE source_system = 'azure' AND alias = %s", (crl, nwl_sub))
    try:
        (report,) = await run(env, ConnectorActivities(
            db, MemoryRawStore(), _source(FakeAzure.from_fixtures())),
            ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
        assert report.status != "succeeded"
        assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact WHERE tenant_id = %s",
                    nwl) == [(0,)]
        assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact f JOIN cloudops.cloud_account"
                         " a ON a.id = f.account_id WHERE a.native_id = %s", nwl_sub) == [(0,)]
        assert rows(dsn, "SELECT count(*) FROM staging.ingest_dead_letter"
                         " WHERE source_system = 'azure'")[0][0] >= 1
    finally:
        with psycopg.connect(dsn, autocommit=True) as cx:
            cx.execute("UPDATE cloudops.tenant_alias SET tenant_id = %s"
                       " WHERE source_system = 'azure' AND alias = %s", (nwl, nwl_sub))


@pytest.mark.isolation
async def test_one_customers_daily_cost_is_invisible_to_another(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    await run(env, ConnectorActivities(db, MemoryRawStore(), _source(FakeAzure.from_fixtures())),
              ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    nwl, crl = tenant_id("NWL"), tenant_id("CRL")
    app = Database.create(synthetic_module.app_dsn, pool_size=1, max_overflow=0)
    try:
        async with app.transaction(TenantScope({crl}, Role.FINOPS, "test:crl")) as conn:
            assert {str(r[0]) for r in await conn.execute(text(
                "SELECT DISTINCT currency FROM cloudops.cost_fact"))} == {"GBP"}
            assert await conn.scalar(text(
                "SELECT count(*) FROM cloudops.cost_fact WHERE tenant_id = :t"), {"t": nwl}) == 0
        async with app.transaction(TenantScope((), Role.FINOPS, "test:none")) as conn:
            assert await conn.scalar(text("SELECT count(*) FROM cloudops.cost_fact")) == 0
    finally:
        await app.dispose()
