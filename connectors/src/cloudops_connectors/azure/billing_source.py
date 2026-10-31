"""Azure provider totals and invoices -> ``cloudops.billing_total`` (``core/billing.py``).

Two entities, both daily, one scope (the management endpoint), each run
starting from ARM's subscription listing (the delegated subscriptions):

- ``cost_total``: per subscription, the Cost Management query (``ActualCost``,
  ``MonthToDate``, the sum of ``Cost`` with ``Currency``) -> ``provider_total``
  for the month of the run's day, ``as_of`` the run's day: the month so far,
  as Azure bills it.
- ``invoice``: per subscription, the billing account's invoices for the
  invoice periods of the last ``INVOICE_MONTHS`` months -> ``invoice``:
  ``billedAmount`` (``amountDue`` when absent) with its currency,
  ``name`` -> document_ref, ``invoicePeriodStartDate`` -> period,
  ``invoiceDate`` -> as_of.

The cursor is a queue (JSON): the listing (``nextLink`` followed), then one
request per subscription. The run's day and invoice window are fixed when the
listing is fetched and carried in bronze metadata, so a replay of a page
writes exactly what the run did. Numbers are parsed straight to ``Decimal``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.core import billing
from cloudops_connectors.core.ingest import Fetched, PageContext, PageInfo, PageOutcome
from cloudops_connectors.core.raw import RawStore
from cloudops_tenancy import Database

SUBSCRIPTIONS: Final = "subscriptions"
COST_QUERY: Final = "cost_query"
INVOICES: Final = "invoices"
INVOICE_MONTHS: Final = 3
COST_COLUMNS: Final = ("totalCost", "Cost", "PreTaxCost")


class MalformedRecordError(ValueError):
    """A row or invoice without what keys it: kept in bronze, counted, skipped."""


def _months_back(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 - months
    return date(index // 12, index % 12 + 1, 1)


def _queue(cursor: str | None) -> list[dict[str, Any]]:
    if cursor is None:
        return [{"subs": None}]
    queue = json.loads(cursor)
    if not isinstance(queue, list) or not queue or not all(isinstance(q, dict) for q in queue):
        raise ValueError("bad billing cursor")
    return queue


def _cursor(queue: list[dict[str, Any]]) -> str | None:
    return json.dumps(queue, sort_keys=True) if queue else None


def _json(body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MalformedRecordError("payload is not JSON") from None
    if not isinstance(doc, dict):
        raise MalformedRecordError("payload is not a JSON object")
    return doc


def _amount(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise MalformedRecordError("an amount is a number")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise MalformedRecordError("an amount is a number") from None
    if not amount.is_finite():
        raise MalformedRecordError("an amount is finite")
    return amount


def _currency(value: Any) -> str:
    if not isinstance(value, str) or len(value.strip()) != 3 or not value.strip().isupper():
        raise MalformedRecordError("a currency is three capitals")
    return value.strip()


def _day(value: Any, name: str) -> date:
    if not isinstance(value, str):
        raise MalformedRecordError(f"{name} is required")
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise MalformedRecordError(f"{name} is not an ISO date") from None
    return (moment if moment.tzinfo is None else moment.astimezone(UTC)).date()


def cost_totals(body: bytes, subscription: str, as_of: date) -> tuple[list[billing.Total], int]:
    """A query response's rows: (cost, currency), one per billing currency."""
    props = _json(body).get("properties")
    if not isinstance(props, Mapping):
        raise MalformedRecordError("a query response has properties")
    names = [str(c.get("name")) for c in props.get("columns") or [] if isinstance(c, Mapping)]
    cost = next((names.index(n) for n in COST_COLUMNS if n in names), None)
    if cost is None or "Currency" not in names:
        raise MalformedRecordError("the query response has no cost or currency column")
    ccy = names.index("Currency")
    rows = props.get("rows") or []
    if not isinstance(rows, list):
        raise MalformedRecordError("rows is a list")
    out: list[billing.Total] = []
    bad = 0
    for row in rows:
        try:
            if not isinstance(row, list) or len(row) != len(names):
                raise MalformedRecordError("a row does not match its columns")
            out.append(billing.Total(
                subscription, billing.PROVIDER_TOTAL, f"{as_of:%Y-%m}", as_of,
                _amount(row[cost]), _currency(row[ccy]),
                raw_extra={"query": "ActualCost/MonthToDate"}))
        except MalformedRecordError:
            bad += 1
    return out, bad


