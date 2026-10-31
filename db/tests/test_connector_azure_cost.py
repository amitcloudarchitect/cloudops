"""The Azure Cost Management FOCUS exports end to end: the generic connector
workflow on a time-skipping Temporal server, the migrated database (a module of
its own: the synthetic tenants, aliases, accounts and inventory, and no cost),
and the recorded exports served by the fake storage account through the real
blob client.

Ground truth comes from ``cloudops_devdata.azure_fixtures``: every export
run's rows, to six decimals, in two currencies, with their tags.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.blob import BlobClient
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import FakeAzure, FakeBlobStore
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    PageOutcome,
    ProcessRequest,
    ReplayRequest,
    scope_key,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_devdata.aws_fixtures import CostKey, Money
from cloudops_devdata.azure_fixtures import (
    EUR_ROOT,
    GBP_ROOT,
    INJECTION,
    UNKNOWN_RESOURCE_NAME,
    UNREGISTERED,
    ExportRun,
    export_expectations,
)
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

ENTITY = mapping.COST_EXPORT
_COST = (
    "SELECT a.native_id, f.usage_date, f.resource_id, f.service_native, f.charge_class::text,"
    " f.unblended_cost, f.amortized_cost, f.currency, f.invoice_period, f.source_batch,"
    " f.source_system, f.tenant_id, f.raw_extra, f.tags_snapshot"
    " FROM cloudops.cost_fact f JOIN cloudops.cloud_account a ON a.id = f.account_id"
)


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
        for table in ("cloudops.cost_fact", "staging.cost_fact_load", "staging.cur_assembly",
                      "staging.tenant_alias_quarantine", "staging.resource_quarantine",
                      "staging.schema_field", "staging.ingest_dead_letter"):
            cx.execute(f"DELETE FROM {table}")


def _source(blobs: FakeBlobStore) -> AzureSource:
    base = f"https://t{uuid.uuid4().hex[:8]}.management.example"  # its own watermarks
    return AzureSource(
        AzureClient(base, transport=FakeAzure.from_fixtures().transport(), sleep=no_sleep),
        exports=BlobClient("https://exports.example", transport=blobs.transport(),
                           max_results=4, sleep=no_sleep),
        export_roots=blobs.roots)


def _mark(dsn: str, source: AzureSource, scope: str) -> Any:
    found = rows(dsn, "SELECT watermark FROM staging.ingest_watermark"
                      " WHERE source_system = 'azure' AND entity = %s AND scope_key = %s",
                 ENTITY, scope_key(source.instance, scope))
    return found[0][0] if found else None


def _cost(dsn: str) -> dict[CostKey, tuple[Money, str, str]]:
    out: dict[CostKey, tuple[Money, str, str]] = {}
    for r in rows(dsn, _COST):
        key = CostKey(r[0], r[1], r[2], r[3], r[4])
        assert key not in out
        out[key] = (Money(r[5], r[6], r[7]), r[8], r[9])
    return out


def _expected(*runs: ExportRun) -> dict[CostKey, tuple[Money, str, str]]:
    return {k: (m, r.period, r.run_id) for r in runs for k, m in r.rows.items()}


def _sum(*runs: ExportRun) -> dict[str, tuple[Decimal, Decimal]]:
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for r in runs:
        for m in r.rows.values():
            u, am = out.get(m.currency, (Decimal(0), Decimal(0)))
            out[m.currency] = (u + m.unblended, am + m.amortized)
    return out


class _Probe(ConnectorActivities):
    """After every page: each subscription's period in cost_fact is empty or
    exactly one whole run's statement. Never two runs, never half of one."""

    dsn: str
    statements: dict[tuple[str, str], list[dict[str, Any]]]
    checks: int = 0

    @activity.defn(name="process")
    async def process(self, req: ProcessRequest) -> PageOutcome:
        out = await super().process(req)
        got = rows(self.dsn,
                   "SELECT a.native_id, f.invoice_period, array_agg(DISTINCT f.source_batch),"
                   " array_agg(DISTINCT f.currency), sum(f.unblended_cost),"
                   " sum(f.amortized_cost)"
                   " FROM cloudops.cost_fact f JOIN cloudops.cloud_account a"
                   "   ON a.id = f.account_id GROUP BY 1, 2")
        for account, period, batches, ccys, u, am in got:
            assert len(batches) == 1, "rows from two runs at once"
            assert len(ccys) == 1
            stated = {"run": batches[0], "currency": ccys[0], "unblended": u, "amortized": am}
            assert stated in self.statements[(account, period)], (account, period)
        self.checks += 1
        return out


def _probe(db: Database, raw: MemoryRawStore, source: AzureSource, dsn: str,
           truth: dict[str, ExportRun]) -> _Probe:
    probe = _Probe(db, raw, source)
    probe.dsn = dsn
    statements: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in truth.values():
        per: dict[tuple[str, str], list[Decimal]] = {}
        for k, m in r.rows.items():
            acc = per.setdefault((k.account, m.currency), [Decimal(0), Decimal(0)])
            acc[0] += m.unblended
            acc[1] += m.amortized
        for (account, ccy), (u, am) in per.items():
            statements.setdefault((account, r.period), []).append(
                {"run": r.run_id, "currency": ccy, "unblended": u, "amortized": am})
    probe.statements = statements
    return probe


