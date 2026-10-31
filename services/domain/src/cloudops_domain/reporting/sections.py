"""The monthly pack's sections, generated from live data (HLD M19).

    content = await generate(conn, tenant_id=..., period=date(2026, 9, 1),
                             contract_currency="EUR", availability=..., financial=...)

Sections, in pack order:

| Section | Source | Built here |
| --- | --- | --- |
| Executive summary | the other sections' key figures, and billed cloud consumption | yes |
| Volumetrics | ServiceNow work items | yes |
| Financial dashboard | the commercial engine's margin snapshot (step 16) | ``financial.py`` |
| Vulnerabilities | Qualys detections | yes |
| CSAT | no survey source is connected yet | a visible gap |
| Availability | the telemetry store's ``availability`` metric | yes, when connected |
| Automation progress | triage runs and their reviews | yes |
| Revenue mix | the SOW in force (step 16's register) | ``financial.py`` |

A section with no source says so (``captured=False`` and why), never zero
(HLD design principle 7).

The two ``COMMERCIAL_SECTIONS`` carry margin, cost base and SOW revenue. They
are stored apart from the rest of the pack, where only a commercial role reads
them (HLD principle 6), and nothing from them reaches another section.

Everything runs under the caller's scope, narrowed to one tenant, with the
tenant predicate inside each statement too. Counts are integers; money is
``Decimal`` in its currency, converted per row at the average rate in force
on the row's usage date (``cloudops.to_currency``), and never rounded here.
A missing rate is a gap that names the pair and date, never a zero.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from sqlalchemy import text

from cloudops_domain.reporting.money import check_currency
from cloudops_tenancy import ScopedConnection, TenantScope

Kind = Literal["count", "percent", "days", "hours", "money", "text"]

SECTION_ORDER: Final = (
    "executive_summary", "volumetrics", "financial_dashboard", "vulnerabilities", "csat",
    "availability", "automation_progress", "revenue_mix",
)
SECTION_TITLES: Final = {
    "executive_summary": "Executive summary",
    "volumetrics": "Volumetrics",
    "financial_dashboard": "Financial dashboard",
    "vulnerabilities": "Vulnerabilities",
    "csat": "Customer satisfaction",
    "availability": "Availability",
    "automation_progress": "Automation progress",
    "revenue_mix": "Revenue mix",
}
# Margin, cost base and SOW revenue: stored apart, read by commercial roles only.
COMMERCIAL_SECTIONS: Final = ("financial_dashboard", "revenue_mix")
# The summary sections a model drafts narrative for, when they are captured.
NARRATIVE_SECTIONS: Final = ("executive_summary", "volumetrics", "vulnerabilities",
                             "availability", "automation_progress")
HIGH_SEVERITY: Final = 4  # Qualys 4 and 5
AGEING_BUCKETS: Final = ((0, 30), (31, 90), (91, None))
HUNDRED: Final = Decimal(100)


@dataclass(frozen=True)
class Figure:
    key: str
    label: str
    value: str  # an int or an unrounded Decimal, as text; rounding is presentation's job
    kind: Kind
    unit: str  # "incidents", "percent", "days", "hours", or a money figure's ISO currency
    source_system: str
    query_ref: str
    note: str | None = None


@dataclass(frozen=True)
class Table:
    key: str
    title: str
    columns: tuple[str, ...]
    kinds: tuple[Kind, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    captured: bool
    gap: str | None = None  # why it is not captured
    figures: tuple[Figure, ...] = ()
    tables: tuple[Table, ...] = ()

    def figure(self, key: str) -> Figure | None:
        return next((f for f in self.figures if f.key == key), None)


@dataclass(frozen=True)
class PackContent:
    tenant_id: UUID
    tenant_name: str
    period: date
    contract_currency: str
    data_as_of: datetime
    sections: tuple[Section, ...] = field(default_factory=tuple)

    def section(self, key: str) -> Section:
        return next(s for s in self.sections if s.key == key)

    def to_json(self) -> dict[str, Any]:
        return {"tenant_name": self.tenant_name, "period": self.period.isoformat(),
                "contract_currency": self.contract_currency,
                "data_as_of": self.data_as_of.isoformat(),
                "sections": {s.key: asdict(s) for s in self.sections}}

    @classmethod
    def from_json(cls, tenant_id: UUID, data: Mapping[str, Any]) -> PackContent:
        sections = []
        for key in SECTION_ORDER:
            s = data["sections"].get(key)
            if s is None:
                continue
            sections.append(Section(
                key=s["key"], title=s["title"], captured=s["captured"], gap=s["gap"],
                figures=tuple(Figure(**f) for f in s["figures"]),
                tables=tuple(Table(t["key"], t["title"], tuple(t["columns"]), tuple(t["kinds"]),
                                   tuple(tuple(r) for r in t["rows"])) for t in s["tables"]),
            ))
        return cls(tenant_id, data["tenant_name"], date.fromisoformat(data["period"]),
                   data["contract_currency"], datetime.fromisoformat(data["data_as_of"]),
                   tuple(sections))


# --- interfaces for what is built elsewhere -----------------------------------------


class AvailabilityGapError(Exception):
    """The source cannot give a correct figure for the month: the section says
    why (its message) instead of failing the whole pack."""


class AvailabilitySource(Protocol):
    """Mean availability (percent, 0 to 100) per resource over a window."""

    async def availability(self, scope: TenantScope, start: datetime, end: datetime
                           ) -> Mapping[UUID, float]: ...


class FinancialSections(Protocol):
    """The financial dashboard and revenue mix, from the commercial engine (step 16):
    ``financial.CommercialSections``. Both sections for a tenant and month, in the
    contract currency."""

    async def financial_dashboard(self, conn: ScopedConnection, tenant_id: UUID, period: date,
                                  contract_currency: str) -> Section: ...

    async def revenue_mix(self, conn: ScopedConnection, tenant_id: UUID, period: date,
                          contract_currency: str) -> Section: ...


def _gap(key: str, why: str) -> Section:
    return Section(key, SECTION_TITLES[key], False, why)


# --- helpers ----------------------------------------------------------------------


def month_bounds(period: date) -> tuple[datetime, datetime]:
    if period.day != 1:
        raise ValueError("a period is the first day of a month")
    start = datetime.combine(period, time(0), UTC)
    nxt = date(period.year + period.month // 12, period.month % 12 + 1, 1)
    return start, datetime.combine(nxt, time(0), UTC)


def ref(name: str, **params: Any) -> str:
    """A stable reference to the query and parameters behind a figure, with no
    tenant id or name in it (as ``cloudops_api.provenance.query_ref``)."""
    canonical = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return f"report.{name}.v1:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _share(part: int, whole: int) -> str | None:
    return None if whole == 0 else str(Decimal(part) * HUNDRED / Decimal(whole))


async def _rows(conn: ScopedConnection, sql: str, params: Mapping[str, Any]
                ) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in await conn.execute(text(sql), dict(params))]


# --- sections ---------------------------------------------------------------------

_KINDS: Final = ("incident", "service_request", "change_task", "project_task")
# Open high-severity detections at month end, by age. Built once from the
# bucket constants, never from input.
_AGEING: Final = text(
    "SELECT " + ", ".join(  # noqa: S608
        f"count(*) FILTER (WHERE :e - first_detected >= interval '{lo} days'"
        + (f" AND :e - first_detected < interval '{hi + 1} days')" if hi is not None else ")")
        for lo, hi in AGEING_BUCKETS)
    + " FROM cloudops.vulnerability WHERE tenant_id = :t AND severity >= :high"
    " AND first_detected < :e AND (remediated_at IS NULL OR remediated_at >= :e)")


async def volumetrics(conn: ScopedConnection, tenant: UUID, period: date) -> Section:
    start, end = month_bounds(period)
    p = {"t": tenant, "s": start, "e": end}
    opened = dict(await _rows(conn, (
        "SELECT kind, count(*) FROM cloudops.work_item WHERE tenant_id = :t"
        " AND opened_at >= :s AND opened_at < :e GROUP BY kind"), p))
    closed = dict(await _rows(conn, (
        "SELECT kind, count(*) FROM cloudops.work_item WHERE tenant_id = :t"
        " AND closed_at >= :s AND closed_at < :e GROUP BY kind"), p))
    backlog = dict(await _rows(conn, (
        "SELECT kind, count(*) FROM cloudops.work_item WHERE tenant_id = :t"
        " AND opened_at < :e AND (closed_at IS NULL OR closed_at >= :e) GROUP BY kind"), p))
    priority = await _rows(conn, (
        "SELECT coalesce(priority, 'unset'), count(*) FROM cloudops.work_item"
        " WHERE tenant_id = :t AND kind = 'incident' AND opened_at >= :s AND opened_at < :e"
        " GROUP BY 1 ORDER BY 1"), p)
    sla = tuple((await conn.execute(text(
        "SELECT count(*) FILTER (WHERE made_sla), count(*) FILTER (WHERE made_sla IS NOT NULL)"
        " FROM cloudops.work_item WHERE tenant_id = :t AND kind = 'incident'"
        " AND closed_at >= :s AND closed_at < :e"), p)).one())
    q = ref("volumetrics", period=period)
    figures = [
        Figure("incidents_opened", "Incidents opened", str(int(opened.get("incident", 0))),
               "count", "incidents", "servicenow", q),
        Figure("incidents_closed", "Incidents closed", str(int(closed.get("incident", 0))),
               "count", "incidents", "servicenow", q),
        Figure("requests_opened", "Service requests opened",
               str(int(opened.get("service_request", 0))), "count", "requests", "servicenow", q),
        Figure("changes_closed", "Change tasks closed", str(int(closed.get("change_task", 0))),
               "count", "change tasks", "servicenow", q),
    ]
    attainment = _share(int(sla[0]), int(sla[1]))
    if attainment is not None:
        figures.append(Figure("incident_sla_attainment", "Incidents resolved within SLA",
                              attainment, "percent", "percent", "servicenow", q))
    else:
        figures.append(Figure("incident_sla_attainment", "Incidents resolved within SLA", "",
                              "text", "", "servicenow", q, "no incident with an SLA closed"))
    by_kind = Table("by_kind", "Work by type", ("Type", "Opened", "Closed", "Open at month end"),
                    ("text", "count", "count", "count"),
                    tuple((k.replace("_", " "), str(int(opened.get(k, 0))),
                           str(int(closed.get(k, 0))), str(int(backlog.get(k, 0))))
                          for k in _KINDS))
    by_priority = Table("incidents_by_priority", "Incidents opened by priority",
                        ("Priority", "Incidents"), ("text", "count"),
                        tuple((str(r[0]), str(int(r[1]))) for r in priority))
    return Section("volumetrics", SECTION_TITLES["volumetrics"], True, None, tuple(figures),
                   (by_kind, by_priority))


async def vulnerabilities(conn: ScopedConnection, tenant: UUID, period: date) -> Section:
    start, end = month_bounds(period)
    p = {"t": tenant, "s": start, "e": end, "high": HIGH_SEVERITY}
    by_severity = await _rows(conn, (
        "SELECT severity,"
        " count(*) FILTER (WHERE first_detected < :e"
        "   AND (remediated_at IS NULL OR remediated_at >= :e)),"
        " count(*) FILTER (WHERE first_detected >= :s AND first_detected < :e),"
        " count(*) FILTER (WHERE remediated_at >= :s AND remediated_at < :e)"
        " FROM cloudops.vulnerability WHERE tenant_id = :t AND first_detected < :e"
        " GROUP BY severity ORDER BY severity DESC"), p)
    fix = (await conn.execute(text(
        "SELECT avg(fix_days), count(*) FROM cloudops.vulnerability WHERE tenant_id = :t"
        " AND remediated_at >= :s AND remediated_at < :e"), p)).one()
    ages = tuple((await conn.execute(_AGEING, p)).one())
    q = ref("vulnerabilities", period=period)
    open_high = sum(int(r[1]) for r in by_severity if int(r[0]) >= HIGH_SEVERITY)
    figures = [
        Figure("open_high", "Open severity 4 and 5 at month end", str(open_high), "count",
               "vulnerabilities", "qualys", q),
        Figure("new_total", "New detections", str(sum(int(r[2]) for r in by_severity)), "count",
               "vulnerabilities", "qualys", q),
        Figure("remediated_total", "Remediated", str(sum(int(r[3]) for r in by_severity)),
               "count", "vulnerabilities", "qualys", q),
    ]
    if fix[0] is not None:
        figures.append(Figure("mean_fix_days", "Mean days to remediate", str(Decimal(fix[0])),
                              "days", "days", "qualys", q))
    severity = Table("by_severity", "By severity",
                     ("Severity", "Open at month end", "New", "Remediated"),
                     ("text", "count", "count", "count"),
                     tuple((str(int(r[0])), str(int(r[1])), str(int(r[2])), str(int(r[3])))
                           for r in by_severity))
    labels = [f"{lo}-{hi} days" if hi is not None else f"over {lo - 1} days"
              for lo, hi in AGEING_BUCKETS]
    ageing = Table("high_ageing", "Open severity 4 and 5 by age", ("Age", "Open"),
                   ("text", "count"),
                   tuple((label, str(int(n))) for label, n in zip(labels, ages, strict=True)))
    return Section("vulnerabilities", SECTION_TITLES["vulnerabilities"], True, None,
                   tuple(figures), (severity, ageing))


async def availability(conn: ScopedConnection, scope: TenantScope, tenant: UUID, period: date,
                       source: AvailabilitySource | None) -> Section:
    if source is None:
        return _gap("availability", "the telemetry store is not connected to report generation")
    start, end = month_bounds(period)
    narrowed = TenantScope([tenant], scope.role, scope.principal_id,
                           account_ids=scope.account_ids)
    try:
        measured = await source.availability(narrowed, start, end)
    except AvailabilityGapError as exc:
        return _gap("availability", str(exc))
    if not measured:
        return _gap("availability", "no availability samples for this month")
    names = dict(await _rows(conn, (
        "SELECT id, name FROM cloudops.resource WHERE tenant_id = :t"
        " AND id = ANY(CAST(:ids AS uuid[]))"), {"t": tenant, "ids": [str(i) for i in measured]}))
    # Telemetry values are measurements (floats); four places is far below what
    # the samples can resolve, and keeps the figure a Decimal from here on.
    values = {rid: Decimal(str(round(v, 4))) for rid, v in measured.items()}
    mean = sum(values.values(), Decimal(0)) / Decimal(len(values))
    q = ref("availability", period=period)
    lowest = sorted(values.items(), key=lambda kv: (kv[1], str(names.get(kv[0], kv[0]))))[:10]
    table = Table("lowest", "Lowest availability", ("Resource", "Availability"),
                  ("text", "percent"),
                  tuple((str(names.get(rid, rid)), str(v)) for rid, v in lowest))
    return Section("availability", SECTION_TITLES["availability"], True, None, (
        Figure("mean_availability", "Mean availability", str(mean), "percent", "percent",
               "telemetry", q),
        Figure("resources_measured", "Resources measured", str(len(values)), "count",
               "resources", "telemetry", q),
        Figure("lowest_availability", "Lowest resource availability", str(lowest[0][1]),
               "percent", "percent", "telemetry", q),
    ), (table,))


async def automation_progress(conn: ScopedConnection, tenant: UUID, period: date) -> Section:
    start, end = month_bounds(period)
    p = {"t": tenant, "s": start, "e": end}
    routes = dict(await _rows(conn, (
        "SELECT route, count(*) FROM cloudops.triage_result WHERE tenant_id = :t"
        " AND created_at >= :s AND created_at < :e GROUP BY route"), p))
    reviews = dict(await _rows(conn, (
        "SELECT v.decision, count(*) FROM cloudops.triage_review v"
        " JOIN cloudops.triage_result r ON r.id = v.triage_result_id"
        " WHERE v.tenant_id = :t AND r.created_at >= :s AND r.created_at < :e"
        " GROUP BY v.decision"), p))
    total = sum(int(v) for v in routes.values())
    accepted, overridden = int(reviews.get("accept", 0)), int(reviews.get("override", 0))
    q = ref("automation_progress", period=period)
    figures = [
        Figure("tickets_triaged", "Tickets triaged by agents", str(total), "count", "tickets",
               "cloudops_platform", q),
        Figure("ready_for_planning", "Triaged ready for planning",
               str(int(routes.get("ready_for_planning", 0))), "count", "tickets",
               "cloudops_platform", q),
        Figure("triage_reviews", "Triage reviews by engineers", str(accepted + overridden),
               "count", "reviews", "cloudops_platform", q),
    ]
    rate = _share(accepted, accepted + overridden)
    if rate is not None:
        figures.append(Figure("triage_acceptance", "Triage accepted without override", rate,
                              "percent", "percent", "cloudops_platform", q))
    figures.append(Figure("automated_resolutions", "Automated resolutions", "", "text", "",
                          "cloudops_platform", q,
                          "the execution plane is not built: every fix is made by a person"))
    return Section("automation_progress", SECTION_TITLES["automation_progress"], True, None,
                   tuple(figures))


_MISSING_RATES: Final = text("""
SELECT c.currency, min(c.usage_date)
FROM (SELECT DISTINCT currency, usage_date FROM cloudops.cost_fact
      WHERE tenant_id = :t AND usage_date >= :s AND usage_date < :e
        AND currency <> :target) c
