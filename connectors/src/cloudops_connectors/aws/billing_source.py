"""AWS provider totals and invoices -> ``cloudops.billing_total`` (``core/billing.py``).

Two entities, both daily, one scope per payer (management) account: the
billing APIs answer for the payer's whole organisation, each figure naming
its linked account.

- ``cost_total``: Cost Explorer ``GetCostAndUsage``, MONTHLY, ``UnblendedCost``
  grouped by ``LINKED_ACCOUNT``, from the first of the month to today (to the
  end of last month on the 1st) -> ``provider_total``, ``as_of`` the period's
  end minus one day (Cost Explorer's end is exclusive), period its start month.
- ``invoice``: Invoicing ``ListInvoiceSummaries`` for the payer, invoices
  issued in the last ``INVOICE_DAYS`` days -> ``invoice``:
  ``BaseCurrencyAmount.TotalAmount`` in its ``CurrencyCode``, ``InvoiceId`` ->
  document_ref, ``BillingPeriod`` -> period, ``IssuedDate`` -> as_of, and the
  summary's ``AccountId`` the account (with invoice units, the linked account
  the invoice is for).

The cursor (JSON) holds the request's dates and the API's page token, so every
page of a run asks for the same period. The figures come from the response
(its ``TimePeriod``, its dates), never from the request, and are parsed
straight to ``Decimal``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from cloudops_connectors.aws import mapping
from cloudops_connectors.aws.client import CostExplorerClient, InvoicingClient
from cloudops_connectors.core import billing, identity
from cloudops_connectors.core.ingest import Fetched, PageContext, PageInfo, PageOutcome
from cloudops_connectors.core.raw import RawStore
from cloudops_tenancy import Database

COST_AND_USAGE: Final = "cost_and_usage"
INVOICE_SUMMARIES: Final = "invoice_summaries"
INVOICE_DAYS: Final = 92
METRIC: Final = "UnblendedCost"


class MalformedRecordError(ValueError):
    """A group or summary without what keys it: kept in bronze, counted, skipped."""


@dataclass(frozen=True)
class PayerBilling:
    """One payer's billing clients (each signed for that payer)."""

    payer: str
    cost_explorer: CostExplorerClient
    invoicing: InvoicingClient


