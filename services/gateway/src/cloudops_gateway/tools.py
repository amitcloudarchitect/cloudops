"""The platform's read-only tools.

Step 06 proved the pattern with three; the assistant (ADR 0029) adds a ticket
and its SLA, SLA attainment, open vulnerabilities, recent changes and margin.
A customer scope gets its price instead of cost (ADR 0021) from the same tool.

Each runs under the conversation's scope through packages/tenancy, so
row-level security decides what exists: a resource of another tenant is "not
found", exactly as in the API. Sections of a result that need a permission
the caller lacks (tickets inside a resource context, say) are left out rather
than shown. Money stays ``Decimal`` and travels as a string.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_gateway.registry import Source, Tool, ToolContext, ToolKind, ToolResult
from cloudops_tenancy import Role

KINDS: Final = ["incident", "change_task", "service_request", "project_task"]


def _now() -> datetime:
    return datetime.now(UTC)


async def get_resource_context(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    ref = str(args["resource"]).strip()
    try:
        by_id: UUID | None = UUID(ref)
    except ValueError:
        by_id = None
    async with ctx.db.transaction(ctx.scope) as conn:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(
            text(
                "SELECT id, name, type_canonical, region, state, operating_system, tags,"
                " is_critical_ci FROM cloudops.resource WHERE deleted_at IS NULL"
                " AND (id = :id OR name_norm = lower(split_part(:name, '.', 1)))"
                " ORDER BY name LIMIT 3"
            ),
            {"id": by_id, "name": ref},
        )]
        if len(rows) != 1:
            # None, or ambiguous: never guess which resource was meant.
            return ToolResult({"found": False, "matches": len(rows)})
        rid, name, rtype, region, state, os_name, tags, critical = rows[0]
        out: dict[str, Any] = {
            "found": True, "id": rid, "name": name, "type": rtype, "region": region,
            "state": state, "operating_system": os_name, "critical": critical,
            "tags": dict(tags or {}),
        }
        sources = [Source("platform_inventory", f"resource:{rid}", _now())]
        if "tickets.read" in ctx.permissions:
            out["recent_tickets"] = [
                {"number": r[0], "priority": r[1], "state": r[2], "summary": r[3],
                 "opened_at": r[4]}
                for r in (tuple(x) for x in await conn.execute(
                    text("SELECT number, priority, state, short_description, opened_at"
                         " FROM cloudops.work_item WHERE resource_id = :id"
                         " ORDER BY opened_at DESC LIMIT 10"), {"id": rid}))
            ]
            sources.append(Source("servicenow", f"work_item:resource:{rid}", _now()))
        if "vulnerabilities.read" in ctx.permissions:
            out["open_vulnerabilities"] = [
                {"qid": r[0], "title": r[1], "severity": r[2], "first_detected": r[3]}
                for r in (tuple(x) for x in await conn.execute(
                    text("SELECT qid, title, severity, first_detected FROM cloudops.vulnerability"
                         " WHERE resource_id = :id AND remediated_at IS NULL"
                         " ORDER BY severity DESC, first_detected LIMIT 10"), {"id": rid}))
            ]
            sources.append(Source("qualys", f"vulnerability:resource:{rid}", _now()))
    return ToolResult(out, tuple(sources))


async def search_tickets(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    days = int(args.get("days", 30))
    limit = int(args.get("limit", 10))
    since = _now() - timedelta(days=days)
    async with ctx.db.transaction(ctx.scope) as conn:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(
            text(
                "SELECT number, kind, priority, state, short_description, configuration_item,"
                " opened_at, closed_at,"
                " ts_rank_cd(to_tsvector('english', coalesce(short_description, '') || ' '"
                "            || coalesce(close_notes, '')), q) AS rank"
                " FROM cloudops.work_item, plainto_tsquery('english', :query) AS q"
                " WHERE opened_at >= :since"
                "   AND (CAST(:kind AS text) IS NULL OR kind = :kind)"
                "   AND to_tsvector('english', coalesce(short_description, '') || ' '"
                "       || coalesce(close_notes, '')) @@ q"
                " ORDER BY rank DESC, opened_at DESC LIMIT :limit"
            ),
            {"query": args["query"], "since": since, "kind": args.get("kind"), "limit": limit},
        )]
    tickets = [
        {"number": r[0], "kind": r[1], "priority": r[2], "state": r[3], "summary": r[4],
         "configuration_item": r[5], "opened_at": r[6], "closed_at": r[7]}
        for r in rows
    ]
    return ToolResult(
        {"tickets": tickets, "count": len(tickets), "days": days},
        (Source("servicenow", f"work_item:search:{len(tickets)}", _now()),),
    )


def _closed_months(today: date, months: int) -> tuple[date, date]:
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    return start, end


async def _customer_price(ctx: ToolContext, months: int) -> ToolResult:
    """What a customer sees instead of cost (ADR 0021): the price it is charged,
    by closed calendar month, provider and currency, and nothing finer."""
    start, end = _closed_months(_now().date(), months)
    async with ctx.db.transaction(ctx.scope) as conn:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
            "SELECT period, provider, currency, price FROM"
            " cloudops.customer_monthly_price(:start, :end, NULL)"
            " ORDER BY period, provider, currency"), {"start": start, "end": end})]
    return ToolResult(
        {"measure": "price_charged",
         # price None: a month with a line not yet priced; never a cost, never zero.
         "series": [{"month": r[0], "provider": r[1], "currency": r[2], "price": r[3]}
                    for r in rows],
         "months": months, "data_through": end},
        (Source("customer_price", f"customer_price:{start}:{end}",
                datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)),),
    )


async def get_cost_trend(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    if ctx.scope.role is Role.CUSTOMER:
        return await _customer_price(ctx, max(1, min(12, int(args.get("days", 90)) // 30)))
    days = int(args.get("days", 30))
    unit = "week" if args.get("granularity") == "week" else "day"
    start = (_now() - timedelta(days=days - 1)).date()
    async with ctx.db.transaction(ctx.scope) as conn:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(
            text(
                f"SELECT date_trunc('{unit}', usage_date)::date AS period, currency,"  # noqa: S608
                " sum(amortized_cost) AS amortized, max(usage_date) AS last_day"
                " FROM cloudops.cost_fact WHERE usage_date >= :start"
                " GROUP BY 1, 2 ORDER BY 1, 2"
            ),
            {"start": start},
        )]
    as_of = max((r[3] for r in rows), default=None)
    return ToolResult(
        {
            # Amortised, in transaction currency, unconverted and unrounded.
            "series": [{"period": r[0], "currency": r[1], "amortized": r[2]} for r in rows],
            "granularity": unit,
            "currencies": sorted({r[1] for r in rows}),
            "data_through": as_of,
        },
        (Source("cost_fact", f"cost_trend:{unit}:{days}",
                datetime.combine(as_of, datetime.min.time(), tzinfo=UTC) if as_of else _now()),),
    )


_TICKET_COLUMNS: Final = (
    # Only what a customer scope is granted (migration 0011), for everyone: the
    # assistant never needs assignees, workgroups or hours.
    "number, kind, priority, state, short_description, configuration_item, opened_at,"
    " closed_at, change_type, response_sla, resolve_sla, made_sla, sla_breached,"
    " resolve_time_seconds, closure_code, close_notes"
)


async def get_ticket(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    number = str(args["number"]).strip().upper()
    async with ctx.db.transaction(ctx.scope) as conn:
        found = (await conn.execute(text(
            f"SELECT {_TICKET_COLUMNS} FROM cloudops.work_item WHERE number = :n"  # noqa: S608
            " ORDER BY opened_at DESC LIMIT 1"), {"n": number})).one_or_none()
    if found is None:
        # Another tenant's ticket and one that never existed look the same.
        return ToolResult({"found": False})
    r: tuple[Any, ...] = tuple(found)
    keys = [k.strip() for k in _TICKET_COLUMNS.split(",")]
    out = {"found": True, **dict(zip(keys, r, strict=True))}
    out["summary"] = out.pop("short_description")
    return ToolResult(out, (Source("servicenow", f"work_item:{number}", _now()),))


async def get_sla_summary(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    days = int(args.get("days", 30))
    since = _now() - timedelta(days=days)
    async with ctx.db.transaction(ctx.scope) as conn:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
            "SELECT kind, priority, count(*) AS opened,"
            " count(*) FILTER (WHERE closed_at IS NOT NULL) AS closed,"
            " count(*) FILTER (WHERE made_sla) AS made_sla,"
            " count(*) FILTER (WHERE sla_breached) AS breached,"
            " count(*) FILTER (WHERE closed_at IS NULL AND sla_breached) AS open_breached"
            " FROM cloudops.work_item WHERE opened_at >= :since"
            " GROUP BY kind, priority ORDER BY kind, priority"), {"since": since})]
    groups = [{"kind": r[0], "priority": r[1], "opened": r[2], "closed": r[3],
               "made_sla": r[4], "breached": r[5], "open_breached": r[6]} for r in rows]
    measured = sum(g["made_sla"] + g["breached"] for g in groups)
    attainment = (None if measured == 0 else
                  str((Decimal(sum(g["made_sla"] for g in groups)) * 100 / measured)
                      .quantize(Decimal("0.1"))))
    return ToolResult(
        {"days": days, "groups": groups, "sla_attainment_percent": attainment,
         "note": "attainment = made SLA / (made + breached); null when nothing was measured"},
        (Source("servicenow", f"work_item:sla:{days}", _now()),),
    )


async def get_open_vulnerabilities(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    minimum = int(args.get("min_severity", 3))
    limit = int(args.get("limit", 10))
    async with ctx.db.transaction(ctx.scope) as conn:
        counts = {int(r[0]): int(r[1]) for r in (tuple(x) for x in await conn.execute(text(
            "SELECT severity, count(*) FROM cloudops.vulnerability"
            " WHERE remediated_at IS NULL GROUP BY 1")))}
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
            "SELECT qid, title, severity, asset_name, first_detected, last_detected"
            " FROM cloudops.vulnerability WHERE remediated_at IS NULL AND severity >= :min"
            " ORDER BY severity DESC, first_detected, qid LIMIT :limit"),
            {"min": minimum, "limit": limit})]
    now = _now()
    return ToolResult(
        {"open_by_severity": {str(k): counts.get(k, 0) for k in (5, 4, 3, 2, 1)},
         "open_total": sum(counts.values()),
         "oldest_first": [{"qid": r[0], "title": r[1], "severity": r[2], "asset": r[3],
                           "first_detected": r[4], "last_detected": r[5],
                           "age_days": max((now - r[4]).days, 0)} for r in rows],
         "severity_scale": "1 (minimal) to 5 (urgent)"},
        (Source("qualys", f"vulnerability:open:{minimum}", now),),
    )


async def get_recent_changes(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    days = int(args.get("days", 14))
    limit = int(args.get("limit", 15))
    since = _now() - timedelta(days=days)
    async with ctx.db.transaction(ctx.scope) as conn:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
            "SELECT number, change_request, change_type, state, short_description,"
            " configuration_item, opened_at, closed_at FROM cloudops.work_item"
            " WHERE kind = 'change_task' AND (opened_at >= :since OR closed_at >= :since)"
            " ORDER BY coalesce(closed_at, opened_at) DESC LIMIT :limit"),
            {"since": since, "limit": limit})]
    return ToolResult(
        {"days": days, "changes": [
            {"number": r[0], "change_request": r[1], "change_type": r[2], "state": r[3],
             "summary": r[4], "configuration_item": r[5], "opened_at": r[6], "closed_at": r[7]}
            for r in rows]},
        (Source("servicenow", f"work_item:changes:{days}", _now()),),
    )


async def get_margin(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    """The latest margin snapshot per month. Commercial roles only: the matrix
    gates the tool, and commercial_visibility RLS holds even if it did not."""
    months = int(args.get("months", 3))
    async with ctx.db.transaction(ctx.scope) as conn:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
            "SELECT DISTINCT ON (period) period, through, status, version, computed_at,"
            " not_captured, contract_currency, revenue, actual_cost, modelled_cost,"
            " actual_margin, modelled_margin, margin_pct, band_basis, band, target_margin"
            " FROM cloudops.margin_snapshot WHERE period >= :since"
            " ORDER BY period DESC, version DESC LIMIT :months"),
            {"since": (_now().date().replace(day=1) - timedelta(days=31 * months)).replace(day=1),
             "months": months})]
    snapshots = [
        {"period": r[0], "through": r[1], "status": r[2], "version": r[3],
         "not_captured": r[5], "currency": r[6], "revenue": r[7], "actual_cost": r[8],
         "modelled_cost": r[9], "actual_margin": r[10], "modelled_margin": r[11],
         "margin_fraction": r[12], "band_basis": r[13], "band": r[14],
         "target_margin_fraction": r[15],
         # The band rests on the modelled figure when actual hours are missing.
         "is_modelled": r[13] == "modelled"}
        for r in rows
    ]
    as_of = max((r[4] for r in rows), default=_now())
    return ToolResult({"snapshots": snapshots},
                      (Source("margin_snapshot", f"margin:{months}", as_of),))


TOOLS: Final = (
    Tool(
        name="get_resource_context",
        description="Details of one cloud resource in the current customer's estate, by id or "
                    "name, with its recent tickets and open vulnerabilities.",
        parameters={
            "type": "object", "additionalProperties": False, "required": ["resource"],
            "properties": {"resource": {"type": "string", "minLength": 1, "maxLength": 200}},
        },
        permission="inventory.read", kind=ToolKind.READ, handler=get_resource_context,
        timeout_seconds=10,
    ),
    Tool(
        name="search_tickets",
        description="Full-text search of the current customer's tickets.",
        parameters={
            "type": "object", "additionalProperties": False, "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 2, "maxLength": 200},
                "days": {"type": "integer", "minimum": 1, "maximum": 90},
                "kind": {"enum": KINDS},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
        },
        permission="tickets.read", kind=ToolKind.READ, handler=search_tickets,
        timeout_seconds=10,
    ),
    Tool(
        name="get_cost_trend",
        description="Amortised cloud cost per day or week for the current customer, in each "
                    "transaction currency (never converted).",
        parameters={
            "type": "object", "additionalProperties": False,
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 90},
                "granularity": {"enum": ["day", "week"]},
            },
        },
        permission="cost.read", kind=ToolKind.READ, handler=get_cost_trend,
        timeout_seconds=15,
    ),
    Tool(
        name="get_ticket",
        description="One ticket of the current customer by its number (e.g. INC0012345): "
                    "state, priority, SLA targets and whether the SLA was made or breached.",
        parameters={
            "type": "object", "additionalProperties": False, "required": ["number"],
            "properties": {"number": {"type": "string", "pattern": "^[A-Za-z]{2,8}[0-9]{3,12}$"}},
        },
        permission="tickets.read", kind=ToolKind.READ, handler=get_ticket, timeout_seconds=10,
    ),
    Tool(
        name="get_sla_summary",
        description="Tickets opened in the last N days by kind and priority, with how many "
                    "made or breached their SLA, and overall SLA attainment.",
        parameters={
            "type": "object", "additionalProperties": False,
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 180}},
        },
        permission="tickets.read", kind=ToolKind.READ, handler=get_sla_summary,
        timeout_seconds=10,
    ),
    Tool(
        name="get_open_vulnerabilities",
        description="Open vulnerability findings for the current customer: counts by severity "
                    "(1-5) and the oldest open findings at or above a severity.",
        parameters={
            "type": "object", "additionalProperties": False,
            "properties": {
                "min_severity": {"type": "integer", "minimum": 1, "maximum": 5},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
        },
        permission="vulnerabilities.read", kind=ToolKind.READ,
        handler=get_open_vulnerabilities, timeout_seconds=10,
    ),
    Tool(
        name="get_recent_changes",
        description="Change tasks opened or closed in the last N days for the current customer.",
        parameters={
            "type": "object", "additionalProperties": False,
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 90},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
        },
        permission="tickets.read", kind=ToolKind.READ, handler=get_recent_changes,
        timeout_seconds=10,
    ),
    Tool(
        name="get_margin",
        description="Getronics' margin on the current customer, per month: revenue, cost, "
                    "margin, band and target, and whether the figure is modelled.",
        parameters={
            "type": "object", "additionalProperties": False,
            "properties": {"months": {"type": "integer", "minimum": 1, "maximum": 12}},
        },
        permission="commercial.read", kind=ToolKind.READ, handler=get_margin,
        timeout_seconds=10,
    ),
)
