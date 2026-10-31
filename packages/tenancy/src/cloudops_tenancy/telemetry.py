"""The single door to the telemetry column store.

    store = TelemetryStore.create(url, database, username, password)
    await store.write(scope, samples, dedupe_token="metrics:3:1200-1450")
    points = await store.query(scope, MetricQuery("cpu.utilisation", start, end))

The column store has no row-level security (LLD, Telemetry store): "tenant
isolation here is enforced by query construction ... the weakest isolation
boundary in the platform". So this wrapper *is* the isolation, and it is built
so that it cannot be bypassed through it:

1. **No SQL in, ever.** Callers describe what they want (``MetricQuery``: a
   metric name, a time range, resources, a grain, an aggregate); the statement
   is assembled here from constants. There is no method that accepts SQL,
   a table name or a predicate.
2. **The tenant predicate is always there.** Every statement begins its WHERE
   with ``tenant_id IN {tenants}`` bound from the ``TenantScope`` and, for a
   subscription-limited scope (ADR 0002, D16), ``account_id IN {accounts}``.
   ``_send`` refuses a statement without it, so a future edit that loses it
   fails loudly instead of leaking.
3. **Values travel as server-side parameters** (``{name:Type}``), never
   spliced into the text. Metric names, units and sources are also checked
   against a strict pattern.
4. **An empty scope reads nothing and sends nothing.** Unset never means all.
5. **Reads run read-only** (``readonly=2``): even a defective statement cannot
   write. **Writes need the ingest role**, and every row's tenant (and account,
   if restricted) must be in scope before anything is sent: all or nothing,
   the WITH CHECK of a policy that does not exist here.
6. **Raw clients fail CI** (``tools/ci/check_data_access.py``): drivers, and any
   string naming these tables or the store's HTTP dialect outside this package.

Writes carry a deduplication token: a batch retried after a crash (the same
Event Hubs partition and sequence range) is stored once, rollups included.

Grains (LLD retention): ``raw`` keeps 90 days, ``5m`` 13 months, ``1h`` three
years. Metric values are measurements, not money, so they are floats.
"""

from __future__ import annotations

import enum
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import httpx

from cloudops_tenancy.errors import TenancyError
from cloudops_tenancy.scope import Role, TenantScope

_NAME: Final = re.compile(r"^[a-z0-9_.]{1,64}$")
_LABEL: Final = re.compile(r"^[A-Za-z0-9_.:/ -]{1,64}$")
_DATABASE: Final = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
_PREDICATE: Final = "WHERE tenant_id IN {tenants:Array(UUID)}"
_TIME: Final = "DateTime64(3, 'UTC')"
MAX_LIMIT: Final = 100_000


class TelemetryError(TenancyError):
    """The telemetry store refused, or the wrapper refused to ask it."""


class TelemetryScopeError(TelemetryError):
    """A write named a tenant or account outside its scope, or the role may not
    do this. Nothing was sent."""


class Grain(enum.StrEnum):
    RAW = "raw"
    FIVE_MINUTES = "5m"
    HOUR = "1h"


class Aggregate(enum.StrEnum):
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    SUM = "sum"
    COUNT = "count"


def _aware(moment: datetime, name: str) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise TelemetryError(f"{name} must be a timezone-aware datetime")
    return moment.astimezone(UTC)


def _check_name(value: str, name: str, pattern: re.Pattern[str] = _NAME) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise TelemetryError(f"{name} is not a valid name")
    return value


@dataclass(frozen=True, slots=True)
class MetricSample:
    """One point of the canonical metric model."""

    tenant_id: UUID
    account_id: UUID
    resource_id: UUID
    metric: str
    ts: datetime
    value: float
    unit: str
    source: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "account_id", "resource_id"):
            if type(getattr(self, name)) is not UUID:
                raise TelemetryError(f"{name} must be a uuid.UUID")
        _check_name(self.metric, "metric")
        _check_name(self.unit, "unit", _LABEL)
        _check_name(self.source, "source", _LABEL)
        _aware(self.ts, "ts")
        if isinstance(self.value, bool) or not isinstance(self.value, int | float) or (
                not math.isfinite(self.value)):
            raise TelemetryError("value must be a finite number")