WHERE NOT EXISTS (
    SELECT 1 FROM cloudops.fx_rate f
    WHERE f.from_currency = c.currency AND f.to_currency = :target
      AND f.rate_type = 'average' AND f.effective_date <= c.usage_date)
GROUP BY c.currency ORDER BY c.currency
""")
_CONSUMPTION: Final = text("""
SELECT count(*), coalesce(sum(cloudops.to_currency(unblended_cost, currency, :target,
                                                   usage_date, 'average')), 0),
       min(usage_date), max(usage_date)
FROM cloudops.cost_fact
WHERE tenant_id = :t AND usage_date >= :s AND usage_date < :e
""")


async def consumption(conn: ScopedConnection, tenant: UUID, period: date, currency: str
                      ) -> Figure:
    """Billed cloud consumption in ``currency``: every cost row converted at the
    average rate in force on its own usage date, then summed. Unreconciled:
    reconciliation to the invoice is step 16."""
    start, end = month_bounds(period)
    p = {"t": tenant, "s": start.date(), "e": end.date(), "target": check_currency(currency)}
    q = ref("consumption", period=period, currency=currency)
    missing = [tuple(r) for r in await conn.execute(_MISSING_RATES, p)]
    if missing:
        pairs = ", ".join(f"{c}->{currency} from {d.isoformat()}" for c, d in missing)
        return Figure("cloud_consumption", "Billed cloud consumption", "", "text", "",
                      "cloud_billing", q, f"not shown: no average FX rate for {pairs}")
    rows, total, _first, _last = tuple((await conn.execute(_CONSUMPTION, p)).one())
    note = "unreconciled; each row at the average rate on its usage date"
    if not rows:
        note = "no billing rows for this month"
    return Figure("cloud_consumption", "Billed cloud consumption", str(Decimal(total)), "money",
                  currency, "cloud_billing", q, note)


def executive_summary(sections: Mapping[str, Section], spend: Figure) -> Section:
    picks = (("volumetrics", "incidents_opened"), ("volumetrics", "incident_sla_attainment"),
             ("vulnerabilities", "open_high"), ("availability", "mean_availability"),
             ("automation_progress", "triage_acceptance"))
    figures = [f for key, fig in picks if (s := sections.get(key)) is not None and s.captured
               and (f := s.figure(fig)) is not None]
    figures.append(spend)
    # The commercial sections' gaps stay with them: the summary is not commercial.
    gaps = [(k, s) for k, s in sections.items()
            if not s.captured and k not in COMMERCIAL_SECTIONS]
    table = Table("not_captured", "Not captured this month", ("Section", "Why"),
                  ("text", "text"), tuple((SECTION_TITLES[k], s.gap or "") for k, s in gaps))
    return Section("executive_summary", SECTION_TITLES["executive_summary"], True, None,
                   tuple(figures), (table,) if gaps else ())


async def generate(conn: ScopedConnection, scope: TenantScope, *, tenant_id: UUID,
                   tenant_name: str, period: date, contract_currency: str, now: datetime,
                   availability_source: AvailabilitySource | None,
                   financial: FinancialSections) -> PackContent:
    built = {
        "volumetrics": await volumetrics(conn, tenant_id, period),
        "financial_dashboard": await financial.financial_dashboard(
            conn, tenant_id, period, contract_currency),
        "vulnerabilities": await vulnerabilities(conn, tenant_id, period),
        "csat": _gap("csat", "no customer satisfaction survey source is connected"),
        "availability": await availability(conn, scope, tenant_id, period, availability_source),
        "automation_progress": await automation_progress(conn, tenant_id, period),
        "revenue_mix": await financial.revenue_mix(conn, tenant_id, period, contract_currency),
    }
    spend = await consumption(conn, tenant_id, period, contract_currency)
    summary = executive_summary(built, spend)
    ordered = tuple(summary if k == "executive_summary" else built[k] for k in SECTION_ORDER)
    return PackContent(tenant_id, tenant_name, period, contract_currency, now, ordered)


def narrative_input(section: Section) -> dict[str, Any]:
    """What a model is given to draft a section's narrative: figures and tables,
    numbers and labels only. No ticket text or document content reaches it."""
    return {
        "section": section.key, "title": section.title,
        "figures": [{"key": f.key, "label": f.label, "value": f.value, "unit": f.unit,
                     "note": f.note} for f in section.figures],
        "tables": [{"title": t.title, "columns": list(t.columns),
                    "rows": [list(r) for r in t.rows]} for t in section.tables],
    }


def narrative_sections(content: PackContent) -> Sequence[str]:
    return [k for k in NARRATIVE_SECTIONS if content.section(k).captured]
