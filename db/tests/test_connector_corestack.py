"""The CoreStack connector end to end (ADR 0028): the generic connector workflow on a
time-skipping Temporal server, the migrated database (a module of its own, holding
the synthetic tenants, aliases and inventory), and the recorded fixtures served by
the fake CoreStack through the real client.

Ground truth comes from ``cloudops_devdata.corestack_fixtures.expectations()``:
every finding's table, tenant (or quarantine), the account and resource it must
link to, its severity and status.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    ReplayRequest,
    RunReport,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.corestack import mapping
from cloudops_connectors.corestack.client import CoreStackClient
from cloudops_connectors.corestack.fake import (
    FIXTURE_ACCESS_KEY,
    FIXTURE_SECRET,
    FakeCoreStack,
    load_fixture,
)
from cloudops_connectors.corestack.source import CoreStackSource
from cloudops_devdata.corestack_fixtures import INJECTION, QUARANTINED_NAME, Expected, expectations
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

ENTITY = mapping.ENTITY
TENANTS = tuple(t["id"] for t in load_fixture()["tenants"])
TRUTH = expectations()


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    with psycopg.connect(synthetic_module.admin_dsn, autocommit=True) as cx:
        for table in ("cloudops.cost_finding", "cloudops.security_finding",
                      "staging.tenant_alias_quarantine", "staging.resource_quarantine",
                      "staging.schema_field", "staging.ingest_watermark"):
            cx.execute(f"DELETE FROM {table}")
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _source(fake: FakeCoreStack, page_size: int = 4) -> CoreStackSource:
    base = f"https://t{uuid.uuid4().hex[:8]}.corestack.example"
    client = CoreStackClient(base, lambda: FIXTURE_ACCESS_KEY, lambda: FIXTURE_SECRET,
                             transport=fake.transport(), page_size=page_size, min_interval=0.0,
                             sleep=no_sleep)
    return CoreStackSource(client, TENANTS)


async def _ingest(env: WorkflowEnvironment, acts: ConnectorActivities) -> RunReport:
    (report,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    return report


def _written(table: str) -> list[Expected]:
    return [e for e in TRUTH if e.table == table and e.tenant_code and not e.malformed
            and not e.mismatch]


def _stored(dsn: str, table: str) -> dict[str, tuple[Any, ...]]:
    return {r[0]: r[1:] for r in rows(
        dsn, f"SELECT finding_id, tenant_id, account_id, resource_id, status, source_scope,"
             f" raw_extra FROM cloudops.{table} WHERE source_system = 'corestack'")}


async def test_ingest_files_every_finding_where_the_ground_truth_says(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    raw, fake = MemoryRawStore(), FakeCoreStack.from_fixtures()
    report = await _ingest(env, ConnectorActivities(db, raw, _source(fake)))
    assert report.status == "succeeded"
    assert sorted(s.scope for s in report.scopes) == sorted(TENANTS)

    for table, name in (("cost", "cost_finding"), ("security", "security_finding")):
        want = _written(table)
        stored = _stored(dsn, name)
        assert set(stored) == {e.finding_id for e in want}, table
        for e in want:
            tenant, account, resource, status, scope, _extra = stored[e.finding_id]
            assert tenant == tenant_id(e.tenant_code or "")
            assert account == e.account_id, e.finding_id
            assert resource == e.resource_id, e.finding_id
            assert (status, scope) == (e.status, e.corestack_tenant)
    sev = dict(rows(dsn, "SELECT finding_id, severity FROM cloudops.security_finding"))
    assert all(sev[e.finding_id] == e.severity for e in _written("security"))
    cats = {r[0] for r in rows(dsn, "SELECT DISTINCT category FROM cloudops.security_finding")}
    assert cats == {"security", "compliance"}
    # No money in CoreStack's API: no saving is ever invented.
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_finding"
                     " WHERE estimated_saving IS NOT NULL") == [(0,)]

    # Another customer's subscription: rejected, so nothing of it is written anywhere.
    mismatch = [e.finding_id for e in TRUTH if e.mismatch]
    assert mismatch
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_finding WHERE finding_id = ANY(%s)",
                mismatch) == [(0,)]
    # Operation is kept in bronze only.
    assert not any(e.table is None and e.finding_id in _stored(dsn, "cost_finding")
                   for e in TRUTH)
    # The tenant nobody is mapped to: quarantined by name, with a count.
    unknown = Counter(e.corestack_tenant for e in TRUTH if e.tenant_code is None)
    assert rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                     " WHERE source_system = 'corestack'") == [(QUARANTINED_NAME,
                                                                sum(unknown.values()))]
    # A resource the inventory lacks: quarantined in its tenant, the finding still written.
    unlinked = Counter(e.unlinked for e in TRUTH if e.unlinked)
    assert dict(rows(dsn, "SELECT external_key, occurrence_count FROM staging.resource_quarantine"
                          " WHERE source_system = 'corestack'")) == dict(unlinked)
    # Text is data: the hostile description is kept verbatim, nothing acted on it.
    assert rows(dsn, "SELECT count(*) FROM cloudops.security_finding"
                     " WHERE raw_extra->>'description' = %s", INJECTION) != [(0,)]
    assert raw_first(raw.events)
    assert report.counts.rejected == sum(1 for e in TRUTH if e.malformed) + len(mismatch)


async def test_a_finding_gone_from_the_snapshot_is_resolved_and_replay_resolves_nothing(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    raw, fake = MemoryRawStore(), FakeCoreStack.from_fixtures()
    acts = ConnectorActivities(db, raw, _source(fake))
    await _ingest(env, acts)
    fixed = next(e for e in _written("security") if e.status == "open")
    rec_id = fixed.finding_id.split(":")[0]
    siblings = {e.finding_id for e in TRUTH if e.finding_id.startswith(f"{rec_id}:")}
    fake.resolve(fixed.corestack_tenant, rec_id)
    fake.expire_tokens()  # the next run must fetch a new token, once
    second = await _ingest(env, acts)
    assert second.status == "succeeded"
    stored = _stored(dsn, "security_finding")
    assert {stored[f][3] for f in siblings} == {"resolved"}
    assert rows(dsn, "SELECT count(*) FROM cloudops.security_finding WHERE status = 'resolved'"
                     " AND resolved_at IS NULL") == [(0,)]
    still = [e for e in _written("security") if e.status == "open"
             and e.finding_id not in siblings]
    assert {stored[e.finding_id][3] for e in still} == {"open"}
    # Malformed recommendations resolve nothing of their own; others' scopes untouched.
    tokens = [r for r in fake.requests if r.url.path == "/v1/auth/tokens"]
    assert len(tokens) == 2

    # Replaying the first day's bronze puts nothing back to open and resolves nothing.
    before = rows(dsn, "SELECT finding_id, status, updated_at FROM cloudops.security_finding"
                       " ORDER BY finding_id")
    day = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ENTITY, day, day)])
    assert replay.status == "succeeded"
    assert rows(dsn, "SELECT finding_id, status, updated_at FROM cloudops.security_finding"
                     " ORDER BY finding_id") == before


async def test_the_client_sent_nothing_but_the_token_and_the_two_reads(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    fake = FakeCoreStack.from_fixtures()
    await _ingest(env, ConnectorActivities(db, MemoryRawStore(), _source(fake)))
    kinds = Counter(
        ("POST" if r.method == "POST" else "GET",
         "token" if r.url.path == "/v1/auth/tokens" else
         "list" if r.url.path.endswith("/service_account/list") else
         "detail" if "/get/" in r.url.path else "other")
        for r in fake.requests)
    assert set(kinds) <= {("POST", "token"), ("GET", "list"), ("GET", "detail")}
    assert kinds[("POST", "token")] == 1, "one token, reused"
    # Paged: the list in pages of four, and the many-resource detail over two pages.
    assert kinds[("GET", "list")] > len(TENANTS)


async def test_a_subscription_shared_with_the_platform_files_under_the_customer_holding_it(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    """ADR 0027: the platform's own subscription is a customer's too. Its alias names
    the platform's tenant, but the customer holds the account as well, so CoreStack's
    findings on it link under the customer, never rejected and never the platform's."""
    dsn = synthetic_module.admin_dsn
    shared = next(e for e in _written("security") if e.account_id and e.tenant_code == "NWL")
    native = rows(dsn, "SELECT native_id FROM cloudops.cloud_account WHERE id = %s",
                  shared.account_id)[0][0]
    other = tenant_id("FAB")  # stands in for the internal tenant: it owns the alias
    with psycopg.connect(dsn, autocommit=True) as cx:
        before = cx.execute("SELECT tenant_id FROM cloudops.tenant_alias"
                            " WHERE source_system = 'azure' AND alias = %s", (native,)).fetchall()
        cx.execute("DELETE FROM cloudops.tenant_alias WHERE source_system = 'azure'"
                   " AND alias = %s", (native,))
        cx.execute("INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias,"
                   " confirmed_by) VALUES (%s, 'azure', %s, 'test:shared')", (other, native))
    try:
        report = await _ingest(env, ConnectorActivities(db, MemoryRawStore(),
                                                        _source(FakeCoreStack.from_fixtures())))
        assert report.status == "succeeded"
        stored = _stored(dsn, "security_finding")
        assert stored[shared.finding_id][:2] == (tenant_id("NWL"), shared.account_id)
        assert rows(dsn, "SELECT count(*) FROM cloudops.security_finding WHERE tenant_id = %s",
                    other) == [(0,)]
    finally:
        with psycopg.connect(dsn, autocommit=True) as cx:
            cx.execute("DELETE FROM cloudops.tenant_alias WHERE source_system = 'azure'"
                       " AND alias = %s", (native,))
            for (tenant,) in before:
                cx.execute("INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias,"
                           " confirmed_by) VALUES (%s, 'azure', %s, 'test:restore')",
                           (tenant, native))


