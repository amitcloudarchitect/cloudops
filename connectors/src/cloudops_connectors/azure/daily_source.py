"""Daily cost from the Cost Management query API -> ``cloudops.cost_fact`` (entity
``cost_daily``, ADR 0023).

For a subscription that cannot have a Cost Management export: a pay-as-you-go or
Sponsorship offer ("WebDirect") refuses both FOCUS and actual-cost exports. The
query API answers for any subscription the identity can read cost on.

A run starts from ARM's subscription listing (as ``cost_total``) and asks, per
subscription and period, for ``ActualCost`` by day, ServiceName and ChargeType
(``client.daily_query``). The periods are the calendar month of the run's day, and
the month before while the day is within ``RESTATE_DAYS`` of the 1st, when late
charges still land. Each answer is a whole statement of that subscription's period,
so it is an *assembly* (``core/assembly.py``): its pages staged one by one, swapped
into ``cost_fact`` in one transaction when the last page is in, superseded by a
newer statement of the same subscription and period. The cursor is a queue (JSON):

    listing   ARM's subscriptions (nextLink followed), stored as ``.subs.json``;
              the run's day is fixed here and carried in bronze metadata
    daily     one page of one statement; a page with a nextLink queues the next,
              which the assembly then waits for

Rows, parsed with ``Decimal`` from the JSON numbers as sent:

    Cost          unblended_cost and amortized_cost (an ActualCost query does not
                  amortise; there is one figure)
    UsageDate     usage_date (yyyymmdd); a day outside the period is rejected
    ServiceName   service_native
    ChargeType    charge_class, by ``focus.charge_class`` with the category below
    Currency      currency

No resource: the query is grouped by service, so ``resource_id`` is empty and no
resource is linked or quarantined. The charge-class rule sees the service as the
meter category ("Virtual Machines Licenses" is licence) and no publisher, so
Marketplace charges are not told apart here: a known limit of this path.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from cloudops_connectors.azure import focus, mapping
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.cost_source import accounts
from cloudops_connectors.core import assembly, drift, identity, state
from cloudops_connectors.core.ingest import (
    Fetched,
    MissingRawError,
    PageContext,
    PageInfo,
    PageOutcome,
    principal,
)
from cloudops_connectors.core.raw import RawStore
from cloudops_tenancy import Database, Role, TenantScope

ENTITY: Final = mapping.COST_DAILY
SUBSCRIPTIONS: Final = "subscriptions"
DAILY: Final = "daily"
RESTATE_DAYS: Final = 5
COLUMNS: Final = frozenset({"Cost", "UsageDate", "ServiceName", "ChargeType", "Currency"})
_NAMESPACE: Final = uuid.UUID("5d0f6c8e-2b1a-4c1e-9a7e-3f1d2c0b9a51")

# The query API's charge types, in FOCUS's categories (what ``focus.charge_class``
# reads), with the commitment type for an unused one. Anything else is "other".
_CATEGORY: Final[Mapping[str, tuple[str, str, str]]] = {
    "usage": ("Usage", "", ""),
    "purchase": ("Purchase", "", ""),
    "refund": ("Adjustment", "", ""),
    "roundingadjustment": ("Adjustment", "", ""),
    "tax": ("Tax", "", ""),
    "credit": ("Credit", "", ""),
    "unusedreservation": ("Usage", "Reservation", "Unused"),
    "unusedsavingsplan": ("Usage", "Savings Plan", "Unused"),
}


class MalformedRecordError(ValueError):
    """A response or row without what keys it: kept in bronze, counted, skipped."""


@dataclass(frozen=True)
class DailyLine:
    usage_date: date
    service: str | None
    charge_type: str
    charge_class: str
    cost: Decimal
    currency: str


def periods(day: date) -> list[tuple[date, date]]:
    """(first, last) day of each month a run on ``day`` restates, oldest first."""
    first = day.replace(day=1)
    out = [(first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1))]
    if day.day <= RESTATE_DAYS:
        last = first - timedelta(days=1)
        out.insert(0, (last.replace(day=1), last))
    return out


def _json(body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MalformedRecordError("payload is not JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("properties"), Mapping):
        raise MalformedRecordError("a query response has properties")
    return doc


def _line(row: Any, at: Mapping[str, int], start: date, end: date) -> DailyLine:
    if not isinstance(row, list) or len(row) != len(at):
        raise MalformedRecordError("a row does not match its columns")
    cost = row[at["Cost"]]
    if isinstance(cost, bool) or not isinstance(cost, (Decimal, int)):
        raise MalformedRecordError("Cost is a number")
    try:
        amount = Decimal(cost)
    except InvalidOperation:
        raise MalformedRecordError("Cost is a number") from None
    if not amount.is_finite():
        raise MalformedRecordError("Cost is finite")
    try:
        usage = datetime.strptime(str(row[at["UsageDate"]]), "%Y%m%d").date()
    except ValueError:
        raise MalformedRecordError("UsageDate is yyyymmdd") from None
    if len(str(row[at["UsageDate"]])) != 8 or not start <= usage <= end:
        raise MalformedRecordError("UsageDate is outside the period")
    currency = row[at["Currency"]]
    if not isinstance(currency, str) or not focus.CURRENCY.fullmatch(currency.strip()):
        raise MalformedRecordError("Currency is three capitals")
    service = row[at["ServiceName"]]
    service = service.strip() if isinstance(service, str) and service.strip() else None
    charge_type = row[at["ChargeType"]]
    charge_type = charge_type.strip() if isinstance(charge_type, str) else ""
    category, discount, status = _CATEGORY.get(charge_type.lower(), (charge_type, "", ""))
    return DailyLine(
        usage, service, charge_type,
        focus.charge_class(category, billed=amount, discount_type=discount,
                           discount_status=status, meter_category=service or "",
                           service=service or ""),
        amount, currency.strip())


def lines(body: bytes, start: date, end: date) -> tuple[list[DailyLine], int, list[str],
                                                        str | None]:
    """A page's lines, the rows rejected, its column names, and its next link."""
    props = _json(body)["properties"]
    names = [str(c.get("name")) for c in props.get("columns") or [] if isinstance(c, Mapping)]
    if not set(names) >= COLUMNS:
        raise MalformedRecordError("the response lacks a column the query asked for")
    at = {n: i for i, n in enumerate(names)}
    rows = props.get("rows") or []
    if not isinstance(rows, list):
        raise MalformedRecordError("rows is a list")
    out: list[DailyLine] = []
    bad = 0
    for row in rows:
        try:
            out.append(_line(row, at, start, end))
        except MalformedRecordError:
            bad += 1
    link = props.get("nextLink")
    return out, bad, names, link if isinstance(link, str) and link.strip() else None


