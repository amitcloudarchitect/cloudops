"""The telemetry wrapper without a store: what it sends, and what it refuses
to send. The isolation suite (db/tests/test_isolation_telemetry.py) runs it
against a real column store."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cloudops_tenancy import Role, TenantScope
from cloudops_tenancy.telemetry import (
    Aggregate,
    Grain,
    MetricQuery,
    MetricSample,
    TelemetryError,
    TelemetryScopeError,
    TelemetryStore,
    build_query,
)

A, B = uuid4(), uuid4()
ACCT = uuid4()
T0 = datetime(2026, 10, 30, tzinfo=UTC)


def _q(**kw: object) -> MetricQuery:
    base: dict[str, object] = {"metric": "cpu.utilisation", "start": T0,
                               "end": T0 + timedelta(hours=1)}
    base.update(kw)
    return MetricQuery(**base)  # type: ignore[arg-type]


def _sample(tenant: UUID = A, account: UUID = ACCT, **kw: Any) -> MetricSample:
    base: dict[str, object] = {"tenant_id": tenant, "account_id": account,
                               "resource_id": uuid4(), "metric": "cpu.utilisation",
                               "ts": T0, "value": 42.5, "unit": "percent",
                               "source": "azure_monitor"}
    base.update(kw)
    return MetricSample(**base)  # type: ignore[arg-type]


class _Recorder:
    def __init__(self, body: str = "") -> None:
        self.requests: list[httpx.Request] = []
        self.body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, text=self.body)


def _store(rec: _Recorder) -> TelemetryStore:
    return TelemetryStore.create("http://store", "telemetry", "u", "p",
                                 transport=httpx.MockTransport(rec))


@pytest.mark.parametrize("grain", list(Grain))
@pytest.mark.parametrize("agg", list(Aggregate))
def test_every_statement_starts_with_the_tenant_predicate(grain: Grain, agg: Aggregate) -> None:
    sql, params = build_query("telemetry", TenantScope([A], Role.ENGINEER, "p"),
                              _q(grain=grain, aggregate=agg))
    assert "WHERE tenant_id IN {tenants:Array(UUID)}" in sql
    assert params["tenants"] == f"['{A}']"
    assert "account_id" not in sql  # an unrestricted scope


def test_a_restricted_scope_adds_the_account_predicate() -> None:
    sql, params = build_query("telemetry",
                              TenantScope([A], Role.CUSTOMER, "p", account_ids=[ACCT]), _q())
    assert "AND account_id IN {accounts:Array(UUID)}" in sql
    assert params["accounts"] == f"['{ACCT}']"


def test_values_travel_as_parameters_never_in_the_text() -> None:
    r = uuid4()
    sql, params = build_query("telemetry", TenantScope([A], Role.ENGINEER, "p"),
                              _q(resource_ids=(r,)))
    assert str(A) not in sql
    assert str(r) not in sql
    assert "cpu.utilisation" not in sql
    assert params["metric"] == "cpu.utilisation"


@pytest.mark.parametrize("metric", ["cpu' OR 1=1 --", "CPU", "a;b", "", "x" * 65, "cpu\n"])
def test_a_metric_name_cannot_carry_anything_else(metric: str) -> None:
    with pytest.raises(TelemetryError):
        _q(metric=metric)


def test_no_method_accepts_sql() -> None:
    public = {n for n in dir(TelemetryStore) if not n.startswith("_")}
    assert public == {"create", "aclose", "ping", "query", "write", "purge"}


async def test_an_empty_scope_reads_nothing_and_sends_nothing() -> None:
    rec = _Recorder()
    store = _store(rec)
    assert await store.query(TenantScope([], Role.ENGINEER, "p"), _q()) == []
    assert await store.query(TenantScope([A], Role.CUSTOMER, "p", account_ids=[]), _q()) == []
    assert rec.requests == []


async def test_an_unresolved_scope_is_refused() -> None:
    with pytest.raises(TelemetryScopeError):
        await _store(_Recorder()).query(TenantScope.for_resolution("p"), _q())


async def test_reads_are_read_only_and_parameterised() -> None:
    row = {"tenant_id": str(A), "resource_id": str(uuid4()), "metric": "cpu.utilisation",
           "at": "2026-10-30 00:00:00.000", "value": 12.5, "unit": "percent"}
    rec = _Recorder(json.dumps(row) + "\n")
    points = await _store(rec).query(TenantScope([A], Role.ENGINEER, "p"), _q())
    assert [p.value for p in points] == [12.5]
    assert points[0].at == T0
    sent = rec.requests[0]
    assert sent.url.params["readonly"] == "2"
    assert sent.url.params["param_tenants"] == f"['{A}']"


async def test_a_row_outside_the_scope_is_never_handed_back() -> None:
    row = {"tenant_id": str(B), "resource_id": str(uuid4()), "metric": "cpu.utilisation",
           "at": "2026-10-30 00:00:00.000", "value": 1.0, "unit": "percent"}
    with pytest.raises(TelemetryScopeError):
        await _store(_Recorder(json.dumps(row))).query(TenantScope([A], Role.ENGINEER, "p"),
                                                       _q())


async def test_writes_need_the_ingest_role() -> None:
    rec = _Recorder()
    with pytest.raises(TelemetryScopeError):
        await _store(rec).write(TenantScope([A], Role.PLATFORM_ADMIN, "p"), [_sample()],
                                dedupe_token="t")
    assert rec.requests == []


async def test_a_write_naming_another_tenant_sends_nothing_at_all() -> None:
    rec = _Recorder()
    with pytest.raises(TelemetryScopeError):
        await _store(rec).write(TenantScope([A], Role.INGEST, "p"),
                                [_sample(), _sample(tenant=B)], dedupe_token="t")
    with pytest.raises(TelemetryScopeError):
        await _store(rec).write(TenantScope([A], Role.INGEST, "p", account_ids=[uuid4()]),
                                [_sample()], dedupe_token="t")
    assert rec.requests == []


async def test_a_write_carries_its_deduplication_token() -> None:
    rec = _Recorder()
    n = await _store(rec).write(TenantScope([A], Role.INGEST, "p"), [_sample(), _sample()],
                                dedupe_token="metrics:3:10-11")
    assert n == 2
    params = rec.requests[0].url.params
    assert params["insert_deduplication_token"] == "metrics:3:10-11"
    assert params["deduplicate_blocks_in_dependent_materialized_views"] == "1"
    assert len(rec.requests[0].content.splitlines()) == 2


@pytest.mark.parametrize(
    "bad",
    [{"ts": datetime(2026, 1, 1)}, {"value": float("nan")}, {"value": True},
     {"unit": "per'cent"}, {"metric": "Bad Metric"}, {"tenant_id": str(A)}],
)
def test_samples_are_validated(bad: dict[str, Any]) -> None:
    with pytest.raises(TelemetryError):
        _sample(**bad)


async def test_store_errors_do_not_echo_the_statement() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"Code: 60. tenant {A} secret detail",
                              headers={"X-ClickHouse-Exception-Code": "60"})

    store = TelemetryStore.create("http://s", "telemetry", "u", "p",
                                  transport=httpx.MockTransport(fail))
    with pytest.raises(TelemetryError) as err:
        await store.query(TenantScope([A], Role.ENGINEER, "p"), _q())
    assert str(A) not in str(err.value)


async def test_cross_tenant_refusals_are_security_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LLD, Observability: any cross-tenant query attempt pages. A role refusal
    (wrong role, unresolved scope) is not a cross-tenant attempt and does not."""
    row = {"tenant_id": str(B), "resource_id": str(uuid4()), "metric": "cpu.utilisation",
           "at": "2026-10-30 00:00:00.000", "value": 1.0, "unit": "percent"}
    with caplog.at_level("CRITICAL", logger="cloudops.security"):
        with pytest.raises(TelemetryScopeError):
            await _store(_Recorder(json.dumps(row))).query(
                TenantScope([A], Role.ENGINEER, "p"), _q())
        with pytest.raises(TelemetryScopeError):
            await _store(_Recorder()).write(TenantScope([A], Role.INGEST, "p"),
                                            [_sample(tenant=B)], dedupe_token="t")
        with pytest.raises(TelemetryScopeError):
            await _store(_Recorder()).query(TenantScope.for_resolution("p"), _q())
    events = [(r.security_event, r.source) for r in caplog.records]  # type: ignore[attr-defined]
    assert events == [("cross_tenant_attempt", "tenancy.telemetry_read"),
                      ("cross_tenant_attempt", "tenancy.telemetry_write")]
    assert str(B) not in caplog.text


