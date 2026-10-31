"""The CoreStack connector end to end: the generic connector workflow on a
time-skipping Temporal server, the migrated database (a module of its own,
holding the synthetic tenants, aliases and inventory, and no findings or cost),
and the recorded fixtures served by the fake account through the real client.

Ground truth comes from ``cloudops_devdata.corestack_fixtures.expectations()``:
every finding's tenant (or quarantine), the account and resource it must link
to, and its saving with its currency.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    ReplayRequest,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.corestack import mapping
from cloudops_connectors.corestack.client import CoreStackClient
from cloudops_connectors.corestack.fake import FakeCoreStack, load_fixture
from cloudops_connectors.corestack.source import CoreStackSource
from cloudops_devdata.corestack_fixtures import INJECTION, Expected, expectations
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

ENTITY = mapping.ENTITY


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _source(fake: FakeCoreStack, page_size: int = 10) -> CoreStackSource:
    base = f"https://t{uuid.uuid4().hex[:8]}.corestack.example"
    return CoreStackSource(CoreStackClient(base, transport=fake.transport(),
                                           page_size=page_size, sleep=no_sleep))


def _max_updated() -> Any:
    return max(mapping.updated_times(mapping.records(load_fixture("findings.json"))))


def _clear(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as cx:
        for table in ("cloudops.cost_finding", "staging.tenant_alias_quarantine",
                      "staging.resource_quarantine", "staging.schema_field"):
            cx.execute(f"DELETE FROM {table}")


def _written(truth: list[Expected]) -> list[Expected]:
    return [e for e in truth if e.tenant_code and not e.malformed and not e.mismatch]


async def _ingest(env: WorkflowEnvironment, db: Database, dsn: str,
                  fake: FakeCoreStack | None = None) -> None:
    _clear(dsn)
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake or FakeCoreStack.from_fixtures()))
    (report,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert report.status == "succeeded"


async def test_ingest_lands_every_finding_where_the_ground_truth_says(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    raw, fake = MemoryRawStore(), FakeCoreStack.from_fixtures()
    source = _source(fake)
    (report,) = await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
                          [IngestRequest(ENTITY, SINCE)])
    truth = expectations()
    good = _written(truth)
    held = [e for e in truth if e.tenant_code is None and not e.malformed]
    malformed = [e for e in truth if e.malformed or e.mismatch]  # both rejected
    assert report.status == "succeeded"
    c = report.counts
    assert c.pages > 2, "ten findings per page"
    assert (c.records, c.written, c.quarantined, c.rejected) == (
        len(truth), len(good), len(held), len(malformed))

    stored = {r[0]: r[1:] for r in rows(
        dsn, "SELECT finding_id, tenant_id, account_id, resource_id, status,"
             " estimated_saving, saving_currency FROM cloudops.cost_finding")}
    assert len(stored) == len(good)
    for e in good:
        tenant, account, resource, status, saving, currency = stored[e.finding_id]
        assert tenant == tenant_id(e.tenant_code or "")
        assert (account, resource) == (e.account_id, e.resource_id), e.finding_id
        assert (status, saving, currency) == (e.status, e.saving, e.currency), e.finding_id
    assert sum(1 for e in good if e.resource_id) > len(good) // 2
    assert {e.customer_raw for e in good} >= {"Northwind Logistics Prod"}, "normalised match"

    # Unmatched customer names quarantined with counts; nothing written for them.
    assert dict(rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                          " WHERE source_system = 'corestack'")) == dict(
        Counter(e.customer_raw for e in held))
    # Unlinked resources quarantined under the finding's own tenant.
    unlinked = Counter((tenant_id(e.tenant_code or ""), e.unlinked) for e in good if e.unlinked)
    assert {(r[0], r[1]): r[2] for r in rows(
        dsn, "SELECT tenant_id, external_key, occurrence_count FROM staging.resource_quarantine"
             " WHERE source_system = 'corestack'")} == dict(unlinked)
    assert c.ci_unlinked == sum(unlinked.values())

    # Findings beside the billing exports, never instead: no cost written.
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact") == [(0,)]
    # Raw before parsed; the injection is data in raw_extra, not an instruction.
    assert raw_first(raw.events)
    assert all(r.startswith("corestack/finding/") for r in raw.pages)
    extra = rows(dsn, "SELECT raw_extra FROM cloudops.cost_finding"
                      " WHERE raw_extra->>'annotations' = %s", INJECTION)
    assert len(extra) == 1
    assert extra[0][0]["policy_id"].startswith("CS-POL-")

    # The watermark: the highest update time committed; the first run is the baseline.
    assert rows(dsn, "SELECT watermark FROM staging.ingest_watermark WHERE scope_key = %s",
                source.instance) == [(_max_updated(),)]
    assert rows(dsn, "SELECT count(*) FROM staging.schema_field WHERE drift IS NOT NULL"
                     " AND source_system = 'corestack'") == [(0,)]
    assert rows(dsn, "SELECT status, written, quarantined, rejected, ci_unlinked"
                     " FROM staging.ingest_run WHERE id = %s", uuid.UUID(report.run_id)) == [
        ("succeeded", len(good), len(held), len(malformed), sum(unlinked.values()))]


async def test_an_account_or_resource_of_another_tenant_is_never_linked(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    await _ingest(env, db, dsn)
    # Every link stays inside the finding's own tenant.
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_finding f"
                     " JOIN cloudops.cloud_account a ON a.id = f.account_id"
                     " WHERE a.tenant_id <> f.tenant_id") == [(0,)]
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_finding f"
                     " JOIN cloudops.resource r ON r.id = f.resource_id"
                     " WHERE r.tenant_id <> f.tenant_id") == [(0,)]
    # The planted one, Northwind's name on a Contoso account and resource, is
    # rejected: not written, not quarantined, under either tenant.
    (cross,) = [e for e in expectations() if e.mismatch]
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_finding WHERE finding_id = %s",
                cross.finding_id) == [(0,)]


@pytest.mark.isolation
async def test_nothing_in_northwinds_scope_carries_contosos_identifiers(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    """A source that labels one customer's account with another's name must not
    put the first customer's identifiers in the second's view."""
    dsn = synthetic_module.admin_dsn
    await _ingest(env, db, dsn)
    crl = tenant_id("CRL")
    accounts = {str(r[0]) for r in rows(
        dsn, "SELECT native_id FROM cloudops.cloud_account WHERE tenant_id = %s", crl)}
    resources = {str(r[0]) for r in rows(
        dsn, "SELECT native_id FROM cloudops.resource WHERE tenant_id = %s", crl)}
    names = {str(r[0]) for r in rows(
        dsn, "SELECT name FROM cloudops.resource WHERE tenant_id = %s", crl)}
    assert accounts
    assert resources

    app = Database.create(synthetic_module.app_dsn, pool_size=1, max_overflow=0)
    try:
        async with app.transaction(TenantScope([tenant_id("NWL")], Role.ENGINEER,
                                               "isolation-test")) as conn:
            findings = [tuple(r) for r in await conn.execute(text(
                "SELECT account_native_id, resource_native_id, title, raw_extra::text"
                " FROM cloudops.cost_finding"))]
    finally:
        await app.dispose()
    # The resource quarantine is staging (ingest only): read under the same tenant.
    async with db.transaction(TenantScope([tenant_id("NWL")], Role.INGEST,
                                          "isolation-test")) as conn:
        held = [tuple(r) for r in await conn.execute(text(
            "SELECT external_key, name FROM staging.resource_quarantine"))]
    assert held, "the unknown Northwind resource is there"
    assert findings
    for account, resource, title, extra in findings:
        assert account not in accounts
        assert resource not in resources
        assert not any(a in (title or "") or a in (extra or "") for a in accounts)
    for key, name in held:
        assert key not in resources
        assert not any(a in key for a in accounts)
        assert name not in names


async def test_savings_are_stored_exactly_to_six_decimals_with_their_currencies(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    await _ingest(env, db, dsn)
    good = {e.finding_id: e for e in _written(expectations())}
    got = rows(dsn, "SELECT finding_id, estimated_saving::text, saving_currency"
                    " FROM cloudops.cost_finding")
    for finding_id, saving, currency in got:
        e = good[finding_id]
        if e.saving is None:
            assert (saving, currency) == (None, None)
        else:
            assert saving == f"{e.saving.quantize(Decimal('0.000001')):f}", finding_id
            assert currency == e.currency
    # Totals per currency, exact; never summed across currencies.
    totals = dict(rows(dsn, "SELECT saving_currency, sum(estimated_saving)"
                            " FROM cloudops.cost_finding WHERE estimated_saving IS NOT NULL"
                            " GROUP BY 1"))
    want: dict[str, Decimal] = {}
    for e in good.values():
        if e.saving is not None and e.currency is not None:
            want[e.currency] = want.get(e.currency, Decimal(0)) + e.saving
    assert set(totals) == {"EUR", "GBP", "USD"}
    assert totals == want


async def test_a_second_run_reads_only_the_overlap_and_duplicates_nothing(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    fake = FakeCoreStack.from_fixtures()
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    before = rows(dsn, "SELECT finding_id, status, estimated_saving FROM cloudops.cost_finding"
                       " ORDER BY 1")
    (second,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    since = (_max_updated() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert fake.requests[-1].url.params["updated_since"] == since
    assert 1 <= second.counts.records < 5
    assert rows(dsn, "SELECT finding_id, status, estimated_saving FROM cloudops.cost_finding"
                     " ORDER BY 1") == before


async def test_a_status_change_updates_the_finding_in_place(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    fake = FakeCoreStack.from_fixtures()
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    target = next(e for e in _written(expectations()) if e.status == "open" and e.saving)
    fake.touch(target.finding_id, "2026-10-30T08:00:00Z", status="Resolved",
               estimated_monthly_savings={"amount": "10.500000", "currency": "USD"})
    (report,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert report.counts.written >= 1
    assert rows(dsn, "SELECT count(*), max(status), max(estimated_saving), max(saving_currency),"
                     " max(updated_at)::text FROM cloudops.cost_finding WHERE finding_id = %s",
                target.finding_id) == [
        (1, "resolved", Decimal("10.500000"), "USD", "2026-10-30 08:00:00+00")]


async def test_replay_reprocesses_bronze_without_refetching_or_moving_the_watermark(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    raw, fake = MemoryRawStore(), FakeCoreStack.from_fixtures()
    source = _source(fake)
    acts = ConnectorActivities(db, raw, source)
    (first,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    snapshot_sql = ("SELECT finding_id, tenant_id, account_id, resource_id, status,"
                    " estimated_saving, saving_currency, updated_at FROM cloudops.cost_finding"
                    " ORDER BY 1")
    snapshot = rows(dsn, snapshot_sql)
    mark = rows(dsn, "SELECT watermark FROM staging.ingest_watermark WHERE scope_key = %s",
                source.instance)
    requests = len(fake.requests)
    on = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ENTITY, on, on)])
    assert replay.status == "succeeded"
    assert len(fake.requests) == requests, "replay never touches the source"
    assert replay.counts.written == first.counts.written
    assert rows(dsn, snapshot_sql) == snapshot
    assert rows(dsn, "SELECT watermark FROM staging.ingest_watermark WHERE scope_key = %s",
                source.instance) == mark


# --- isolation ---------------------------------------------------------------------------


@pytest.mark.isolation
async def test_findings_are_isolated_by_tenant_and_restricted_by_account(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    await _ingest(env, db, dsn)
    nwl, crl = tenant_id("NWL"), tenant_id("CRL")
    owner = dict(rows(dsn, "SELECT finding_id, tenant_id FROM cloudops.cost_finding"))
    assert {nwl, crl} == set(owner.values())
    on_account = rows(dsn, "SELECT account_id, finding_id FROM cloudops.cost_finding"
                           " WHERE tenant_id = %s AND account_id IS NOT NULL", nwl)
    account = Counter(a for a, _ in on_account).most_common(1)[0][0]
    crl_account = rows(dsn, "SELECT account_id FROM cloudops.cost_finding"
                            " WHERE tenant_id = %s AND account_id IS NOT NULL"
                            " ORDER BY 1 LIMIT 1", crl)[0][0]

    app = Database.create(synthetic_module.app_dsn, pool_size=1, max_overflow=0)
    try:
        async def visible(scope: TenantScope) -> dict[str, tuple[Any, ...]]:
            async with app.transaction(scope) as conn:
                result = await conn.execute(text(
                    "SELECT finding_id, tenant_id, account_id FROM cloudops.cost_finding"))
                return {str(r[0]): tuple(r[1:]) for r in result}

        # Tenant B's scope never sees tenant A's findings, and the reverse.
        as_crl = await visible(TenantScope([crl], Role.ENGINEER, "isolation-test"))
        as_nwl = await visible(TenantScope([nwl], Role.ENGINEER, "isolation-test"))
        assert as_crl
        assert as_nwl
        assert set(as_crl) == {f for f, t in owner.items() if t == crl}
        assert set(as_nwl) == {f for f, t in owner.items() if t == nwl}
        assert not set(as_crl) & set(as_nwl)
        # No scope, no rows: never "all tenants".
        assert await visible(TenantScope([], Role.ENGINEER, "isolation-test")) == {}

        # An account-restricted scope sees only findings on its accounts; a
        # finding with no account is hidden from it.
        limited = await visible(TenantScope([nwl], Role.ENGINEER, "isolation-test",
                                            account_ids=[account]))
        assert set(limited) == {f for a, f in on_account if a == account}
        assert all(a == account for _, a in limited.values())
        # Contoso's account, restricted: only Contoso's findings on it.
        crl_limited = await visible(TenantScope([crl], Role.ENGINEER, "isolation-test",
                                                account_ids=[crl_account]))
        assert crl_limited
        assert all(owner[f] == crl and a == crl_account for f, (_, a) in crl_limited.items())
        # Tenant A's account id under tenant B's scope reaches nothing.
        assert await visible(TenantScope([crl], Role.ENGINEER, "isolation-test",
                                         account_ids=[account])) == {}
    finally:
        await app.dispose()