async def test_runs_swap_whole_and_a_restatement_replaces_the_period_exactly(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    truth = export_expectations()
    es, eo1, eo2, gs, go = (truth[k] for k in ("ES", "EO1", "EO2", "GS", "GO"))
    raw, blobs = MemoryRawStore(), FakeBlobStore.from_fixtures()
    source = _source(blobs)
    probe = _probe(db, raw, source, dsn, truth)

    # Stage 0: September for both, EUR October as first exported; GBP October
    # still waits for a part, so none of it is in cost_fact.
    (first,) = await run(env, probe, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert first.status == "succeeded"
    assert [s.scope for s in first.scopes] == [EUR_ROOT, GBP_ROOT]
    assert _cost(dsn) == _expected(es, eo1, gs)
    assert rows(dsn, "SELECT DISTINCT assembly_id FROM staging.cost_fact_load") == [
        (go.run_id,)]
    assert _mark(dsn, source, EUR_ROOT) == eo1.manifest_last_modified
    assert _mark(dsn, source, GBP_ROOT) == gs.manifest_last_modified

    # Stage 1: October restated in EUR, and GBP October's late part lands.
    blobs.advance()
    (second,) = await run(env, probe, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert second.status == "succeeded"
    assert probe.checks > 8
    after = _cost(dsn)
    assert after == _expected(es, eo2, gs, go), "EO2 replaces EO1's October exactly"
    assert not (set(eo1.rows) - set(eo2.rows)) & set(after), "EO1's dropped row is gone"
    assert rows(dsn, "SELECT count(*) FROM staging.cost_fact_load") == [(0,)]

    # Totals in both currencies, to six decimals.
    totals = {r[0]: (r[1], r[2]) for r in rows(
        dsn, "SELECT currency, sum(unblended_cost), sum(amortized_cost) FROM cloudops.cost_fact"
             " GROUP BY 1")}
    assert set(totals) == {"EUR", "GBP"}
    for ccy, (u, am) in _sum(es, eo2, gs, go).items():
        assert (f"{totals[ccy][0]:.6f}", f"{totals[ccy][1]:.6f}") == (f"{u:.6f}", f"{am:.6f}")
    assert totals["EUR"] != _sum(es, eo1)["EUR"], "the restatement changed the figures"

    # Rows carry their tenant, source, run, tags and the unmapped columns.
    tenants = {r[0]: r[1] for r in rows(dsn, "SELECT native_id, tenant_id"
                                             " FROM cloudops.cloud_account")}
    tags = {k: t for r in (es, eo2, gs, go) for k, t in r.tags.items()}
    for r in rows(dsn, _COST):
        assert r[11] == tenants[r[0]]
        assert r[10] == "azure"
        assert r[13] == tags[CostKey(r[0], r[1], r[2], r[3], r[4])]
        parts = r[12].get("parts", [r[12]])
        assert all(p["lines"] >= 1 and p["charge_categories"] for p in parts)
        assert all("_tags_snapshot" not in p for p in parts), "tags moved, not duplicated"
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact WHERE tags_snapshot IS NOT NULL"
                )[0][0] > 0
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact WHERE resource_id IS NOT NULL"
                )[0][0] > 0, "ARM ids link case-insensitively"
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact WHERE raw_extra::text LIKE %s",
                f"%{INJECTION}%")[0][0] > 0
    classes = {r[0] for r in rows(dsn, "SELECT DISTINCT charge_class::text"
                                       " FROM cloudops.cost_fact")}
    assert classes == {"consumption", "licence", "support", "marketplace", "reservation",
                       "savings_plan", "tax", "credit", "other"}

    # Unknown subscription: quarantined with every sighting, nothing written for it.
    unknown = sum(r.unknown_subscription_lines for r in (es, eo1, eo2))
    assert dict(rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                          " WHERE source_system = 'azure'")) == {UNREGISTERED: unknown}
    assert rows(dsn, "SELECT count(*) FROM cloudops.cloud_account WHERE native_id = %s",
                UNREGISTERED) == [(0,)]
    # A resource nobody has: the cost is kept with no resource, the id quarantined.
    assert rows(dsn, "SELECT sum(occurrence_count), bool_and(account_id IS NOT NULL)"
                     " FROM staging.resource_quarantine WHERE source_system = 'azure'"
                     " AND name = %s", UNKNOWN_RESOURCE_NAME) == [
        (sum(sum(r.unlinked.values()) for r in (es, eo1, eo2)), True)]
    rejected = first.counts.rejected + second.counts.rejected
    assert rejected == sum(r.malformed_lines for r in truth.values()) + go.malformed_lines, (
        "GO's first part (with its malformed row) is read in both runs")

    assert raw_first(raw.events)
    assert all(r.startswith("azure/cost_export/") for r in raw.pages)
    assert _mark(dsn, source, EUR_ROOT) == eo2.manifest_last_modified
    assert _mark(dsn, source, GBP_ROOT) == go.manifest_last_modified


