"""The AWS connector end to end: the generic connector workflow on a
time-skipping Temporal server, the migrated database (a module of its own,
holding the synthetic tenants, aliases, accounts and inventory, and no cost),
and the recorded fixtures served by the fake Config aggregator and the fake CUR
bucket through the real clients.

Ground truth comes from ``cloudops_devdata.aws_fixtures``: every Config item's
tenant (or quarantine) and the resource it updates, and every CUR assembly's
rows, to six decimals, in two currencies.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from cloudops_connectors.aws import mapping
from cloudops_connectors.aws.client import ConfigClient, S3Client
from cloudops_connectors.aws.fake import AGGREGATOR, REGION, FakeConfig, FakeS3
from cloudops_connectors.aws.source import AwsSource
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
from cloudops_devdata.aws_fixtures import (
    EUR_REPORT,
    INJECTION,
    UNKNOWN_RESOURCE,
    UNREGISTERED,
    USD_REPORT,
    CostKey,
    CurAssembly,
    Money,
    config_expectations,
    cur_expectations,
)
from cloudops_devdata.servicenow_fixtures import FIXTURE_ANCHOR, FIXTURE_SEED
from cloudops_devdata.synthetic import generate, tenant_id
from cloudops_tenancy import Database

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _source(config: FakeConfig, s3: FakeS3, limit: int = 4) -> AwsSource:
    instance = f"https://t{uuid.uuid4().hex[:8]}.aws.example"  # its own watermarks
    return AwsSource(
        ConfigClient(REGION, AGGREGATOR, base_url="https://config.example",
                     transport=config.transport(), limit=limit, sleep=no_sleep),
        S3Client(s3.bucket, REGION, base_url="https://s3.example", transport=s3.transport(),
                 max_keys=5, sleep=no_sleep),
        s3.reports, instance=instance)


def _clear(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as cx:
        for table in ("cloudops.cost_fact", "staging.cost_fact_load", "staging.cur_assembly",
                      "staging.tenant_alias_quarantine", "staging.resource_quarantine",
                      "staging.schema_field"):
            cx.execute(f"DELETE FROM {table}")


def _mark(dsn: str, source: AwsSource, entity: str, scope: str) -> Any:
    found = rows(dsn, "SELECT watermark FROM staging.ingest_watermark WHERE source_system = 'aws'"
                      " AND entity = %s AND scope_key = %s",
                 entity, scope_key(source.instance, scope))
    return found[0][0] if found else None


# --- Config ------------------------------------------------------------------------------

_RESOURCES = (
    "SELECT a.native_id, r.native_id, r.id, r.tenant_id, r.name, r.type_canonical,"
    " r.deleted_at, ARRAY(SELECT host(x) FROM unnest(r.ip_addresses) x), r.operating_system,"
    " r.state, r.source_system, r.raw_extra, r.last_seen, r.tags"
    " FROM cloudops.resource r JOIN cloudops.cloud_account a ON a.id = r.account_id"
    " WHERE a.provider = 'aws'"
)


async def test_config_lands_every_item_where_the_ground_truth_says(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    raw, fake = MemoryRawStore(), FakeConfig.from_fixtures()
    source = _source(fake, FakeS3.from_fixtures())
    before = rows(dsn, "SELECT count(*) FROM cloudops.resource")[0][0]
    (report,) = await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
                          [IngestRequest(mapping.CONFIG, SINCE)])
    truth = config_expectations()
    good = [e for e in truth if e.tenant_code and not e.malformed]
    held = [e for e in truth if e.tenant_code is None and not e.malformed]
    new = [e for e in good if e.resource_id is None]
    assert report.status == "succeeded"
    c = report.counts
    assert c.pages > 2, "four results per page"
    assert (c.written, c.quarantined, c.rejected, c.records) == (
        len(good), len(held), 2, len(truth))

    stored = {(r[0], r[1]): r for r in rows(dsn, _RESOURCES)}
    for e in good:
        r = stored[(e.account, e.arn)]
        assert r[3] == tenant_id(e.tenant_code or "")
        if e.resource_id is not None:
            assert r[2] == e.resource_id, "the seeded row is updated, not duplicated"
        assert (r[4], r[5]) == (e.name, e.type_canonical)
        assert r[6] == (e.captured_at if e.deleted else None)
        assert tuple(r[7] or ()) == e.ips
        assert (r[8], r[9]) == (e.operating_system, e.state), e.arn
        assert (r[10], r[12]) == ("aws", e.captured_at)
    assert rows(dsn, "SELECT count(*) FROM cloudops.resource")[0][0] == before + len(new)
    ds = generate(FIXTURE_ANCHOR, FIXTURE_SEED)
    aws = {a.id for a in ds.accounts if a.provider == "aws"}
    assert len(stored) == sum(1 for r in ds.resources if r.account_id in aws) + len(new)

    # Unmapped fields kept; an injection in a tag is data, stored verbatim.
    vm = next(r for r in stored.values() if r[5] == "compute.vm" and r[6] is None)
    assert vm[11]["availabilityZone"].startswith("eu-west-1")
    assert vm[11]["configuration"] == {"instanceType": "m5.large"}
    assert any(r[13].get("Description") == INJECTION for r in stored.values())

    # An account nobody registered: quarantined with a count, nothing written for it.
    assert dict(rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                          " WHERE source_system = 'aws'")) == {UNREGISTERED: len(held)}
    assert rows(dsn, "SELECT count(*) FROM cloudops.cloud_account WHERE native_id = %s",
                UNREGISTERED) == [(0,)]
    # Onboarded accounts keep their names.
    names = {a.native_id: a.display_name for a in ds.accounts if a.id in aws}
    assert dict(rows(dsn, "SELECT native_id, display_name FROM cloudops.cloud_account"
                          " WHERE provider = 'aws'")) == names

    assert raw_first(raw.events)
    assert all(r.startswith("aws/config/") for r in raw.pages)
    # One watermark, the run's highest capture time (malformed items included).
    assert _mark(dsn, source, mapping.CONFIG, AGGREGATOR) == max(e.captured_at for e in truth)
    assert rows(dsn, "SELECT count(*) FROM staging.schema_field WHERE drift IS NOT NULL"
                ) == [(0,)]


async def test_config_reads_the_overlap_next_time_and_records_a_deletion(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    fake = FakeConfig.from_fixtures()
    source = _source(fake, FakeS3.from_fixtures(), limit=100)
    acts = ConnectorActivities(db, MemoryRawStore(), source)
    await run(env, acts, ConnectorIngest.run, [IngestRequest(mapping.CONFIG, SINCE)])
    snapshot = rows(dsn, _RESOURCES + " ORDER BY 2")
    mark = _mark(dsn, source, mapping.CONFIG, AGGREGATOR)

    (second,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(mapping.CONFIG, SINCE)])
    since = mapping.since_literal(mark - timedelta(minutes=5))
    assert since in json.loads(fake.requests[-1].content)["Expression"]
    assert second.counts.records < len(config_expectations()) // 2, "only the overlap"
    assert rows(dsn, _RESOURCES + " ORDER BY 2") == snapshot, "nothing duplicated or regressed"

    live = next(e for e in config_expectations()
                if e.resource_id and not e.deleted and e.type_canonical == "compute.vm")
    gone = mark + timedelta(hours=2)
    fake.touch(live.arn, gone.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
               configurationItemStatus="ResourceDeleted", configuration={}, tags=[])
    (third,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(mapping.CONFIG, SINCE)])
    assert third.counts.written >= 1
    assert rows(dsn, "SELECT id, deleted_at, state FROM cloudops.resource WHERE native_id = %s",
                live.arn) == [(live.resource_id, gone, "deleted")]
    assert _mark(dsn, source, mapping.CONFIG, AGGREGATOR) == gone


async def test_config_replay_reprocesses_bronze_without_fetching_or_moving_the_watermark(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    raw, fake = MemoryRawStore(), FakeConfig.from_fixtures()
    source = _source(fake, FakeS3.from_fixtures())
    acts = ConnectorActivities(db, raw, source)
    (first,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(mapping.CONFIG, SINCE)])
    snapshot = rows(dsn, _RESOURCES + " ORDER BY 2")
    mark = _mark(dsn, source, mapping.CONFIG, AGGREGATOR)
    requests = len(fake.requests)
    on = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run,
                          [ReplayRequest(mapping.CONFIG, on, on)])
    assert replay.status == "succeeded"
    assert len(fake.requests) == requests, "replay never touches the source"
    assert replay.counts.written == first.counts.written
    assert rows(dsn, _RESOURCES + " ORDER BY 2") == snapshot
    assert _mark(dsn, source, mapping.CONFIG, AGGREGATOR) == mark


# --- CUR -------------------------------------------------------------------------------------

_COST = (
    "SELECT a.native_id, f.usage_date, f.resource_id, f.service_native, f.charge_class::text,"
    " f.unblended_cost, f.amortized_cost, f.currency, f.invoice_period, f.source_batch,"
    " f.source_system, f.tenant_id, f.raw_extra"
    " FROM cloudops.cost_fact f JOIN cloudops.cloud_account a ON a.id = f.account_id"
)


def _cost(dsn: str) -> dict[CostKey, tuple[Money, str, str]]:
    """What cost_fact holds: key -> (money, invoice period, assembly)."""
    out: dict[CostKey, tuple[Money, str, str]] = {}
    for r in rows(dsn, _COST):
        key = CostKey(r[0], r[1], r[2], r[3], r[4])
        assert key not in out
        out[key] = (Money(r[5], r[6], r[7]), r[8], r[9])
    return out


def _expected(*assemblies: CurAssembly) -> dict[CostKey, tuple[Money, str, str]]:
    return {k: (m, a.period, a.assembly_id) for a in assemblies for k, m in a.rows.items()}


def _statements(truth: dict[str, CurAssembly]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """(account, period) -> the totals each assembly states for it."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in truth.values():
        for (account, ccy), (u, am) in a.totals().items():
            out.setdefault((account, a.period), []).append(
                {"assembly": a.assembly_id, "currency": ccy, "unblended": u, "amortized": am})
    return out