@dataclass(frozen=True, slots=True)
class MetricQuery:
    """What to read. Everything here is a value; nothing here is SQL."""

    metric: str
    start: datetime
    end: datetime
    resource_ids: tuple[UUID, ...] = ()
    grain: Grain = Grain.HOUR
    aggregate: Aggregate = Aggregate.AVG
    limit: int = 10_000

    def __post_init__(self) -> None:
        _check_name(self.metric, "metric")
        if _aware(self.end, "end") <= _aware(self.start, "start"):
            raise TelemetryError("end must be after start")
        if not all(type(r) is UUID for r in self.resource_ids):
            raise TelemetryError("resource_ids must be uuid.UUID values")
        if type(self.grain) is not Grain or type(self.aggregate) is not Aggregate:
            raise TelemetryError("grain and aggregate must be enum members")
        if not isinstance(self.limit, int) or not 1 <= self.limit <= MAX_LIMIT:
            raise TelemetryError(f"limit must be between 1 and {MAX_LIMIT}")


@dataclass(frozen=True, slots=True)
class MetricPoint:
    tenant_id: UUID
    resource_id: UUID
    metric: str
    at: datetime
    value: float
    unit: str


# Read statements, per grain. {value} is one of the constant expressions below.
_TABLE: Final = {Grain.RAW: "metric_sample", Grain.FIVE_MINUTES: "metric_5m",
                 Grain.HOUR: "metric_1h"}
_RAW_VALUE: Final = {
    Aggregate.AVG: "avg(value)", Aggregate.MIN: "min(value)", Aggregate.MAX: "max(value)",
    Aggregate.SUM: "sum(value)", Aggregate.COUNT: "toFloat64(count())",
}
_ROLLUP_VALUE: Final = {
    Aggregate.AVG: "sumMerge(sum_state) / countMerge(count_state)",
    Aggregate.MIN: "minMerge(min_state)", Aggregate.MAX: "maxMerge(max_state)",
    Aggregate.SUM: "sumMerge(sum_state)", Aggregate.COUNT: "toFloat64(countMerge(count_state))",
}
_COLUMNS: Final = ("tenant_id", "account_id", "resource_id", "metric", "ts", "value", "unit",
                   "source")


def build_query(database: str, scope: TenantScope, q: MetricQuery) -> tuple[str, dict[str, str]]:
    """The statement and its parameters. Public so tests can prove the
    predicate is always there; it sends nothing."""
    _check_name(database, "database", _DATABASE)
    time_col = "ts" if q.grain is Grain.RAW else "bucket"
    value = (_RAW_VALUE if q.grain is Grain.RAW else _ROLLUP_VALUE)[q.aggregate]
    unit = "any(unit)"
    where = [_PREDICATE]
    params = {
        "tenants": _array(scope.tenant_ids),
        "metric": q.metric,
        "start": _ts(q.start),
        "end": _ts(q.end),
        "limit": str(q.limit),
    }
    if scope.account_ids is not None:
        where.append("AND account_id IN {accounts:Array(UUID)}")
        params["accounts"] = _array(scope.account_ids)
    where.append("AND metric = {metric:String}")
    where.append(f"AND {time_col} >= {{start:{_TIME}}} AND {time_col} < {{end:{_TIME}}}")
    if q.resource_ids:
        where.append("AND resource_id IN {resources:Array(UUID)}")
        params["resources"] = _array(q.resource_ids)
    # Every interpolated piece is a module constant or a validated identifier;
    # values are parameters.
    sql = (
        f"SELECT tenant_id, resource_id, metric, {time_col} AS at, {value} AS value,"  # noqa: S608
        f" {unit} AS unit FROM {database}.{_TABLE[q.grain]} "
        + " ".join(where)
        + f" GROUP BY tenant_id, resource_id, metric, {time_col}"
        + " ORDER BY tenant_id, resource_id, at LIMIT {limit:UInt32}"
        + " FORMAT JSONEachRow"
    )
    return sql, params


def _array(ids: Sequence[UUID] | frozenset[UUID]) -> str:
    return "[" + ",".join(f"'{u}'" for u in sorted(ids, key=str)) + "]"


