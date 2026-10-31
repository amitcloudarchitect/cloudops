"""Portfolio consolidation in the reporting currency (HLD, Multi-currency).

A customer's pack is in its contract currency. The portfolio view is in a
reporting currency the viewer chooses, built **from the same underlying rows**:
each cost row is converted once, at the average rate in force on its own
usage date, straight into the reporting currency. It is never a sum of the
packs' contract-currency totals converted again, which would apply a second
rate to an already converted figure.

Volumes (incidents, open vulnerabilities) need no conversion and are summed.
A customer with a missing rate shows that gap instead of a number, and the
portfolio total is then marked incomplete rather than quietly smaller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import text

from cloudops_domain.reporting.money import check_currency
from cloudops_domain.reporting.sections import HIGH_SEVERITY, month_bounds
from cloudops_tenancy import ScopedConnection

_TENANTS: Final = text("SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")
_MISSING: Final = text("""
SELECT c.tenant_id, c.currency, min(c.usage_date)
FROM (SELECT DISTINCT tenant_id, currency, usage_date FROM cloudops.cost_fact
      WHERE usage_date >= :s AND usage_date < :e AND currency <> :target) c
WHERE NOT EXISTS (
    SELECT 1 FROM cloudops.fx_rate f
    WHERE f.from_currency = c.currency AND f.to_currency = :target
      AND f.rate_type = 'average' AND f.effective_date <= c.usage_date)
GROUP BY c.tenant_id, c.currency
""")
_SPEND: Final = text("""
SELECT tenant_id,
       sum(cloudops.to_currency(unblended_cost, currency, :target, usage_date, 'average'))
FROM cloudops.cost_fact
WHERE usage_date >= :s AND usage_date < :e
  AND NOT (tenant_id = ANY(CAST(:skip AS uuid[])))
GROUP BY tenant_id
""")
_VOLUMES: Final = text("""
SELECT t.id,
       (SELECT count(*) FROM cloudops.work_item w WHERE w.tenant_id = t.id
         AND w.kind = 'incident' AND w.opened_at >= :ts AND w.opened_at < :te),
       (SELECT count(*) FROM cloudops.vulnerability v WHERE v.tenant_id = t.id
         AND v.severity >= :high AND v.first_detected < :te
         AND (v.remediated_at IS NULL OR v.remediated_at >= :te))
FROM cloudops.tenant t
""")


@dataclass(frozen=True)
class CustomerLine:
    tenant_id: UUID
    tenant_name: str
    consumption: Decimal | None  # in the reporting currency, unrounded
    gap: str | None
    incidents_opened: int
    open_high_vulnerabilities: int


@dataclass(frozen=True)
class Portfolio:
    period: date
    reporting_currency: str
    customers: list[CustomerLine]
    total_consumption: Decimal
    complete: bool  # False when any customer's consumption could not be converted
    incidents_opened: int
    open_high_vulnerabilities: int


async def consolidate(conn: ScopedConnection, period: date, reporting_currency: str
                      ) -> Portfolio:
    start, end = month_bounds(period)
    target = check_currency(reporting_currency)
    p = {"s": start.date(), "e": end.date(), "target": target}
    gaps: dict[UUID, str] = {}
    for tenant, ccy, first in [tuple(r) for r in await conn.execute(_MISSING, p)]:
        gaps[tenant] = (gaps.get(tenant, "") + f"no average rate {ccy}->{target} from "
                        f"{first.isoformat()}; ").strip()
    spend = {r[0]: Decimal(r[1]) for r in (tuple(x) for x in await conn.execute(
        _SPEND, {**p, "skip": [str(t) for t in gaps]}))}
    volumes = {r[0]: (int(r[1]), int(r[2])) for r in (tuple(x) for x in await conn.execute(
        _VOLUMES, {"ts": start, "te": end, "high": HIGH_SEVERITY}))}
    lines = []
    for tenant, name in [tuple(r) for r in await conn.execute(_TENANTS)]:
        incidents, high = volumes.get(tenant, (0, 0))
        lines.append(CustomerLine(
            tenant, str(name), None if tenant in gaps else spend.get(tenant, Decimal(0)),
            gaps.get(tenant), incidents, high))
    return Portfolio(
        period, target, lines,
        sum((c.consumption for c in lines if c.consumption is not None), Decimal(0)),
        not gaps, sum(c.incidents_opened for c in lines),
        sum(c.open_high_vulnerabilities for c in lines),
    )