async def test_replay_is_idempotent_never_restores_a_restated_run_or_moves_a_watermark(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    truth = export_expectations()
    raw, blobs = MemoryRawStore(), FakeBlobStore.from_fixtures()
    source = _source(blobs)
    probe = _probe(db, raw, source, dsn, truth)
    (first,) = await run(env, probe, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    blobs.advance()
    await run(env, probe, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    snapshot = _cost(dsn)
    marks = [_mark(dsn, source, r) for r in (EUR_ROOT, GBP_ROOT)]
    requests = len(blobs.requests)
    on = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, probe, ConnectorReplay.run, [ReplayRequest(ENTITY, on, on)])
    assert replay.status == "succeeded"
    assert _cost(dsn) == snapshot
    # The first run's pages by hand, last: EO1 is complete again, but EO2
    # restated it, so EO1 swaps nothing back.
    env_ = ActivityEnvironment()
    for ref in sorted(r for r in raw.pages if first.run_id in r):
        await env_.run(probe.process, ProcessRequest(ENTITY, ref, "replay-by-hand",
                                                     datetime.now(UTC).isoformat(), False))
    assert _cost(dsn) == snapshot
    assert len(blobs.requests) == requests, "replay never touches the storage account"
    assert [_mark(dsn, source, r) for r in (EUR_ROOT, GBP_ROOT)] == marks


async def test_a_rerun_with_nothing_new_changes_nothing(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    blobs = FakeBlobStore.from_fixtures()
    blobs.advance()
    source = _source(blobs)
    acts = ConnectorActivities(db, MemoryRawStore(), source)
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    snapshot = rows(dsn, "SELECT id, unblended_cost FROM cloudops.cost_fact ORDER BY 1")
    (again,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert again.status == "succeeded"
    assert again.counts.written == 0
    assert rows(dsn, "SELECT id, unblended_cost FROM cloudops.cost_fact ORDER BY 1") == snapshot


# --- isolation --------------------------------------------------------------------------------


@pytest.mark.isolation
async def test_a_subscription_never_writes_under_another_customer(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    """If the alias of Northwind's subscription names Contoso, the account is
    still Northwind's: the part fails and dead-letters, nothing is written
    under either customer, and Contoso's own export is unaffected."""
    dsn = synthetic_module.admin_dsn
    truth = export_expectations()
    nwl_sub = next(k.account for k in truth["ES"].rows)
    nwl, crl = tenant_id("NWL"), tenant_id("CRL")
    with psycopg.connect(dsn, autocommit=True) as cx:
        cx.execute("UPDATE cloudops.tenant_alias SET tenant_id = %s"
                   " WHERE source_system = 'azure' AND alias = %s", (crl, nwl_sub))
    try:
        raw = MemoryRawStore()
        source = _source(FakeBlobStore.from_fixtures())
        (report,) = await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
                              [IngestRequest(ENTITY, SINCE)])
        by_scope = {s.scope: s.status for s in report.scopes}
        assert by_scope == {EUR_ROOT: "dead_lettered", GBP_ROOT: "succeeded"}
        assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact f JOIN cloudops.cloud_account a"
                         " ON a.id = f.account_id WHERE a.native_id = %s", nwl_sub) == [(0,)]
        assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact WHERE tenant_id = %s",
                    nwl) == [(0,)]
        assert rows(dsn, "SELECT DISTINCT tenant_id FROM cloudops.cost_fact") == [(crl,)]
        assert rows(dsn, "SELECT count(*) FROM staging.ingest_dead_letter"
                         " WHERE source_system = 'azure'")[0][0] == 1
        assert _mark(dsn, source, EUR_ROOT) is None
    finally:
        with psycopg.connect(dsn, autocommit=True) as cx:
            cx.execute("UPDATE cloudops.tenant_alias SET tenant_id = %s"
                       " WHERE source_system = 'azure' AND alias = %s", (nwl, nwl_sub))


@pytest.mark.isolation
async def test_one_customers_export_rows_are_invisible_to_another(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    blobs = FakeBlobStore.from_fixtures()
    blobs.advance()
    await run(env, ConnectorActivities(db, MemoryRawStore(), _source(blobs)),
              ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    nwl, crl = tenant_id("NWL"), tenant_id("CRL")
    app = Database.create(synthetic_module.app_dsn, pool_size=1, max_overflow=0)
    try:
        async with app.transaction(TenantScope({crl}, Role.FINOPS, "test:crl")) as conn:
            currencies = {str(r[0]) for r in await conn.execute(text(
                "SELECT DISTINCT currency FROM cloudops.cost_fact"))}
            assert currencies == {"GBP"}
            assert await conn.scalar(text(
                "SELECT count(*) FROM cloudops.cost_fact WHERE tenant_id = :t"), {"t": nwl}) == 0
        async with app.transaction(TenantScope((), Role.FINOPS, "test:none")) as conn:
            assert await conn.scalar(text("SELECT count(*) FROM cloudops.cost_fact")) == 0
    finally:
        await app.dispose()