def _ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class TelemetryStore:
    """Owns the HTTP client to the store; nothing else in the platform does."""

    def __init__(self, client: httpx.AsyncClient, database: str) -> None:
        self._client = client
        self._database = _check_name(database, "database", _DATABASE)

    @classmethod
    def create(
        cls, url: str, database: str, username: str, password: str,
        *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> TelemetryStore:
        client = httpx.AsyncClient(
            base_url=url.rstrip("/"), transport=transport, timeout=30.0,
            headers={"X-ClickHouse-User": username, "X-ClickHouse-Key": password},
        )
        return cls(client, database)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self) -> None:
        response = await self._client.get("/ping")
        response.raise_for_status()

    async def query(self, scope: TenantScope, q: MetricQuery) -> list[MetricPoint]:
        if not isinstance(scope, TenantScope) or not isinstance(q, MetricQuery):
            raise TelemetryError("query needs a TenantScope and a MetricQuery")
        if scope.role is Role.UNRESOLVED:
            raise TelemetryScopeError("an unresolved scope reads nothing")
        if not scope.tenant_ids or scope.account_ids == frozenset():
            return []  # empty scope: zero rows, and no statement at all
        sql, params = build_query(self._database, scope, q)
        body = await self._send(sql, params, read_only=True)
        out = []
        for line in body.splitlines():
            if not line.strip():
                continue
            row: dict[str, Any] = json.loads(line)
            tenant = UUID(str(row["tenant_id"]))
            if tenant not in scope.tenant_ids:  # belt and braces: never hand one back
                raise TelemetryScopeError("the store returned a row outside the scope")
            out.append(MetricPoint(
                tenant_id=tenant, resource_id=UUID(str(row["resource_id"])),
                metric=str(row["metric"]),
                at=datetime.fromisoformat(str(row["at"])).replace(tzinfo=UTC),
                value=float(row["value"]), unit=str(row["unit"]),
            ))
        return out

    async def write(
        self, scope: TenantScope, samples: Sequence[MetricSample], *, dedupe_token: str,
    ) -> int:
        if not isinstance(scope, TenantScope) or scope.role is not Role.INGEST:
            raise TelemetryScopeError("only the ingest role writes telemetry")
        if not isinstance(dedupe_token, str) or not _LABEL.fullmatch(dedupe_token):
            raise TelemetryError("dedupe_token must be a short label")
        for s in samples:
            if not isinstance(s, MetricSample):
                raise TelemetryError("samples must be MetricSample values")
            if s.tenant_id not in scope.tenant_ids:
                raise TelemetryScopeError("a sample names a tenant outside the scope")
            if scope.account_ids is not None and s.account_id not in scope.account_ids:
                raise TelemetryScopeError("a sample names an account outside the scope")
        if not samples:
            return 0
        lines = "\n".join(json.dumps({
            "tenant_id": str(s.tenant_id), "account_id": str(s.account_id),
            "resource_id": str(s.resource_id), "metric": s.metric, "ts": _ts(s.ts),
            "value": float(s.value), "unit": s.unit, "source": s.source,
        }) for s in samples)
        sql = (f"INSERT INTO {self._database}.metric_sample ({', '.join(_COLUMNS)})"
               " FORMAT JSONEachRow")
        response = await self._client.post("/", params={
            "query": sql, "insert_deduplication_token": dedupe_token,
            "deduplicate_blocks_in_dependent_materialized_views": "1",
            "date_time_input_format": "best_effort",
        }, content=lines.encode())
        _raise(response)
        return len(samples)

    async def _send(self, sql: str, params: dict[str, str], *, read_only: bool) -> str:
        if _PREDICATE not in sql:
            raise TelemetryScopeError("refusing a statement without the tenant predicate")
        query = {f"param_{k}": v for k, v in params.items()}
        if read_only:
            query["readonly"] = "2"
        response = await self._client.post("/", params=query, content=sql.encode())
        _raise(response)
        return response.text


def _raise(response: httpx.Response) -> None:
    if response.is_success:
        return
    # The store's message can echo parameters; keep only the status and code.
    code = response.headers.get("X-ClickHouse-Exception-Code", "?")
    raise TelemetryError(f"telemetry store refused the request ({response.status_code}, {code})")
