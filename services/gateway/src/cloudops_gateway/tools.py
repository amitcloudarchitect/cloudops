"""The first three tools, all read-only, to prove the pattern.

Each runs under the conversation's scope through packages/tenancy, so
row-level security decides what exists: a resource of another tenant is "not
found", exactly as in the API. Sections of a result that need a permission
the caller lacks (tickets inside a resource context, say) are left out rather
than shown. Money stays ``Decimal`` and travels as a string.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_gateway.registry import Source, Tool, ToolContext, ToolKind, ToolResult

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


async def get_cost_trend(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
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
)