class _Probe(ConnectorActivities):
    """After every page: each account's period in cost_fact is empty or exactly
    one whole assembly's statement. Never two assemblies, never half of one."""

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
            assert len(batches) == 1, "rows from two assemblies at once"
            assert len(ccys) == 1
            stated = {"assembly": batches[0], "currency": ccys[0], "unblended": u,
                      "amortized": am}
            assert stated in self.statements[(account, period)], (account, period)
        self.checks += 1
        return out


def _probe(db: Database, raw: MemoryRawStore, source: AwsSource, dsn: str,
           truth: dict[str, CurAssembly]) -> _Probe:
    probe = _Probe(db, raw, source)
    probe.dsn = dsn
    probe.statements = _statements(truth)
    return probe


def _totals(dsn: str) -> dict[str, tuple[Decimal, Decimal]]:
    return {r[0]: (r[1], r[2]) for r in rows(
        dsn, "SELECT currency, sum(unblended_cost), sum(amortized_cost) FROM cloudops.cost_fact"
             " GROUP BY 1")}


def _sum(*assemblies: CurAssembly) -> dict[str, tuple[Decimal, Decimal]]:
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for a in assemblies:
        for m in a.rows.values():
            u, am = out.get(m.currency, (Decimal(0), Decimal(0)))
            out[m.currency] = (u + m.unblended, am + m.amortized)
    return out