async def test_a_refused_detail_files_the_recommendation_and_resolves_nothing_it_held(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    """CoreStack answers 400 for some details (every Defender for Cloud one). The run
    still succeeds; the recommendation is filed from its list fields, and what an
    earlier snapshot read from its detail is not resolved for want of it."""
    dsn = synthetic_module.admin_dsn
    raw, fake = MemoryRawStore(), FakeCoreStack.from_fixtures()
    acts = ConnectorActivities(db, raw, _source(fake))
    await _ingest(env, acts)
    held = next(e for e in _written("security") if e.status == "open" and ":" in e.finding_id)
    rec_id = held.finding_id.split(":")[0]
    siblings = {e.finding_id for e in _written("security")
                if e.finding_id.startswith(f"{rec_id}:")}

    fake.refuse_detail(rec_id)
    second = await _ingest(env, acts)
    assert second.status == "succeeded"
    stored = _stored(dsn, "security_finding")
    assert "resolved" not in {stored[f][3] for f in siblings}
    tenant, account, resource, status, _scope, extra = stored[rec_id]
    assert (tenant, account, resource, status) == (tenant_id(held.tenant_code or ""), None,
                                                   None, "open")
    assert extra["detail_status"] == 400

    # The detail answers again: the list-only finding is gone from the snapshot, so
    # it resolves; the per-resource ones carry on.
    fake.refused.clear()
    third = await _ingest(env, acts)
    assert third.status == "succeeded"
    stored = _stored(dsn, "security_finding")
    assert stored[rec_id][3] == "resolved"
    assert "resolved" not in {stored[f][3] for f in siblings}
