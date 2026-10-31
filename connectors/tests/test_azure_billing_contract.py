"""Contract tests for the Azure billing entities (``cost_total``: Cost
Management query; ``invoice``: Billing invoices) against recorded fixtures. No
database, no Temporal server: request shapes, Decimal parsing, malformed
figures, throttling, the queue from the subscription listing, bronze ordering,
and the fixtures."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from cloudops_connectors.azure import mapping
from cloudops_connectors.azure.billing_source import (
    MalformedRecordError,
    cost_totals,
    invoice_totals,
)
from cloudops_connectors.azure.client import COST_QUERY, AzureClient, to_wait
from cloudops_connectors.azure.fake import (
    BILLING_ACCOUNT,
    BILLING_DAY,
    FIXTURE_HOST,
    FakeAzure,
    load_fixture,
)
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.ingest import ConnectorActivities, FetchRequest
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_devdata.azure_fixtures import UNREGISTERED, build_cost

T0 = datetime(2026, 1, 1, tzinfo=UTC)
DAY = date.fromisoformat(BILLING_DAY)


async def _no_sleep(_: float) -> None:
    return None


def _client(fake: FakeAzure, **kwargs: Any) -> AzureClient:
    kwargs.setdefault("sleep", _no_sleep)
    return AzureClient(FIXTURE_HOST, transport=fake.transport(), **kwargs)


def _source(fake: FakeAzure, account: str | None = BILLING_ACCOUNT) -> AzureSource:
    return AzureSource(_client(fake), billing_account=account, today=lambda: DAY)


def _truth(kind: str) -> dict[str, Any]:
    return {e.subscription: e for e in build_cost().billing if e.kind == kind}


# --- parsing -------------------------------------------------------------------------------


def test_a_query_response_becomes_a_provider_total_in_decimal() -> None:
    for sub, e in _truth("provider_total").items():
        found, bad = cost_totals(load_fixture(f"billing/query/{sub}.json"), sub, DAY)
        assert bad == 0
        (t,) = found
        assert (t.account, t.kind, t.invoice_period, t.as_of) == (
            sub, "provider_total", "2026-10", DAY)
        assert isinstance(t.amount, Decimal)
        assert (t.amount, t.currency) == (e.amount, e.currency)


@pytest.mark.parametrize("column", ["totalCost", "Cost", "PreTaxCost"])
def test_the_cost_column_is_found_by_name_wherever_it_is(column: str) -> None:
    body = json.dumps({"properties": {"columns": [{"name": "Currency"}, {"name": column}],
                                      "rows": [["GBP", "12.5"]]}}).encode()
    (t,), bad = cost_totals(body, "s", DAY)
    assert (t.amount, t.currency, bad) == (Decimal("12.5"), "GBP", 0)


def test_rows_that_do_not_fit_are_counted_and_a_response_without_columns_is_malformed() -> None:
    body = (b'{"properties": {"columns": [{"name": "totalCost"}, {"name": "Currency"}],'
            b' "rows": [[1.10, "EUR"], [true, "EUR"], [2, "eur"], [3], "x"]}}')
    found, bad = cost_totals(body, "s", DAY)
    assert ([t.amount for t in found], bad) == ([Decimal("1.10")], 4)
    with pytest.raises(MalformedRecordError):
        cost_totals(b'{"properties": {"columns": [{"name": "totalCost"}], "rows": []}}', "s", DAY)
    with pytest.raises(MalformedRecordError):
        cost_totals(b"not json", "s", DAY)


def test_an_invoice_listing_becomes_invoices_billed_amount_first() -> None:
    for sub, e in _truth("invoice").items():
        found, bad, link = invoice_totals(load_fixture(f"billing/invoices/{sub}.json"), sub)
        assert (bad, link) == (0, None)
        (t,) = found
        assert (t.invoice_period, t.as_of, t.amount, t.currency, t.document_ref) == (
            e.invoice_period, e.as_of, e.amount, e.currency, e.document_ref)
        assert t.raw_extra["amountDue"]["value"] == Decimal(0), "paid: due is kept, not used"


def test_amount_due_stands_in_when_there_is_no_billed_amount() -> None:
    inv = {"name": "G1", "properties": {
        "amountDue": {"currency": "GBP", "value": 99.99},
        "invoiceDate": "2026-10-06T00:00:00Z", "invoicePeriodStartDate": "2026-09-01"}}
    (t,), bad, _ = invoice_totals(json.dumps({"value": [inv]}).encode(), "s")
    assert (t.amount, t.currency, t.as_of, t.invoice_period, bad) == (
        Decimal("99.99"), "GBP", date(2026, 10, 6), "2026-09", 0)


@pytest.mark.parametrize("change", [
    {"name": ""}, {"properties": None},
    {"properties": {"billedAmount": {"currency": "EUR", "value": "x"},
                    "invoiceDate": "2026-10-05", "invoicePeriodStartDate": "2026-09-01"}},
    {"properties": {"billedAmount": {"currency": "EUR", "value": 1},
                    "invoiceDate": "soon", "invoicePeriodStartDate": "2026-09-01"}},
    {"properties": {"billedAmount": {"currency": "EUR", "value": 1},
                    "invoiceDate": "2026-10-05"}},
])
def test_a_malformed_invoice_is_counted_not_guessed(change: dict[str, Any]) -> None:
    good = {"name": "E1", "properties": {
        "billedAmount": {"currency": "EUR", "value": 1}, "invoiceDate": "2026-10-05",
        "invoicePeriodStartDate": "2026-09-01"}}
    found, bad, _ = invoice_totals(json.dumps({"value": [good, {**good, **change}],
                                               "nextLink": "https://x/next"}).encode(), "s")
    assert (len(found), bad) == (1, 1)


# --- the client -------------------------------------------------------------------------------


async def test_the_cost_query_is_fixed_and_per_subscription() -> None:
    fake = FakeAzure.from_fixtures()
    sub = next(iter(_truth("provider_total")))
    await _client(fake).cost_query(sub)
    request = fake.requests[-1]
    assert request.method == "POST"
    assert request.url.path == (f"/subscriptions/{sub}/providers/Microsoft.CostManagement/"
                                "query")
    assert request.url.params["api-version"] == "2023-11-01"
    assert json.loads(request.content) == COST_QUERY
    assert COST_QUERY["type"] == "ActualCost"
    assert COST_QUERY["timeframe"] == "MonthToDate"
    with pytest.raises(ValueError, match="subscription"):
        await _client(fake).cost_query("../providers/x")
    empty = await _client(fake).cost_query("00000000-0000-0000-0000-000000000000")
    assert cost_totals(empty, "s", DAY) == ([], 0), "nothing recorded: no cost, no row"


async def test_invoices_are_read_for_the_billing_account_and_a_window() -> None:
    fake = FakeAzure.from_fixtures()
    sub = next(iter(_truth("invoice")))
    await _client(fake).invoices(BILLING_ACCOUNT, sub, date(2026, 7, 1), DAY)
    request = fake.requests[-1]
    assert "/billingAccounts/12345678%3Agetronics-fixture_2019-05-31/" in str(request.url)
    assert (request.url.params["periodStartDate"], request.url.params["periodEndDate"]) == (
        "2026-07-01", BILLING_DAY)
    with pytest.raises(ValueError, match="billing account"):
        await _client(fake).invoices("../../x", sub, date(2026, 7, 1), DAY)
    with pytest.raises(ValueError, match="management endpoint"):
        await _client(fake).invoices(BILLING_ACCOUNT, sub, date(2026, 7, 1), DAY,
                                     "https://elsewhere.example/next")
    with pytest.raises(httpx.HTTPStatusError):
        await _client(fake).invoices("someone-else", sub, date(2026, 7, 1), DAY)


def test_a_cost_management_429_waits_its_own_retry_header() -> None:
    headers = httpx.Headers({"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "12",
                             "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "3"})
    assert to_wait(429, headers) == 12
    assert to_wait(429, httpx.Headers({"retry-after": "7", **headers})) == 7


# --- the source: the queue and bronze ----------------------------------------------------------


@pytest.mark.parametrize("entity", [mapping.COST_TOTAL, mapping.INVOICE])
async def test_each_listed_subscription_is_read_once_and_every_page_is_in_bronze_first(
    entity: str
) -> None:
    fake, raw = FakeAzure.from_fixtures(subs_page=4), MemoryRawStore()
    acts = ConnectorActivities(None, raw, _source(fake))  # type: ignore[arg-type]
    env = ActivityEnvironment()
    cursor, page, kinds, subs = None, 0, [], []
    while True:
        result = await env.run(acts.fetch_page, FetchRequest(
            "run-1", entity, "", T0.isoformat(), cursor, page))
        meta = raw.metadata[result.raw_ref]
        kinds.append(meta["kind"])
        if meta["kind"] == "subscriptions":
            assert meta["as_of"] == BILLING_DAY
            assert meta["start"] == "2026-07-01"
        else:
            subs.append(meta["subscription"])
        page += 1
        cursor = result.next_cursor
        if cursor is None:
            break
    listed = len(json.loads(load_fixture("subscriptions.json"))["value"])
    assert kinds.count("subscriptions") == -(-listed // 4), "the listing pages by nextLink"
    assert len(subs) == len(set(subs)), "each subscription once"
    assert len(subs) == listed
    assert UNREGISTERED in subs
    first: dict[str, str] = {}
    for op, ref in raw.events:
        first.setdefault(ref, op)
    assert set(first.values()) == {"put"}


async def test_invoices_without_a_billing_account_are_refused() -> None:
    source = _source(FakeAzure.from_fixtures(), account=None)
    cursor = json.dumps([{"sub": UNREGISTERED, "start": "2026-07-01", "end": BILLING_DAY}])
    with pytest.raises(ValueError, match="billing account"):
        await source.fetch(mapping.INVOICE, "", T0, cursor)


# --- the fixtures ----------------------------------------------------------------------------


def test_the_fixtures_match_the_exports_except_one_drifting_subscription() -> None:
    truth = [e for e in build_cost().billing if e.tenant_code is not None]
    drifting = [e for e in truth if abs(e.amount - e.ledger) > e.ledger * Decimal("0.005")]
    assert [(e.tenant_code, e.kind) for e in drifting] == [("CRL", "provider_total")]
    assert all(e.amount == e.ledger for e in truth if e not in drifting)
    assert {e.currency for e in truth} == {"EUR", "GBP"}
    assert {e.kind for e in truth} == {"provider_total", "invoice"}
    assert any(e.tenant_code is None for e in build_cost().billing)
