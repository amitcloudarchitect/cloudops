"""The ECB reference-rate connector end to end: the generic connector workflow
on a time-skipping Temporal server, the migrated database (a module of its own;
its ``fx_rate`` is emptied before each test, so whatever is there the connector
put there), and the recorded history files served by the fake ECB host.

Ground truth comes from ``cloudops_devdata.ecb_fixtures.expectations()``:
every directed rate of the currency set, computed from the generated rates.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
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
    scope_key,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.ecb import mapping
from cloudops_connectors.ecb.client import EcbClient
from cloudops_connectors.ecb.fake import FakeEcb
from cloudops_connectors.ecb.source import REPLACED, REVISED, EcbSource
from cloudops_devdata import ecb_fixtures
from cloudops_tenancy import Database

from .conftest import Synthetic
from .connectors import SINCE, raw_first, rows, run

pytestmark = [pytest.mark.integration]

ENTITY = mapping.ENTITY
_RATES = ("SELECT from_currency, to_currency, rate_type, effective_date, rate, source"
          " FROM cloudops.fx_rate")
_RESTATED = ("SELECT from_currency, to_currency, rate_type, effective_date, previous_rate,"
             " new_rate, source, reason, entered_by FROM cloudops.fx_restatement")


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
        for table in ("cloudops.fx_rate", "cloudops.fx_restatement"):
            cx.execute(f"DELETE FROM {table}")


def _source(fake: FakeEcb, currencies: tuple[str, ...] = mapping.DEFAULT_CURRENCIES
            ) -> EcbSource:
    base = f"https://t{uuid.uuid4().hex[:8]}.ecb.example"  # its own watermark
    return EcbSource(EcbClient(base, transport=fake.transport()), currencies)


def _stored(dsn: str) -> dict[tuple[str, str, str, date], tuple[Decimal, str]]:
    return {(r[0], r[1], r[2], r[3]): (r[4], r[5]) for r in rows(dsn, _RATES)}


def _truth(revised: bool = False) -> dict[tuple[str, str, str, date], Decimal]:
    rates = ecb_fixtures.revised() if revised else ecb_fixtures.published()
    return {(e.from_currency, e.to_currency, e.rate_type, e.effective_date): e.rate
            for e in ecb_fixtures.expectations(rates)}


def _mark(dsn: str, source: EcbSource) -> Any:
    found = rows(dsn, "SELECT watermark FROM staging.ingest_watermark"
                      " WHERE source_system = 'ecb' AND entity = %s AND scope_key = %s",
                 ENTITY, scope_key(source.instance, ""))
    return found[0][0] if found else None


def _insert(dsn: str, frm: str, to: str, kind: str, day: date, rate: str, source: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate, rate_type,"
                   " effective_date, source) VALUES (%s, %s, %s, %s, %s, %s)",
                   (frm, to, Decimal(rate), kind, day, source))


async def test_every_directed_rate_lands_with_the_watermark_and_raw_first(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    raw = MemoryRawStore()
    source = _source(FakeEcb.from_fixtures())
    (report,) = await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
                          [IngestRequest(ENTITY, SINCE)])
    truth = _truth()
    assert report.status == "succeeded"
    assert (report.counts.pages, report.counts.records, report.counts.written,
            report.counts.rejected) == (1, len(truth), len(truth), 0)
    stored = _stored(dsn)
    assert {k: v[0] for k, v in stored.items()} == truth
    assert {v[1] for v in stored.values()} == {"ecb"}
    # Two or more currencies, both directions, average daily and closing at month end.
    assert {k[0] for k in stored} == {"EUR", "GBP", "USD"}
    assert {k[2] for k in stored} == {"average", "closing"}
    assert all(isinstance(v[0], Decimal) for v in stored.values())
    assert rows(dsn, "SELECT count(*) FROM cloudops.fx_restatement") == [(0,)]
    assert _mark(dsn, source) == datetime(2026, 10, 30, tzinfo=UTC)
    assert raw_first(raw.events)
    assert all(r.startswith("ecb/fx/") for r in raw.pages)
    # The application's lookup reads them: a GBP flow on a Saturday converts at
    # Friday's rate, and back again.
    friday = date(2026, 10, 23)
    got = rows(dsn, "SELECT rate, effective_date FROM cloudops.fx_rate_on('GBP', 'USD',"
                    " %s, 'average')", date(2026, 10, 24))
    assert got == [(truth[("GBP", "USD", "average", friday)], friday)]
    assert rows(dsn, "SELECT effective_date FROM cloudops.fx_rate_on('EUR', 'GBP',"
                     " %s, 'closing')", date(2026, 10, 15)) == [(date(2026, 9, 30),)]


async def test_a_rerun_and_a_replay_change_nothing_and_replay_moves_no_watermark(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    raw, fake = MemoryRawStore(), FakeEcb.from_fixtures()
    source = _source(fake)
    acts = ConnectorActivities(db, raw, source)
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    snapshot = rows(dsn, "SELECT id, rate, source FROM cloudops.fx_rate ORDER BY id")
    mark = _mark(dsn, source)

    (again,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert again.status == "succeeded"
    assert again.counts.written == 0
    assert again.counts.records < len(_truth()), "only the lookback window is compared"
    assert source.last is not None
    assert source.last.unchanged == again.counts.records
    requests = len(fake.requests)
    on = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ENTITY, on, on)])
    assert replay.status == "succeeded"
    assert replay.counts.written == 0
    assert len(fake.requests) == requests, "replay never touches the ECB"
    assert rows(dsn, "SELECT id, rate, source FROM cloudops.fx_rate ORDER BY id") == snapshot
    assert rows(dsn, "SELECT count(*) FROM cloudops.fx_restatement") == [(0,)]
    assert _mark(dsn, source) == mark


async def test_an_ecb_revision_updates_the_rate_and_records_the_restatement(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    fake = FakeEcb.from_fixtures()
    source = _source(fake)
    acts = ConnectorActivities(db, MemoryRawStore(), source)
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    before = _stored(dsn)
    fake.revise()
    (second,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])

    day, ccy, published, revised = ecb_fixtures.revised_rate()
    old, new = _truth(), _truth(revised=True)
    changed = {k for k in new if new[k] != old[k]}
    assert changed, "the revision moves every pair with the revised currency that day"
    assert all(k[3] == day and ccy in (k[0], k[1]) for k in changed)
    assert second.counts.written == len(changed)
    assert source.last is not None
    assert source.last.revised == len(changed)
    after = _stored(dsn)
    assert {k: v[0] for k, v in after.items()} == new
    assert {k: v for k, v in after.items() if k not in changed} == {
        k: v for k, v in before.items() if k not in changed}
    assert after[("EUR", ccy, "average", day)] == (revised, "ecb")
    entries = rows(dsn, _RESTATED)
    assert sorted((r[0], r[1], r[2], r[3]) for r in entries) == sorted(changed)
    for r in entries:
        key = (r[0], r[1], r[2], r[3])
        assert (r[4], r[5]) == (old[key], new[key])
        assert (r[6], r[7], r[8]) == ("ecb", REVISED, "connector:ecb")
    eur = next(r for r in entries if (r[0], r[1]) == ("EUR", ccy))
    assert (eur[4], eur[5]) == (published, revised)


async def test_a_finance_correction_is_never_overwritten(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    day, ccy, _, _ = ecb_fixtures.revised_rate()
    other = date(2026, 8, 31)
    # Finance corrected the GBP rate on the day the ECB later revises, and a
    # USD closing rate the ECB never changes.
    _insert(dsn, "EUR", ccy, "average", day, "0.90000000", "finance")
    _insert(dsn, "USD", "EUR", "closing", other, "0.95000000", "finance")
    fake = FakeEcb.from_fixtures()
    source = _source(fake)
    acts = ConnectorActivities(db, MemoryRawStore(), source)
    (first,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert source.last is not None
    assert source.last.skipped == 2
    assert first.counts.written == len(_truth()) - 2
    fake.revise()
    await run(env, acts, ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert source.last is not None
    assert source.last.skipped == 1, "the closing day is outside the second run's lookback"
    stored = _stored(dsn)
    assert stored[("EUR", ccy, "average", day)] == (Decimal("0.90000000"), "finance")
    assert stored[("USD", "EUR", "closing", other)] == (Decimal("0.95000000"), "finance")
    entries = rows(dsn, _RESTATED)
    assert ("EUR", ccy, "average", day) not in {(r[0], r[1], r[2], r[3]) for r in entries}
    assert all(r[6] == "ecb" for r in entries)


async def test_a_rate_from_another_feed_is_replaced_with_an_explicit_entry(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    day = date(2026, 10, 1)
    _insert(dsn, "GBP", "USD", "average", day, "1.11111111", "synthetic-seed")
    _insert(dsn, "USD", "GBP", "average", day, "1.00000000", "synthetic-seed")
    source = _source(FakeEcb.from_fixtures())
    await run(env, ConnectorActivities(db, MemoryRawStore(), source), ConnectorIngest.run,
              [IngestRequest(ENTITY, SINCE)])
    truth = _truth()
    stored = _stored(dsn)
    assert stored[("GBP", "USD", "average", day)] == (truth[("GBP", "USD", "average", day)],
                                                       "ecb")
    entries = {(r[0], r[1]): r for r in rows(dsn, _RESTATED)}
    assert set(entries) == {("GBP", "USD"), ("USD", "GBP")}
    assert entries[("GBP", "USD")][4] == Decimal("1.11111111")
    assert entries[("GBP", "USD")][7] == REPLACED


async def test_a_wider_currency_set_stores_every_pair_among_it(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    source = _source(FakeEcb.from_fixtures(), ("GBP", "USD", "CHF"))
    (report,) = await run(env, ConnectorActivities(db, MemoryRawStore(), source),
                          ConnectorIngest.run, [IngestRequest(ENTITY, SINCE)])
    assert report.status == "succeeded"
    pairs = {(r[0], r[1]) for r in rows(dsn, _RATES)}
    assert len(pairs) == 12
    assert ("CHF", "GBP") in pairs
    assert ("GBP", "CHF") in pairs


async def test_a_malformed_file_dead_letters_in_bronze_and_moves_nothing(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic
) -> None:
    dsn = synthetic_module.admin_dsn
    fake = FakeEcb(b"<html>maintenance</html")
    raw = MemoryRawStore()
    source = _source(fake)
    (report,) = await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
                          [IngestRequest(ENTITY, SINCE)])
    assert report.status == "dead_lettered"
    assert list(raw.pages.values()) == [b"<html>maintenance</html"]
    assert rows(dsn, "SELECT count(*) FROM cloudops.fx_rate") == [(0,)]
    assert _mark(dsn, source) is None
    assert rows(dsn, "SELECT count(*) FROM staging.ingest_dead_letter WHERE source_system = 'ecb'"
                     " AND raw_ref = %s", next(iter(raw.pages)))[0][0] == 1