async def test_a_purge_deletes_one_tenant_at_every_grain_and_nothing_else() -> None:
    """ADR 0038: a purged tenant's telemetry. One tenant, named by parameter, under the
    tenant predicate, in every table."""
    rec = _Recorder()
    assert await _store(rec).purge(TenantScope([A], Role.INGEST, "p")) == 3
    tables = []
    for request in rec.requests:
        sql = request.content.decode()
        assert "WHERE tenant_id IN {tenants:Array(UUID)}" in sql
        assert str(A) not in sql
        assert request.url.params["param_tenants"] == f"['{A}']"
        assert "readonly" not in request.url.params
        tables.append(sql.split()[2])
    assert tables == ["telemetry.metric_sample", "telemetry.metric_5m", "telemetry.metric_1h"]


@pytest.mark.parametrize("scope", [
    TenantScope([A], Role.PLATFORM_ADMIN, "p"),  # not the ingest role
    TenantScope([A, B], Role.INGEST, "p"),  # more than one tenant
    TenantScope([], Role.INGEST, "p"),  # none
    TenantScope([A], Role.INGEST, "p", account_ids=[ACCT]),  # part of a tenant
])
async def test_a_purge_names_exactly_one_whole_tenant(scope: TenantScope) -> None:
    rec = _Recorder()
    with pytest.raises(TelemetryScopeError):
        await _store(rec).purge(scope)
    assert rec.requests == []
