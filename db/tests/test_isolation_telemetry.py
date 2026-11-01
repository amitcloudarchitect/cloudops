"""Isolation on the telemetry column store (LLD, Telemetry store; Isolation
cases). The store has no row-level security: the query wrapper in
``cloudops_tenancy.telemetry`` is the isolation, so the suite exercises that
path explicitly, against a real store, as the application's own user."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from cloudops_tenancy import Role, TenantScope
from cloudops_tenancy.telemetry import (
    Aggregate,
    Grain,
    MetricQuery,
    MetricSample,
    TelemetryScopeError,
    TelemetryStore,
)

from .conftest import Telemetry

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

# Relative to now: the store's TTL expires rows by the real clock (90 days raw).
T0 = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)


class World:
    """Three tenants, two accounts for the first, one resource per account."""

    def __init__(self) -> None:
        self.a, self.b, self.c = uuid4(), uuid4(), uuid4()
        self.a1, self.a2, self.b1, self.c1 = uuid4(), uuid4(), uuid4(), uuid4()
        self.res = {acct: uuid4() for acct in (self.a1, self.a2, self.b1, self.c1)}
        self.owner = {self.a1: self.a, self.a2: self.a, self.b1: self.b, self.c1: self.c}

    def samples(self, minutes: int = 12) -> list[MetricSample]:
        return [
            MetricSample(self.owner[acct], acct, res, "cpu.utilisation",
                         T0 + timedelta(minutes=m), float(10 + m), "percent", "test")
            for acct, res in self.res.items() for m in range(minutes)
        ]

    def ingest(self) -> TenantScope:
        return TenantScope([self.a, self.b, self.c], Role.INGEST, "connector:telemetry")


def _q(grain: Grain = Grain.RAW, **kw: object) -> MetricQuery:
    return MetricQuery("cpu.utilisation", T0 - timedelta(hours=1), T0 + timedelta(hours=2),
                       grain=grain, **kw)  # type: ignore[arg-type]


@pytest.fixture
async def store(telemetry_store: Telemetry) -> AsyncIterator[TelemetryStore]:
    s = TelemetryStore.create(telemetry_store.url, "telemetry", telemetry_store.username,
                              telemetry_store.password)
    yield s
    await s.aclose()


@pytest.fixture
async def world(store: TelemetryStore) -> World:
    w = World()
    await store.write(w.ingest(), w.samples(), dedupe_token=f"seed:{w.a}")
    return w


def _count(t: Telemetry, tenant: UUID, table: str = "metric_sample") -> int:
    sql = (f"SELECT count() FROM telemetry.{table} WHERE tenant_id = '{tenant}'"
           " FORMAT TabSeparated")
    return int(t.admin.post("/", content=sql.encode()).text)


@pytest.mark.parametrize("grain", list(Grain))
async def test_a_tenant_scope_reads_only_its_own_rows(
    store: TelemetryStore, world: World, grain: Grain
) -> None:
    points = await store.query(TenantScope([world.a], Role.ENGINEER, "user:a"), _q(grain))
    assert points
    assert {p.tenant_id for p in points} == {world.a}
    assert {p.resource_id for p in points} == {world.res[world.a1], world.res[world.a2]}


async def test_naming_another_tenants_resource_returns_nothing(
    store: TelemetryStore, world: World
) -> None:
    """Resource ids are a filter inside the scope, never a way out of it."""
    q = _q(resource_ids=(world.res[world.b1], world.res[world.c1]))
    assert await store.query(TenantScope([world.a], Role.ENGINEER, "user:a"), q) == []


async def test_an_empty_scope_returns_zero_rows_not_all_rows(
    store: TelemetryStore, world: World, telemetry_store: Telemetry
) -> None:
    assert _count(telemetry_store, world.b) > 0
    assert await store.query(TenantScope([], Role.PLATFORM_ADMIN, "user:x"), _q()) == []


async def test_a_subscription_limited_scope_sees_only_its_accounts(
    store: TelemetryStore, world: World
) -> None:
    scope = TenantScope([world.a], Role.CUSTOMER, "customer:a", account_ids=[world.a1])
    for grain in Grain:
        points = await store.query(scope, _q(grain))
        assert {p.resource_id for p in points} == {world.res[world.a1]}
    nothing = TenantScope([world.a], Role.CUSTOMER, "customer:a", account_ids=[])
    assert await store.query(nothing, _q()) == []


async def test_a_two_tenant_scope_sees_exactly_those_two(
    store: TelemetryStore, world: World
) -> None:
    points = await store.query(TenantScope([world.a, world.b], Role.FINOPS, "user:f"), _q())
    assert {p.tenant_id for p in points} == {world.a, world.b}


async def test_a_write_for_a_tenant_outside_the_scope_stores_nothing(
    store: TelemetryStore, world: World, telemetry_store: Telemetry
) -> None:
    before = _count(telemetry_store, world.c)
    stray = world.samples(1)
    with pytest.raises(TelemetryScopeError):
        await store.write(TenantScope([world.a], Role.INGEST, "connector:telemetry"), stray,
                          dedupe_token=f"stray:{world.a}")
    assert _count(telemetry_store, world.c) == before
    assert _count(telemetry_store, world.a) == 24  # 2 resources x 12 minutes, unchanged


async def test_a_retried_batch_is_stored_once_rollups_included(
    store: TelemetryStore, telemetry_store: Telemetry
) -> None:
    w = World()
    batch = w.samples(6)
    for _ in range(3):  # a crash before the checkpoint, and the batch is delivered again
        await store.write(w.ingest(), batch, dedupe_token=f"metrics:0:{w.a}")
    assert _count(telemetry_store, w.a) == 12
    (point,) = await store.query(
        TenantScope([w.a], Role.ENGINEER, "user:a"),
        _q(Grain.HOUR, resource_ids=(w.res[w.a1],), aggregate=Aggregate.COUNT))
    assert point.value == 6.0


async def test_rollups_aggregate_the_raw_grain_correctly(
    store: TelemetryStore, world: World
) -> None:
    scope = TenantScope([world.b], Role.ENGINEER, "user:b")
    by = {a: (await store.query(scope, _q(Grain.FIVE_MINUTES, aggregate=a)))
          for a in (Aggregate.AVG, Aggregate.MIN, Aggregate.MAX, Aggregate.COUNT)}
    # Minutes 0-4 hold 10..14, 5-9 hold 15..19, 10-11 hold 20..21.
    assert [p.value for p in by[Aggregate.AVG]] == [12.0, 17.0, 20.5]
    assert [p.value for p in by[Aggregate.MIN]] == [10.0, 15.0, 20.0]
    assert [p.value for p in by[Aggregate.MAX]] == [14.0, 19.0, 21.0]
    assert [p.value for p in by[Aggregate.COUNT]] == [5.0, 5.0, 2.0]


def test_the_application_user_cannot_change_the_schema(telemetry_store: Telemetry) -> None:
    """Per-service users with SELECT and INSERT only (LLD); no default user."""
    import httpx

    with httpx.Client(base_url=telemetry_store.url, headers={
        "X-ClickHouse-User": telemetry_store.username,
        "X-ClickHouse-Key": telemetry_store.password,
    }) as app:
        for ddl in ("ALTER TABLE telemetry.metric_sample DELETE WHERE 1",
                    "TRUNCATE TABLE telemetry.metric_sample",
                    "CREATE TABLE telemetry.x (a UInt8) ENGINE = Memory"):
            assert app.post("/", content=ddl.encode()).status_code != 200
    default = httpx.post(telemetry_store.url, content=b"SELECT 1")
    assert default.status_code != 200, "no default user"


async def test_a_purge_deletes_one_tenant_at_every_grain(
    store: TelemetryStore, world: World, telemetry_store: Telemetry
) -> None:
    """ADR 0038. The application's user (SELECT and INSERT) cannot delete at all; a user
    that may (``ALTER DELETE``, for a purge) deletes only the one tenant it is given."""
    from cloudops_tenancy.telemetry import TelemetryError

    with pytest.raises(TelemetryError):
        await store.purge(TenantScope([world.a], Role.INGEST, "worker:purge"))
    user, password = f"cloudops_purge_{uuid4().hex[:8]}", uuid4().hex
    for statement in (f"CREATE USER {user} IDENTIFIED WITH sha256_password BY '{password}'",
                      f"GRANT SELECT, INSERT, ALTER DELETE ON telemetry.* TO {user}"):
        telemetry_store.admin.post("/", content=statement.encode()).raise_for_status()
    purger = TelemetryStore.create(telemetry_store.url, "telemetry", user, password)
    try:
        await purger.purge(TenantScope([world.a], Role.INGEST, "worker:purge"))
    finally:
        await purger.aclose()
        telemetry_store.admin.post("/", content=f"DROP USER IF EXISTS {user}".encode())
    for table in ("metric_sample", "metric_5m", "metric_1h"):
        assert _count(telemetry_store, world.a, table) == 0, table
        assert _count(telemetry_store, world.b, table) > 0, table
        assert _count(telemetry_store, world.c, table) > 0, table
