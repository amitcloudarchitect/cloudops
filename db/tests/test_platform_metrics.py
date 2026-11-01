"""Platform gauges from the database (step 19; ADR 0022): connector health,
quarantine depth, reconciliation and the platform's own cost (its resources' lines
in the host tenant, ADR 0031), with no tenant anywhere in a label."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from cloudops_connectors.platform_metrics import GAUGES, PLATFORM_GROUP, collect
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
    """The tenant with the most resource-linked cost this month hosts the platform,
    and the resources of one of its accounts sit in a platform resource group; a
    second tenant is internal too, and hosts nothing."""
    with psycopg.connect(data.admin_dsn) as cx:
        (tenant,) = cx.execute(
            "SELECT tenant_id FROM cloudops.cost_fact WHERE invoice_period = %s"
            " AND resource_id IS NOT NULL GROUP BY tenant_id ORDER BY count(*) DESC LIMIT 1",
            (TODAY.strftime("%Y-%m"),)).fetchone() or (None,)
        assert tenant is not None, "the synthetic dataset has cost this month"
        cx.execute("UPDATE cloudops.tenant SET internal = true, hosts_platform = true"
                   " WHERE id = %s", (tenant,))
        cx.execute("UPDATE cloudops.tenant SET internal = true WHERE id = (SELECT id FROM"
                   " cloudops.tenant WHERE id <> %s ORDER BY id LIMIT 1)", (tenant,))
        # Half the host's resources move into the platform's groups; the rest are
        # the subscription's other workloads (in dev, the lab VMs).
        cx.execute(
            "UPDATE cloudops.resource r SET native_id = '/subscriptions/x/resourceGroups/'"
            " || CASE WHEN n %% 3 = 0 THEN 'MC_rg-cloudops-aks-dev-weu_aks/'"
            "         ELSE 'rg-cloudops-app-dev-weu/' END || r.id"
            " FROM (SELECT id, row_number() OVER (ORDER BY id) AS n FROM cloudops.resource"
            "       WHERE tenant_id = %s) k WHERE k.id = r.id AND k.n %% 2 = 0", (tenant,))
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


async def test_own_cost_is_the_platform_resources_in_exact_micros(
    data: Synthetic, internal: str, snapshot: Snapshot
) -> None:
    with psycopg.connect(data.admin_dsn) as cx:
        expected = cx.execute(
            "SELECT c.charge_class::text, c.currency, sum(c.amortized_cost)"
            " FROM cloudops.cost_fact c JOIN cloudops.resource r ON r.id = c.resource_id"
            " WHERE c.tenant_id = %s AND c.invoice_period = %s"
            "   AND lower(r.native_id) ~ %s GROUP BY 1, 2",
            (internal, TODAY.strftime("%Y-%m"), PLATFORM_GROUP)).fetchall()
        (whole,) = cx.execute(
            "SELECT sum(amortized_cost) FROM cloudops.cost_fact WHERE tenant_id = %s"
            " AND invoice_period = %s", (internal, TODAY.strftime("%Y-%m"))).fetchone() or (0,)
    assert expected
    # The platform's part, never the whole subscription.
    assert sum(Decimal(a) for _, _, a in expected) < Decimal(whole)
    assert _one(snapshot, "cloudops.platform.cost_resource_lines") == 1
    reported = {(lab["charge_class"], lab["currency"]): v
                for lab, v in snapshot.values["cloudops.platform.cost_micros"]}
    assert reported == {(c, ccy): micros(Decimal(a)) for c, ccy, a in expected}
    assert all(isinstance(v, int) for v in reported.values())
    # No reconciliation has run for it in this database: not verified, never assumed.
    assert _one(snapshot, "cloudops.platform.cost_verified") == 0
    shares = snapshot.values.get("cloudops.platform.cost_unallocated_share", [])
    assert {lab["dimension"] for lab, _ in shares} <= {"application", "cost_centre"}
    assert all(0 <= v <= 1 for _, v in shares)


async def test_without_a_platform_host_there_is_no_own_cost(
    data: Synthetic, internal: str
) -> None:
    """Internal tenants alone say nothing about the platform's cost."""
    with psycopg.connect(data.admin_dsn) as cx:
        cx.execute("UPDATE cloudops.tenant SET hosts_platform = false WHERE id = %s", (internal,))
    db = Database.create(data.ingest_dsn, pool_size=1, max_overflow=0)
    try:
        snap = await collect(db, TODAY)
    finally:
        await db.dispose()
        with psycopg.connect(data.admin_dsn) as cx:
            cx.execute("UPDATE cloudops.tenant SET hosts_platform = true WHERE id = %s",
                       (internal,))
    assert "cloudops.platform.cost_micros" not in snap.values
    assert "cloudops.platform.cost_resource_lines" not in snap.values
    assert "cloudops.connector.expected_interval" in snap.values


def test_many_tenants_may_be_internal_but_one_hosts(data: Synthetic, internal: str) -> None:
    with psycopg.connect(data.admin_dsn) as cx:
        (count,) = cx.execute("SELECT count(*) FROM cloudops.tenant WHERE internal"
                              ).fetchone() or (0,)
        assert count >= 2
        with pytest.raises(psycopg.errors.UniqueViolation):
            cx.execute("UPDATE cloudops.tenant SET hosts_platform = true WHERE internal")


async def test_the_dev_seed_loads_the_internal_tenant(admin_server: str) -> None:
    """`make seed`'s path: the dataset with the internal tenant loads, passes the
    isolation check, and leaves it the platform's host, with cost but no
    resource-level line (so the platform's part reads as not captured)."""
    from dataclasses import replace as dc_replace

    from cloudops_devdata.seed import demo_internal, demo_spike, load, mark_internal, verify
    from cloudops_devdata.synthetic import generate

    from .conftest import _database

    ds = dc_replace(generate(TODAY, 7), work_items=[], alias_quarantine=[],
                    resource_quarantine=[])
    demo_spike(ds)
    internal = demo_internal(ds)
    with _database(admin_server, "seed") as d:
        ingest = Database.create(d.ingest_dsn, pool_size=1, max_overflow=0)
        app = Database.create(d.app_dsn, pool_size=1, max_overflow=0)
        try:
            await load(ingest, ds)
            await mark_internal(ingest, internal)
            await verify(app, ds)
        finally:
            await ingest.dispose()
            await app.dispose()
        with psycopg.connect(d.admin_dsn) as cx:
            rows = cx.execute("SELECT t.id, count(c.*), count(c.resource_id)"
                              " FROM cloudops.tenant t"
                              " JOIN cloudops.cost_fact c ON c.tenant_id = t.id"
                              " WHERE t.hosts_platform AND t.internal GROUP BY t.id").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == internal
    assert rows[0][1] > 0
    assert rows[0][2] == 0