async def test_cur_swaps_whole_assemblies_and_a_restatement_replaces_the_period_exactly(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    truth = cur_expectations()
    s1, o1, o2, e1 = truth["S1"], truth["O1"], truth["O2"], truth["E1"]
    raw, s3 = MemoryRawStore(), FakeS3.from_fixtures()
    source = _source(FakeConfig.from_fixtures(), s3)
    probe = _probe(db, raw, source, dsn, truth)

    # Stage 0: September and the first October assembly complete; E1 still waits
    # for a file, so nothing of it is in cost_fact.
    (first,) = await run(env, probe, ConnectorIngest.run, [IngestRequest(mapping.CUR, SINCE)])
    assert first.status == "succeeded"
    assert [s.scope for s in first.scopes] == [USD_REPORT, EUR_REPORT]
    assert _cost(dsn) == _expected(s1, o1)
    loading = rows(dsn, "SELECT DISTINCT assembly_id, object_key FROM staging.cost_fact_load")
    assert loading == [(e1.assembly_id, e1.report_keys[0])]
    assert rows(dsn, "SELECT loaded_keys, swapped_at FROM staging.cur_assembly"
                     " WHERE assembly_id = %s", e1.assembly_id) == [([e1.report_keys[0]], None)]
    assert _mark(dsn, source, mapping.CUR, USD_REPORT) == o1.manifest_last_modified
    assert _mark(dsn, source, mapping.CUR, EUR_REPORT) is None

    # Stage 1: AWS restates October and E1's late file lands.
    s3.advance()
    (second,) = await run(env, probe, ConnectorIngest.run, [IngestRequest(mapping.CUR, SINCE)])
    assert second.status == "succeeded"
    assert probe.checks > 10
    after = _cost(dsn)
    assert after == _expected(s1, o2, e1), "O2 replaces O1's October exactly"
    assert not set(o1.rows) - set(o2.rows) & set(after), "O1's dropped row is gone"
    assert rows(dsn, "SELECT count(*) FROM staging.cost_fact_load") == [(0,)]

    # Totals in both currencies, to six decimals.
    totals = _totals(dsn)
    assert set(totals) == {"USD", "EUR"}
    for ccy, (u, am) in _sum(s1, o2, e1).items():
        assert (f"{totals[ccy][0]:.6f}", f"{totals[ccy][1]:.6f}") == (f"{u:.6f}", f"{am:.6f}")
    assert totals["USD"] != _sum(s1, o1)["USD"], "the restatement changed the figures"

    # Rows carry their tenant, source, assembly and the unmapped columns.
    tenants = {r[0]: r[1] for r in rows(dsn, "SELECT native_id, tenant_id"
                                             " FROM cloudops.cloud_account")}
    for r in rows(dsn, _COST + " LIMIT 50"):
        assert r[11] == tenants[r[0]]
        assert r[10] == "aws"
        parts = r[12].get("parts", [r[12]])
        assert all(p["lines"] >= 1 and p["line_item_types"] for p in parts)
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact"
                     " WHERE raw_extra ? 'parts'")[0][0] > 0, "some keys span files"
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact WHERE raw_extra->'columns'"
                     " ->> 'lineItem/LineItemDescription' = %s", INJECTION)[0][0] > 0
    assert rows(dsn, "SELECT count(*) FROM cloudops.cost_fact WHERE charge_class = 'marketplace'"
                ) == [(1,)]

    # Unknown account: quarantined with every sighting, nothing written for it.
    unknown = sum(a.unknown_account_lines for a in (s1, o1, o2))
    assert dict(rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                          " WHERE source_system = 'aws'")) == {UNREGISTERED: unknown}
    assert rows(dsn, "SELECT count(*) FROM cloudops.cloud_account WHERE native_id = %s",
                UNREGISTERED) == [(0,)]
    # A resource nobody has: the cost is kept with no resource, the id quarantined.
    unlinked = sum(a.unlinked[UNKNOWN_RESOURCE] for a in (s1, o1, o2))
    assert rows(dsn, "SELECT occurrence_count, account_id IS NOT NULL"
                     " FROM staging.resource_quarantine WHERE external_key = %s",
                UNKNOWN_RESOURCE) == [(unlinked, True)]
    rejected = first.counts.rejected + second.counts.rejected
    assert rejected == sum(a.malformed_lines for a in truth.values()) + e1.malformed_lines, (
        "E1's first file (with its malformed line) is read in both runs")

    assert raw_first(raw.events)
    assert rows(dsn, "SELECT assembly_id FROM staging.cur_assembly WHERE swapped_at IS NOT NULL"
                     " ORDER BY 1") == sorted((a.assembly_id,) for a in truth.values())
    assert _mark(dsn, source, mapping.CUR, USD_REPORT) == o2.manifest_last_modified
    assert _mark(dsn, source, mapping.CUR, EUR_REPORT) == e1.manifest_last_modified


