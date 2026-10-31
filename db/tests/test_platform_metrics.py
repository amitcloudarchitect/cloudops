"""Platform gauges from the database (step 19; ADR 0022): connector health,
quarantine depth, reconciliation and the platform's own cost, with no tenant
anywhere in a label."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from cloudops_connectors.platform_metrics import GAUGES, collect
from cloudops_devdata.servicenow_fixtures import FIXTURE_ANCHOR
from cloudops_observability import micros
from cloudops_observability.gauges import Snapshot
from cloudops_tenancy import Database

from .conftest import Synthetic, _synthetic

pytestmark = [pytest.mark.integration]

TODAY = date.fromisoformat(str(FIXTURE_ANCHOR)[:10])


@pytest.fixture(scope="module")
async def data(admin_server: str) -> AsyncIterator[Synthetic]:
    async for s in _synthetic(admin_server, "metrics"):
        yield s


@pytest.fixture(scope="module")
def internal(data: Synthetic) -> str:
    """The tenant with the most cost this month plays Getronics itself."""
    with psycopg.connect(data.admin_dsn) as cx:
        (tenant,) = cx.execute(
            "SELECT tenant_id FROM cloudops.cost_fact WHERE invoice_period = %s"
            " GROUP BY tenant_id ORDER BY count(*) DESC LIMIT 1",
            (TODAY.strftime("%Y-%m"),)).fetchone() or (None,)
        assert tenant is not None, "the synthetic dataset has cost this month"
        cx.execute("UPDATE cloudops.tenant SET internal = true WHERE id = %s", (tenant,))
        cx.execute(
            "INSERT INTO staging.tenant_alias_quarantine (source_system, alias, occurrence_count)"
            " VALUES ('servicenow', 'Unmatched Holdings Ltd', 3)"
            " ON CONFLICT (source_system, alias) DO NOTHING")
    return str(tenant)


@pytest.fixture
async def snapshot(data: Synthetic, internal: str) -> Snapshot:
    db = Database.create(data.ingest_dsn, pool_size=1, max_overflow=0)
    try:
        return await collect(db, TODAY)
    finally:
        await db.dispose()


def _one(snap: Snapshot, name: str, **labels: str) -> float | int:
    hits = [v for lab, v in snap.values.get(name, [])
            if all(lab.get(k) == want for k, want in labels.items())]
    assert len(hits) == 1, (name, labels, snap.values.get(name))
    return hits[0]


async def test_every_label_is_free_of_tenants(data: Synthetic, snapshot: Snapshot) -> None:
    with psycopg.connect(data.admin_dsn) as cx:
        tenants = cx.execute("SELECT id::text, canonical_name, short_code FROM cloudops.tenant"
                             " UNION ALL SELECT tenant_id::text, alias, '' FROM"
                             " cloudops.tenant_alias").fetchall()
    forbidden = {v for row in tenants for v in row if v}
    values = {str(v) for series in snapshot.values.values() for labels, _ in series
              for v in labels.values()}
    keys = {k for series in snapshot.values.values() for labels, _ in series for k in labels}
    assert not values & forbidden
    assert keys <= {"source", "entity", "kind", "reason", "charge_class", "currency",
                    "dimension"}
    assert set(snapshot.values) <= {g.name for g in GAUGES}


async def test_connector_health_covers_every_schedule(
    data: Synthetic, snapshot: Snapshot
) -> None:
    with psycopg.connect(data.admin_dsn) as cx:
        schedules = cx.execute("SELECT source_system, entity, extract(epoch FROM every)::bigint"
                               " FROM staging.connector_schedule").fetchall()
    assert schedules
    for source, entity, every in schedules:
        assert _one(snapshot, "cloudops.connector.expected_interval",
                    source=source, entity=entity) == every
        assert _one(snapshot, "cloudops.connector.runs_24h", source=source, entity=entity) >= 0


async def test_quarantine_depth_is_counted_per_source(snapshot: Snapshot) -> None:
    assert _one(snapshot, "cloudops.quarantine.depth", kind="alias", source="servicenow") >= 1
    assert _one(snapshot, "cloudops.quarantine.occurrences", kind="alias",
                source="servicenow") >= 3


async def test_own_cost_is_the_internal_tenants_in_exact_micros(
    data: Synthetic, internal: str, snapshot: Snapshot
) -> None:
    with psycopg.connect(data.admin_dsn) as cx:
        expected = cx.execute(
            "SELECT charge_class::text, currency, sum(amortized_cost) FROM cloudops.cost_fact"
            " WHERE tenant_id = %s AND invoice_period = %s GROUP BY 1, 2",
            (internal, TODAY.strftime("%Y-%m"))).fetchall()
    assert expected
    reported = {(lab["charge_class"], lab["currency"]): v
                for lab, v in snapshot.values["cloudops.platform.cost_micros"]}
    assert reported == {(c, ccy): micros(Decimal(a)) for c, ccy, a in expected}
    assert all(isinstance(v, int) for v in reported.values())
    # No reconciliation has run for it in this database: not verified, never assumed.
    assert _one(snapshot, "cloudops.platform.cost_verified") == 0
    shares = snapshot.values.get("cloudops.platform.cost_unallocated_share", [])
    assert {lab["dimension"] for lab, _ in shares} <= {"application", "cost_centre"}
    assert all(0 <= v <= 1 for _, v in shares)


async def test_without_an_internal_tenant_there_is_no_own_cost(
    data: Synthetic, internal: str
) -> None:
    with psycopg.connect(data.admin_dsn) as cx:
        cx.execute("UPDATE cloudops.tenant SET internal = false WHERE id = %s", (internal,))
    db = Database.create(data.ingest_dsn, pool_size=1, max_overflow=0)
    try:
        snap = await collect(db, TODAY)
    finally:
        await db.dispose()
        with psycopg.connect(data.admin_dsn) as cx:
            cx.execute("UPDATE cloudops.tenant SET internal = true WHERE id = %s", (internal,))
    assert "cloudops.platform.cost_micros" not in snap.values
    assert "cloudops.connector.expected_interval" in snap.values


def test_only_one_tenant_can_be_internal(data: Synthetic, internal: str) -> None:
    with psycopg.connect(data.admin_dsn) as cx, pytest.raises(psycopg.errors.UniqueViolation):
        cx.execute("UPDATE cloudops.tenant SET internal = true WHERE id <> %s", (internal,))