def invoice_totals(body: bytes, subscription: str) -> tuple[list[billing.Total], int, str | None]:
    """An invoice listing page: its invoices, the count rejected, the next link."""
    doc = _json(body)
    invoices = doc.get("value") or []
    if not isinstance(invoices, list):
        raise MalformedRecordError("value is a list")
    out: list[billing.Total] = []
    bad = 0
    for inv in invoices:
        try:
            if not isinstance(inv, Mapping) or not isinstance(inv.get("properties"), Mapping):
                raise MalformedRecordError("an invoice has properties")
            p = inv["properties"]
            money = p.get("billedAmount") if isinstance(p.get("billedAmount"), Mapping) else (
                p.get("amountDue"))
            if not isinstance(money, Mapping):
                raise MalformedRecordError("an invoice has an amount")
            name = inv.get("name")
            if not isinstance(name, str) or not name.strip():
                raise MalformedRecordError("an invoice has a name")
            start = _day(p.get("invoicePeriodStartDate"), "invoicePeriodStartDate")
            extra = {k: p[k] for k in ("status", "invoiceType", "documentType",
                                       "invoicePeriodEndDate", "dueDate") if k in p}
            if isinstance(p.get("amountDue"), Mapping):
                extra["amountDue"] = dict(p["amountDue"])
            out.append(billing.Total(
                subscription, billing.INVOICE, f"{start:%Y-%m}",
                _day(p.get("invoiceDate"), "invoiceDate"), _amount(money.get("value")),
                _currency(money.get("currency")), name.strip(), extra))
        except MalformedRecordError:
            bad += 1
    link = doc.get("nextLink")
    return out, bad, link if isinstance(link, str) and link.strip() else None


class AzureBilling:
    """The two entities' fetch, inspect and process, for ``AzureSource``."""

    def __init__(self, client: AzureClient, billing_account: str | None,
                 today: Callable[[], date]) -> None:
        self._client = client
        self._account = billing_account
        self._today = today

    async def fetch(self, entity: str, cursor: str | None) -> Fetched:
        head = _queue(cursor)[0]
        if "subs" in head:
            today = self._today()
            meta = {"kind": SUBSCRIPTIONS, "as_of": today.isoformat(),
                    "start": _months_back(today, INVOICE_MONTHS).isoformat(),
                    "end": today.isoformat()}
            return Fetched(await self._client.subscriptions(head.get("next")), ".subs.json",
                           meta)
        sub = str(head["sub"])
        if entity == mapping.COST_TOTAL:
            return Fetched(await self._client.cost_query(sub), ".json",
                           {"kind": COST_QUERY, "subscription": sub,
                            "as_of": str(head["as_of"])})
        if self._account is None:
            raise ValueError("no billing account is configured for invoices")
        body = await self._client.invoices(self._account, sub,
                                           date.fromisoformat(str(head["start"])),
                                           date.fromisoformat(str(head["end"])),
                                           head.get("next"))
        return Fetched(body, ".json", {"kind": INVOICES, "subscription": sub})

    async def inspect(self, raw: RawStore, entity: str, ref: str, body: bytes,
                      cursor: str | None) -> PageInfo:
        queue = _queue(cursor)
        head, rest = queue[0], queue[1:]
        meta = await raw.meta(ref)
        kind = meta.get("kind")
        if kind == SUBSCRIPTIONS:
            listed = mapping.subscriptions(body)
            subs = sorted({*head.get("found", []), *(s.subscription_id for s in listed)})
            link = mapping.next_link(body)
            if link is not None:
                return PageInfo(len(listed), _cursor([{"subs": True, "next": link,
                                                       "found": subs}, *rest]))
            window = {"as_of": meta["as_of"]} if entity == mapping.COST_TOTAL else {
                "start": meta["start"], "end": meta["end"]}
            return PageInfo(len(listed), _cursor([{"sub": s, **window} for s in subs] + rest))
        # A response that does not parse is left for ``process`` to fail, so the
        # page dead-letters with its bronze reference.
        try:
            if kind == COST_QUERY:
                found_, bad_ = cost_totals(body, str(head["sub"]),
                                           date.fromisoformat(str(head["as_of"])))
                return PageInfo(len(found_) + bad_, _cursor(rest))
            found, bad, link = invoice_totals(body, str(head["sub"]))
        except MalformedRecordError:
            return PageInfo(0, _cursor(rest))
        if link is not None:
            return PageInfo(len(found) + bad, _cursor([{**head, "next": link}, *rest]))
        return PageInfo(len(found) + bad, _cursor(rest))

    async def process(self, db: Database, raw: RawStore, entity: str, ref: str,
                      ctx: PageContext) -> PageOutcome:
        meta = await raw.meta(ref)
        kind = meta.get("kind")
        body = await raw.get(ref)
        if kind == SUBSCRIPTIONS:
            listed = mapping.subscriptions(body)
            return await billing.store(db, ref, ctx, source=mapping.SOURCE, entity=entity,
                                       totals=[], records=len(listed))
        sub = meta.get("subscription", "")
        if kind == COST_QUERY and entity == mapping.COST_TOTAL:
            totals, bad = cost_totals(body, sub, date.fromisoformat(meta["as_of"]))
        elif kind == INVOICES and entity == mapping.INVOICE:
            totals, bad, _ = invoice_totals(body, sub)
        else:
            raise ValueError("bronze page is not this entity's billing response")
        return await billing.store(db, ref, ctx, source=mapping.SOURCE, entity=entity,
                                   totals=totals, records=len(totals) + bad, rejected=bad)