async def test_cur_replay_is_idempotent_never_restores_a_restated_assembly_or_moves_a_watermark(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    truth = cur_expectations()
    raw, s3 = MemoryRawStore(), FakeS3.from_fixtures()
    source = _source(FakeConfig.from_fixtures(), s3)
    probe = _probe(db, raw, source, dsn, truth)
    (first,) = await run(env, probe, ConnectorIngest.run, [IngestRequest(mapping.CUR, SINCE)])
    s3.advance()
    await run(env, probe, ConnectorIngest.run, [IngestRequest(mapping.CUR, SINCE)])
    snapshot = _cost(dsn)
    marks = [_mark(dsn, source, mapping.CUR, r) for r in (USD_REPORT, EUR_REPORT)]
    requests = len(s3.requests)
    on = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, probe, ConnectorReplay.run, [ReplayRequest(mapping.CUR, on, on)])
    assert replay.status == "succeeded"
    assert _cost(dsn) == snapshot
    # Whatever order that took, replay the first run's pages last, by hand: O1 is
    # complete again, but O2 restated it, so O1 swaps nothing back.
    env_ = ActivityEnvironment()
    for ref in sorted(r for r in raw.pages if first.run_id in r):
        await env_.run(probe.process, ProcessRequest(mapping.CUR, ref, "replay-by-hand",
                                                     datetime.now(UTC).isoformat(), False))
    assert _cost(dsn) == snapshot
    assert len(s3.requests) == requests, "replay never touches the bucket"
    assert [_mark(dsn, source, mapping.CUR, r) for r in (USD_REPORT, EUR_REPORT)] == marks
    assert rows(dsn, "SELECT swapped_at IS NULL FROM staging.cur_assembly"
                     " WHERE assembly_id = %s", truth["O1"].assembly_id) == [(False,)]


async def test_a_cur_rerun_with_nothing_new_changes_nothing(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    _clear(dsn)
    raw, s3 = MemoryRawStore(), FakeS3.from_fixtures()
    s3.advance()
    source = _source(FakeConfig.from_fixtures(), s3)
    acts = ConnectorActivities(db, raw, source)
    await run(env, acts, ConnectorIngest.run, [IngestRequest(mapping.CUR, SINCE)])
    snapshot = rows(dsn, "SELECT id, unblended_cost FROM cloudops.cost_fact ORDER BY 1")
    (again,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(mapping.CUR, SINCE)])
    assert again.status == "succeeded"
    # The last manifests are re-listed in the overlap, but swapped assemblies are
    # not reloaded: the very same rows remain.
    assert again.counts.written == 0
    assert rows(dsn, "SELECT id, unblended_cost FROM cloudops.cost_fact ORDER BY 1") == snapshot