def _queue(cursor: str | None) -> list[dict[str, Any]]:
    if cursor is None:
        return [{"subs": None}]
    queue = json.loads(cursor)
    if not isinstance(queue, list) or not queue or not all(isinstance(q, dict) for q in queue):
        raise ValueError("bad daily cursor")
    return queue


def _cursor(queue: list[dict[str, Any]]) -> str | None:
    return json.dumps(queue, sort_keys=True) if queue else None


def _key(assembly_id: str, page: int) -> str:
    return f"{assembly_id}/page-{page:04d}"


def _who() -> str:
    return principal(mapping.SOURCE)


class AzureDaily:
    """The entity's fetch, inspect and process, for ``AzureSource``."""

    def __init__(self, client: AzureClient, today: Callable[[], date],
                 binding: identity.Binding | None = None) -> None:
        self._client = client
        self._today = today
        self._binding = binding

    def _statement(self, tenant: Any, sub: str) -> str:
        """What a newer statement supersedes. A customer credential's statements are
        its tenant's: the platform's own run of the same subscription (ADR 0023)
        restates the internal tenant's month, never the customer's (ADR 0027)."""
        return f"daily/{sub}" if self._binding is None else f"daily/{tenant}/{sub}"

    async def fetch(self, cursor: str | None) -> Fetched:
        head = _queue(cursor)[0]
        if "subs" in head:
            return Fetched(await self._client.subscriptions(head.get("next")), ".subs.json",
                           {"kind": SUBSCRIPTIONS, "as_of": self._today().isoformat()})
        start, end = date.fromisoformat(head["from"]), date.fromisoformat(head["to"])
        body = await self._client.cost_daily(str(head["sub"]), start, end, head.get("next"))
        meta = {"kind": DAILY, "subscription": str(head["sub"]), "period": f"{start:%Y-%m}",
                "from": head["from"], "to": head["to"], "assembly": str(head["assembly"]),
                "statement_page": str(head["page"])}
        if int(head["page"]) == 0:
            # The statement's time: a later statement of the same period supersedes it.
            meta["last_modified"] = datetime.now(UTC).isoformat()
        else:
            meta |= {"manifest_ref": str(head["manifest_ref"]),
                     "manifest_lm": str(head["manifest_lm"])}
        return Fetched(body, ".json", meta)

    async def inspect(self, raw: RawStore, ref: str, body: bytes,
                      cursor: str | None) -> PageInfo:
        queue = _queue(cursor)
        head, rest = queue[0], queue[1:]
        meta = await raw.meta(ref)
        if meta.get("kind") == SUBSCRIPTIONS:
            listed = mapping.subscriptions(body)
            subs = sorted({*head.get("found", []), *(s.subscription_id for s in listed)})
            link = mapping.next_link(body)
            if link is not None:
                return PageInfo(len(listed), _cursor([{"subs": True, "next": link,
                                                       "found": subs}, *rest]))
            day = date.fromisoformat(meta["as_of"])
            # The statement's id: fixed by the run's listing, so a replay of its pages
            # reuses it, and a later run's statement is a different one.
            work = [{"sub": s, "from": a.isoformat(), "to": b.isoformat(), "page": 0,
                     "assembly": str(uuid.uuid5(_NAMESPACE, f"{ref}|{s}|{a:%Y-%m}"))}
                    for s in identity.admit(self._binding, subs) for a, b in periods(day)]
            return PageInfo(len(listed), _cursor(work + rest))
        try:
            found, bad, _, link = lines(body, date.fromisoformat(head["from"]),
                                        date.fromisoformat(head["to"]))
        except MalformedRecordError:
            return PageInfo(0, _cursor(rest))  # process fails it, with its bronze ref
        if link is None:
            return PageInfo(len(found) + bad, _cursor(rest))
        first = ref if int(head["page"]) == 0 else str(head["manifest_ref"])
        first_lm = (meta["last_modified"] if int(head["page"]) == 0
                    else str(head["manifest_lm"]))
        follow = {**head, "page": int(head["page"]) + 1, "next": link,
                  "manifest_ref": first, "manifest_lm": first_lm}
        return PageInfo(len(found) + bad, _cursor([follow, *rest]))

    async def process(self, db: Database, raw: RawStore, ref: str,
                      ctx: PageContext) -> PageOutcome:
        meta = await raw.meta(ref)
        if meta.get("kind") == SUBSCRIPTIONS:
            listed = mapping.subscriptions(await raw.get(ref))
            return PageOutcome(state.RunCounts(pages=1, records=len(listed)))
        if meta.get("kind") != DAILY:
            raise ValueError("bronze page is not a daily cost response")
        sub, period, run_id = meta["subscription"], meta["period"], meta["assembly"]
        page = int(meta["statement_page"])
        start, end = date.fromisoformat(meta["from"]), date.fromisoformat(meta["to"])
        found, bad, names, link = lines(await raw.get(ref), start, end)
        counts = state.RunCounts(pages=1, records=len(found) + bad, rejected=bad)
        modified = assembly.last_modified(meta, "last_modified" if page == 0
                                          else "manifest_lm")
        first_ref = ref if page == 0 else meta["manifest_ref"]

        async with db.transaction(TenantScope((), Role.INGEST, _who())) as conn:
            tenant = (await identity.resolve_ids(conn, mapping.SOURCE, {sub},
                                                 self._binding)).get(sub)
            existing = await assembly.fetch(conn, run_id)
        if tenant is None:
            # Counted by the rows it carries; a page with none has nothing to count.
            if found:
                async with db.transaction(TenantScope((), Role.INGEST, _who())) as conn:
                    await state.quarantine_aliases(conn, mapping.SOURCE,
                                                   Counter({sub: len(found)}), ref)
            counts.quarantined = len(found)
            return PageOutcome(counts)
        if existing is not None and existing.swapped_at is not None and ctx.advance:
            return PageOutcome(state.RunCounts(pages=1))  # in cost_fact already

        async with db.transaction(TenantScope({tenant}, Role.INGEST, _who())) as conn:
            if page == 0:
                await assembly.register(conn, run_id, self._statement(tenant, sub), period,
                                        first_ref,
                                        [_key(run_id, 0)])
            elif existing is None:
                raise MissingRawError("a later page's statement is not registered: "
                                      "replay its first page")
            if link is not None:
                await assembly.expect(conn, run_id, _key(run_id, page + 1))
            locked = await assembly.fetch(conn, run_id, lock=True)
            if locked is None:  # pragma: no cover - registered above
                raise MissingRawError("the statement vanished while its page was read")
            account = (await accounts(conn, {sub: tenant}))[sub]
            await assembly.load_rows(conn, _loads(found, tenant, account, run_id,
                                                  _key(run_id, page), period))
            now = await assembly.mark_loaded(conn, locked, _key(run_id, page), {tenant},
                                             {account})
            await drift.observe(conn, mapping.SOURCE, ENTITY, set(names), mapped=COLUMNS,
                                expected=COLUMNS, records=counts.records, raw_ref=ref)
            if now.complete:
                superseded = await assembly.superseded(raw, conn, now, modified)
                await assembly.complete(conn, now, superseded, modified, ctx,
                                        source=mapping.SOURCE, entity=ENTITY)
        counts.written = len(found)
        return PageOutcome(counts)


def _loads(found: list[DailyLine], tenant: Any, account: Any, run_id: str, key: str,
           period: str) -> list[dict[str, Any]]:
    """One staged row per (day, service, class, currency), summed in full precision
    and rounded once."""
    sums: dict[tuple[date, str | None, str, str], Decimal] = defaultdict(Decimal)
    types: dict[tuple[date, str | None, str, str], Counter[str]] = defaultdict(Counter)
    for x in found:
        k = (x.usage_date, x.service, x.charge_class, x.currency)
        sums[k] += x.cost
        types[k][x.charge_type] += 1
    return [{
        "tenant_id": tenant, "account_id": account, "assembly_id": run_id, "object_key": key,
        "resource_id": None, "usage_date": d, "invoice_period": period,
        "service_native": service, "charge_class": cls,
        "unblended_cost": focus.stored(amount), "amortized_cost": focus.stored(amount),
        "currency": ccy,
        "raw_extra": json.dumps({"query": "ActualCost/Daily",
                                 "charge_types": dict(sorted(types[(d, service, cls, ccy)]
                                                             .items()))}, sort_keys=True),
    } for (d, service, cls, ccy), amount in sorted(sums.items(), key=lambda i: str(i[0]))]