def _json(body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MalformedRecordError("payload is not JSON") from None
    if not isinstance(doc, dict):
        raise MalformedRecordError("payload is not a JSON object")
    return doc


def _account(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not mapping.ACCOUNT_ID.fullmatch(text):
        raise MalformedRecordError("an account id is twelve digits")
    return text


def _amount(value: Any) -> Decimal:
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise MalformedRecordError("an amount is a decimal")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise MalformedRecordError("an amount is a decimal") from None
    if not amount.is_finite():
        raise MalformedRecordError("an amount is finite")
    return amount


def _currency(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not mapping.CURRENCY.fullmatch(text):
        raise MalformedRecordError("a currency is three capitals")
    return text


def _date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise MalformedRecordError("not an ISO date") from None


def _issued(value: Any) -> date:
    """Epoch seconds (the JSON protocol's timestamps), or an ISO date-time."""
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return datetime.fromtimestamp(int(value), UTC).date()
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise MalformedRecordError("IssuedDate is not a time") from None
        if moment.tzinfo is None:
            raise MalformedRecordError("IssuedDate has no time zone")
        return moment.astimezone(UTC).date()
    raise MalformedRecordError("IssuedDate is required")


def _list(doc: Mapping[str, Any], name: str) -> list[Any]:
    value = doc.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise MalformedRecordError(f"{name} is a list")
    return value


def cost_totals(body: bytes) -> tuple[list[billing.Total], int, str | None]:
    """A ``GetCostAndUsage`` page: its totals, the count rejected, the next token."""
    doc = _json(body)
    out: list[billing.Total] = []
    bad = 0
    for result in _list(doc, "ResultsByTime"):
        if not isinstance(result, Mapping) or not isinstance(result.get("TimePeriod"), Mapping):
            bad += 1
            continue
        try:
            start = _date(result["TimePeriod"].get("Start"))
            end = _date(result["TimePeriod"].get("End"))
        except MalformedRecordError:
            bad += len(result.get("Groups") or []) or 1
            continue
        for group in result.get("Groups") or []:
            try:
                if not isinstance(group, Mapping):
                    raise MalformedRecordError("a group is an object")
                keys = group.get("Keys")
                metric = (group.get("Metrics") or {}).get(METRIC)
                if not isinstance(keys, list) or len(keys) != 1 or not isinstance(metric,
                                                                                   Mapping):
                    raise MalformedRecordError("a group has one key and its metric")
                out.append(billing.Total(
                    _account(keys[0]), billing.PROVIDER_TOTAL, f"{start:%Y-%m}",
                    end - timedelta(days=1), _amount(metric.get("Amount")),
                    _currency(metric.get("Unit")),
                    raw_extra={"time_period": {"start": start.isoformat(),
                                               "end": end.isoformat()},
                               "estimated": bool(result.get("Estimated"))}))
            except MalformedRecordError:
                bad += 1
    token = doc.get("NextPageToken")
    return out, bad, token if isinstance(token, str) and token else None


def invoice_totals(body: bytes) -> tuple[list[billing.Total], int, str | None]:
    """A ``ListInvoiceSummaries`` page: its invoices, the count rejected, the next token."""
    doc = _json(body)
    out: list[billing.Total] = []
    bad = 0
    for s in _list(doc, "InvoiceSummaries"):
        try:
            if not isinstance(s, Mapping):
                raise MalformedRecordError("a summary is an object")
            period = s.get("BillingPeriod")
            amount = s.get("BaseCurrencyAmount")
            invoice = s.get("InvoiceId")
            if not isinstance(period, Mapping) or not isinstance(amount, Mapping) or not (
                    isinstance(invoice, str) and invoice.strip()):
                raise MalformedRecordError("a summary has its period, amount and id")
            year, month = period.get("Year"), period.get("Month")
            if not isinstance(year, int) or not isinstance(month, int) or not 1 <= month <= 12:
                raise MalformedRecordError("BillingPeriod is a year and a month")
            extra = {k: s[k] for k in ("InvoiceType", "OriginalInvoiceId", "DueDate")
                     if k in s}
            entity = s.get("Entity")
            if isinstance(entity, Mapping) and isinstance(entity.get("InvoicingEntity"), str):
                extra["invoicing_entity"] = entity["InvoicingEntity"]
            out.append(billing.Total(
                _account(s.get("AccountId")), billing.INVOICE, f"{year:04d}-{month:02d}",
                _issued(s.get("IssuedDate")), _amount(amount.get("TotalAmount")),
                _currency(amount.get("CurrencyCode")), invoice.strip(), extra))
        except MalformedRecordError:
            bad += 1
    token = doc.get("NextToken")
    return out, bad, token if isinstance(token, str) and token else None


def month_to_date(today: date) -> tuple[date, date]:
    """Cost Explorer's period for a run on ``today``: [start, end)."""
    start = (today - timedelta(days=1)).replace(day=1)
    return start, today


class AwsBilling:
    """The two entities' fetch, inspect and process, for ``AwsSource``."""

    def __init__(self, payers: list[PayerBilling], today: Callable[[], date],
                 binding: identity.Binding | None = None) -> None:
        """``binding``: a customer's own access key (ADR 0027), which may ask only
        for its own account and whose figures resolve to its tenant only."""
        self._payers = {p.payer: p for p in payers}
        if len(self._payers) != len(payers):
            raise ValueError("a payer is configured twice")
        if binding is not None and not set(self._payers) <= binding.native_ids:
            raise identity.TargetOutOfScopeError("a payer outside the credential's scope")
        self._today = today
        self._binding = binding

    @property
    def payers(self) -> list[str]:
        return list(self._payers)

    def _payer(self, scope: str) -> PayerBilling:
        try:
            return self._payers[scope]
        except KeyError:
            raise ValueError("not a configured payer") from None

    async def fetch(self, entity: str, scope: str, cursor: str | None) -> Fetched:
        payer = self._payer(scope)
        at = json.loads(cursor) if cursor else {}
        if not at:
            today = self._today()
            start, end = (month_to_date(today) if entity == mapping.COST_TOTAL
                          else (today - timedelta(days=INVOICE_DAYS), today))
            at = {"start": start.isoformat(), "end": end.isoformat(), "token": None}
        start, end = date.fromisoformat(at["start"]), date.fromisoformat(at["end"])
        meta = {"start": at["start"], "end": at["end"], "payer": payer.payer}
        if entity == mapping.COST_TOTAL:
            body = await payer.cost_explorer.cost_and_usage(start, end, at["token"])
            return Fetched(body, ".json", {**meta, "kind": COST_AND_USAGE})
        body = await payer.invoicing.invoice_summaries(start, end, at["token"])
        return Fetched(body, ".json", {**meta, "kind": INVOICE_SUMMARIES})

    async def inspect(self, raw: RawStore, entity: str, ref: str, body: bytes) -> PageInfo:
        meta = await raw.meta(ref)
        try:
            found, bad, token = (cost_totals(body) if entity == mapping.COST_TOTAL
                                 else invoice_totals(body))
        except MalformedRecordError:
            return PageInfo(0, None)  # ``process`` fails it: dead-lettered, in bronze
        if token is None:
            return PageInfo(len(found) + bad, None)
        return PageInfo(len(found) + bad, json.dumps(
            {"start": meta["start"], "end": meta["end"], "token": token}, sort_keys=True))

    async def process(self, db: Database, raw: RawStore, entity: str, ref: str,
                      ctx: PageContext) -> PageOutcome:
        kind = (await raw.meta(ref)).get("kind")
        body = await raw.get(ref)
        if kind == COST_AND_USAGE and entity == mapping.COST_TOTAL:
            totals, bad, _ = cost_totals(body)
        elif kind == INVOICE_SUMMARIES and entity == mapping.INVOICE:
            totals, bad, _ = invoice_totals(body)
        else:
            raise ValueError("bronze page is not this entity's billing response")
        return await billing.store(db, ref, ctx, source=mapping.SOURCE, entity=entity,
                                   totals=totals, records=len(totals) + bad, rejected=bad,
                                   binding=self._binding)
