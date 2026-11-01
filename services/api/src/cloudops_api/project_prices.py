"""Project prices from ServiceNow project tasks (ADR 0038).

    GET /v1/commercial/project-prices?from=2026-10-01&to=2026-10-31[&currency=EUR]

Two figures per customer, for the dashboards and the revenue table:

- **delivered**: closed project tasks whose ``closed_at`` falls in the range;
- **proposed**: tasks still open whose ``opened_at`` falls in the range.

The amount is *project value (planned cost from ServiceNow PPM)*, not a customer
price: PPM holds no price. It is the task's ``cost`` ("Total planned cost") unless
``CLOUDOPS_SERVICENOW_PROJECT_PRICE_FIELD`` names another field (``work_cost`` for
actual cost). Display it under that label. The ``price`` names in this module and
its contract are kept for the agreed shape.

Staff only, holding ``commercial.read``. A price is commercial, so the database's
restrictive ``commercial_visibility`` policy (migration 0036) hides every row from
any other role whatever this module does, and a customer scope has no grant on
the table at all.

Currency (HLD, Multi-currency): amounts are summed per transaction currency,
unrounded. Given ``currency``, each bucket is also converted at the serving layer,
each task at the average rate in force on its own date (closed or opened). A
bucket with any priced task lacking a rate has no conversion, never a partial sum.
A task without a price is counted, never summed as zero.

``source`` says whether the figures can be read at all: ``source_absent`` while
the instance has no project tables (Project Portfolio Management inactive), so
the console shows "no project data" rather than zeros; ``never_synced`` before
the first run. ``as_of`` is when a sync last succeeded, and each figure's own
``as_of`` is the same time (the request's time before any success).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ConfigDict, Field
from sqlalchemy import text

from cloudops_api.access import Access
from cloudops_api.bff import TenantView
from cloudops_api.pipeline import (
    ApiError,
    AuditedRoute,
    ScopeDep,
    current_access,
    platform,
    requires,
)
from cloudops_api.provenance import ApiModel, Metric, query_ref

router = APIRouter(prefix="/v1", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]
Currency = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
SOURCE: Final = "servicenow"
TASKS: Final = "project tasks"
MAX_RANGE: Final = timedelta(days=731)

Bucket = Literal["delivered", "proposed"]
SourceState = Literal["available", "source_absent", "never_synced"]

_TOTALS: Final = text(
    "WITH t AS ("
    "  SELECT tenant_id,"
    "         CASE WHEN status = 'closed' THEN 'delivered' ELSE 'proposed' END AS bucket,"
    "         price, price_currency,"
    "         (CASE WHEN status = 'closed' THEN closed_at ELSE opened_at END AT TIME ZONE 'UTC')"
    "           ::date AS on_day"
    "  FROM cloudops.project_task"
    "  WHERE (status = 'closed' AND closed_at >= :start AND closed_at < :end)"
    "     OR (status = 'open' AND opened_at >= :start AND opened_at < :end))"
    " SELECT t.tenant_id, t.bucket, t.price_currency, count(*), sum(t.price),"
    "   sum(t.price * r.rate), bool_and(t.price IS NULL OR r.rate IS NOT NULL)"
    " FROM t LEFT JOIN LATERAL (SELECT CASE WHEN CAST(:rep AS text) IS NULL THEN NULL"
    "   WHEN t.price_currency = :rep THEN 1::numeric ELSE ("
    "     SELECT f.rate FROM cloudops.fx_rate f WHERE f.from_currency = t.price_currency"
    "       AND f.to_currency = :rep AND f.rate_type = 'average'"
    "       AND f.effective_date <= t.on_day"
    "     ORDER BY f.effective_date DESC LIMIT 1) END AS rate) r ON true"
    " GROUP BY t.tenant_id, t.bucket, t.price_currency"
    " ORDER BY t.tenant_id, t.bucket, t.price_currency NULLS LAST"
)


class Amount(ApiModel):
    currency: str
    amount: Metric[Decimal]


class PriceBucket(ApiModel):
    tasks: Metric[int]
    unpriced_tasks: Metric[int]  # counted, never summed as zero
    amounts: list[Amount]  # one per transaction currency
    # In the reporting currency, each task at the rate on its own date. Absent
    # without ``currency``, or when any priced task has no rate in force.
    converted: Metric[Decimal] | None


class CustomerProjectPrices(ApiModel):
    tenant: TenantView
    delivered: PriceBucket  # closed tasks, closed in the range
    proposed: PriceBucket  # open tasks, opened in the range


class ProjectPrices(ApiModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    from_: date = Field(alias="from")
    to: date
    reporting_currency: str | None
    source: SourceState
    # When a sync last succeeded: how fresh the figures are. None: never.
    as_of: datetime | None
    customers: list[CustomerProjectPrices]


@dataclass
class _Sums:
    tasks: int = 0
    unpriced: int = 0
    amounts: dict[str, Decimal] = field(default_factory=dict)
    converted: Decimal = Decimal(0)
    complete: bool = True


def _bucket(s: _Sums, rep: str | None, as_of: datetime, ref: str) -> PriceBucket:
    def count(v: int) -> Metric[int]:
        return Metric[int](value=v, unit=TASKS, source_system=SOURCE, as_of=as_of,
                           query_ref=ref, is_modelled=False)

    def money(v: Decimal, unit: str) -> Metric[Decimal]:
        return Metric[Decimal](value=v, unit=unit, source_system=SOURCE, as_of=as_of,
                               query_ref=ref, is_modelled=False)

    return PriceBucket(
        tasks=count(s.tasks), unpriced_tasks=count(s.unpriced),
        amounts=[Amount(currency=c, amount=money(v, c)) for c, v in sorted(s.amounts.items())],
        converted=money(s.converted, rep) if rep is not None and s.complete else None)


def _add(s: _Sums, row: tuple[Any, ...]) -> None:
    _t, _b, ccy, n, total, converted, complete = row
    s.tasks += int(n)
    if ccy is None:
        s.unpriced += int(n)
        return
    s.amounts[str(ccy)] = Decimal(total)
    if converted is None or not complete:
        s.complete = False
    else:
        s.converted += Decimal(converted)


@router.get("/commercial/project-prices", response_model=ProjectPrices)
@requires("commercial.read")
async def project_prices(
    request: Request,
    access: AccessDep,
    scope: ScopeDep,
    from_: Annotated[date, Query(alias="from", description="First day, inclusive (UTC).")],
    to: Annotated[date, Query(description="Last day, inclusive (UTC).")],
    currency: Currency = None,
) -> ProjectPrices:
    """Project value of delivered work (closed tasks) and proposed initiatives (open
    tasks) for every customer in scope, over a date range.

    Project value is the planned cost from ServiceNow PPM (the task's "Total
    planned cost"), not a customer price: PPM holds no price. Label it "project
    value (planned cost from ServiceNow PPM)"."""
    if access.kind != "staff":
        # The database refuses a customer scope anyway (no grant, migration 0036).
        raise ApiError(403, "forbidden")
    if to < from_ or to - from_ > MAX_RANGE:
        raise ApiError(400, "invalid_range")
    start = datetime.combine(from_, time.min, tzinfo=UTC)
    end = datetime.combine(to + timedelta(days=1), time.min, tzinfo=UTC)
    ref = query_ref("commercial.project_prices", 1, start=from_, end=to, rep=currency)
    async with platform(request).db.transaction(scope) as conn:
        names = {UUID(str(r[0])): str(r[1]) for r in (tuple(x) for x in await conn.execute(
            text("SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")))}
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(
            _TOTALS, {"start": start, "end": end, "rep": currency})]
        state: tuple[Any, ...] = tuple((await conn.execute(text(
            "SELECT state, last_success_at FROM cloudops.project_task_source()"))).one())
        source: SourceState = state[0]
        synced: datetime | None = state[1]
        now: datetime = await conn.scalar(text("SELECT now()"))
    as_of = synced or now
    sums: dict[tuple[UUID, str], _Sums] = defaultdict(_Sums)
    for row in rows:
        _add(sums[(UUID(str(row[0])), str(row[1]))], row)
    return ProjectPrices.model_validate({
        "from": from_, "to": to, "reporting_currency": currency, "source": source,
        "as_of": synced,
        "customers": [CustomerProjectPrices(
            tenant=TenantView(id=tid, name=name),
            delivered=_bucket(sums[(tid, "delivered")], currency, as_of, ref),
            proposed=_bucket(sums[(tid, "proposed")], currency, as_of, ref),
        ) for tid, name in names.items()]})
