"""Contract tests for the AWS billing entities (``cost_total``: Cost Explorer;
``invoice``: Invoicing) against recorded fixtures. No database, no Temporal
server: request shapes, Decimal parsing, malformed figures, paging, the
throttling backoff, bronze ordering, the shared merge, and the fixtures."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from cloudops_connectors.aws import mapping
from cloudops_connectors.aws.billing_source import (
    MalformedRecordError,
    PayerBilling,
    cost_totals,
    invoice_totals,
    month_to_date,
)
from cloudops_connectors.aws.client import (
    ConfigClient,
    CostExplorerClient,
    InvoicingClient,
    S3Client,
    ThrottledError,
)
from cloudops_connectors.aws.fake import (
    AGGREGATOR,
    BILLING_DAY,
    CE_HOST,
    INVOICING_HOST,
    REGION,
    FakeConfig,
    FakeCostExplorer,
    FakeInvoicing,
    FakeS3,
    load_fixture,
)
from cloudops_connectors.aws.source import AwsSource
from cloudops_connectors.core import billing
from cloudops_connectors.core.ingest import ConnectorActivities, FetchRequest
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_devdata.aws_fixtures import PAYERS, UNREGISTERED, USD_REPORT, build

USD_PAYER, EUR_PAYER = PAYERS[USD_REPORT], "900000000002"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def _no_sleep(_: float) -> None:
    return None


def _ce(fake: FakeCostExplorer, **kwargs: Any) -> CostExplorerClient:
    kwargs.setdefault("sleep", _no_sleep)
    return CostExplorerClient(fake.payer, base_url=CE_HOST, transport=fake.transport(), **kwargs)


def _inv(fake: FakeInvoicing, **kwargs: Any) -> InvoicingClient:
    kwargs.setdefault("sleep", _no_sleep)
    return InvoicingClient(fake.payer, base_url=INVOICING_HOST, transport=fake.transport(),
                           **kwargs)


def _source(ce: FakeCostExplorer, inv: FakeInvoicing) -> AwsSource:
    s3 = FakeS3.from_fixtures()
    return AwsSource(
        ConfigClient(REGION, AGGREGATOR, transport=FakeConfig.from_fixtures().transport()),
        S3Client(s3.bucket, REGION, transport=s3.transport()), s3.reports,
        billing=[PayerBilling(ce.payer, _ce(ce), _inv(inv))],
        today=lambda: date.fromisoformat(BILLING_DAY))


def _truth(kind: str) -> list[Any]:
    return [e for e in build().billing if e.kind == kind]


# --- Cost Explorer ------------------------------------------------------------------------


def test_month_to_date_is_the_month_so_far_or_last_month_on_the_first() -> None:
    assert month_to_date(date(2026, 10, 28)) == (date(2026, 10, 1), date(2026, 10, 28))
    assert month_to_date(date(2026, 11, 1)) == (date(2026, 10, 1), date(2026, 11, 1))
    assert month_to_date(date(2027, 1, 1)) == (date(2026, 12, 1), date(2027, 1, 1))
    assert month_to_date(date(2026, 10, 2)) == (date(2026, 10, 1), date(2026, 10, 2))


def test_cost_explorer_groups_become_provider_totals_in_decimal() -> None:
    found, bad, token = cost_totals(json.dumps(json.loads(load_fixture(
        f"billing/ce-{USD_PAYER}.json"))["2026-10-01/2026-10-28"]).encode())
    assert (bad, token) == (0, None)
    want = {e.account: e for e in _truth("provider_total") if e.currency == "USD"}
    assert {t.account for t in found} == set(want)
    for t in found:
        e = want[t.account]
        assert (t.kind, t.invoice_period, t.as_of) == ("provider_total", "2026-10",
                                                       date(2026, 10, 27))
        assert isinstance(t.amount, Decimal)
        assert (t.amount, t.currency) == (e.amount, e.currency)
        assert t.raw_extra["estimated"] is True


@pytest.mark.parametrize("group", [
    {"Keys": ["1234"], "Metrics": {"UnblendedCost": {"Amount": "1", "Unit": "USD"}}},
    {"Keys": ["123456789012"], "Metrics": {"UnblendedCost": {"Amount": "1,5", "Unit": "USD"}}},
    {"Keys": ["123456789012"], "Metrics": {"UnblendedCost": {"Amount": True, "Unit": "USD"}}},
    {"Keys": ["123456789012"], "Metrics": {"UnblendedCost": {"Amount": "1", "Unit": "usd"}}},
    {"Keys": ["123456789012"], "Metrics": {"BlendedCost": {"Amount": "1", "Unit": "USD"}}},
    {"Keys": [], "Metrics": {}}, "not a group",
])
def test_a_malformed_group_is_counted_not_guessed(group: Any) -> None:
    good = {"Keys": ["123456789012"],
            "Metrics": {"UnblendedCost": {"Amount": "10.25", "Unit": "USD"}}}
    body = json.dumps({"ResultsByTime": [{"TimePeriod": {"Start": "2026-10-01",
                                                         "End": "2026-10-28"},
                                          "Groups": [good, group]}]}).encode()
    found, bad, _ = cost_totals(body)
    assert ([t.amount for t in found], bad) == ([Decimal("10.25")], 1)


def test_a_float_amount_never_becomes_money() -> None:
    """``Amount`` is a string in Cost Explorer; a JSON float is parsed as Decimal
    text, never through ``float``."""
    body = (b'{"ResultsByTime": [{"TimePeriod": {"Start": "2026-10-01", "End": "2026-10-28"},'
            b' "Groups": [{"Keys": ["123456789012"], "Metrics": {"UnblendedCost":'
            b' {"Amount": 0.1000000000000000055511151231257827, "Unit": "USD"}}}]}]}')
    found, _, _ = cost_totals(body)
    assert found[0].amount == Decimal("0.1000000000000000055511151231257827")
    with pytest.raises(TypeError):
        billing.Total("123456789012", billing.INVOICE, "2026-10", date(2026, 10, 1),
                      0.1, "USD")  # type: ignore[arg-type]


async def test_cost_explorer_is_asked_for_the_month_grouped_by_linked_account() -> None:
    fake = FakeCostExplorer.from_fixtures(USD_PAYER)
    body = await _ce(fake).cost_and_usage(date(2026, 10, 1), date(2026, 10, 28), None)
    assert b"ResultsByTime" in body
    request = fake.requests[-1]
    assert request.headers["x-amz-target"] == "AWSInsightsIndexService.GetCostAndUsage"
    assert json.loads(request.content) == {
        "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-28"}, "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}]}
    with pytest.raises(ValueError, match="period"):
        await _ce(fake).cost_and_usage(date(2026, 10, 2), date(2026, 10, 2), None)
    with pytest.raises(httpx.HTTPStatusError):
        await _ce(fake).cost_and_usage(date(2026, 9, 1), date(2026, 9, 2), None)
    with pytest.raises(ValueError, match="payer"):
        CostExplorerClient("payer-one")


async def test_limit_exceeded_backs_off_then_succeeds_and_persistent_throttling_raises() -> None:
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    fake = FakeCostExplorer.from_fixtures(USD_PAYER)
    fake.throttle_next = 2
    client = _ce(fake, sleep=sleep, rand=lambda: 0.0)
    await client.cost_and_usage(date(2026, 10, 1), date(2026, 10, 28), None)
    assert waits == [0.5, 1.0]
    fake.throttle_next = 10
    with pytest.raises(ThrottledError):
        await _ce(fake, max_throttle_retries=1).cost_and_usage(
            date(2026, 10, 1), date(2026, 10, 28), None)


# --- Invoicing -------------------------------------------------------------------------------


def test_invoice_summaries_become_invoices_in_decimal() -> None:
    found, bad, token = invoice_totals(load_fixture(f"billing/invoices-{USD_PAYER}.json"))
    assert (bad, token) == (0, None)
    merged, conflicting = billing.merge(found)
    assert conflicting == 0
    want = {e.account: e for e in _truth("invoice")}
    assert {t.account: (t.invoice_period, t.as_of, t.amount, t.currency, t.document_ref)
            for t in merged} == {
        a: (e.invoice_period, e.as_of, e.amount, e.currency, e.document_ref)
        for a, e in want.items()}
    assert any(t.document_ref and "," in t.document_ref for t in merged), (
        "an invoice and its credit memo, issued together, are one figure")


@pytest.mark.parametrize("change", [
    {"AccountId": "12"}, {"InvoiceId": ""}, {"BillingPeriod": {"Month": 13, "Year": 2026}},
    {"BaseCurrencyAmount": {"TotalAmount": "abc", "CurrencyCode": "USD"}},
    {"BaseCurrencyAmount": {"TotalAmount": "1", "CurrencyCode": ""}},
    {"IssuedDate": "2026-10-02T08:00:00"}, {"IssuedDate": None},
])
def test_a_malformed_summary_is_counted_not_guessed(change: dict[str, Any]) -> None:
    good = json.loads(load_fixture(f"billing/invoices-{USD_PAYER}.json"))["InvoiceSummaries"][0]
    body = json.dumps({"InvoiceSummaries": [good, {**good, **change}]}).encode()
    found, bad, _ = invoice_totals(body)
    assert (len(found), bad) == (1, 1)


def test_issued_dates_read_as_epoch_seconds_or_zoned_iso() -> None:
    good = json.loads(load_fixture(f"billing/invoices-{USD_PAYER}.json"))["InvoiceSummaries"][0]
    for issued in (1790928000, "2026-10-02T08:00:00Z", "2026-10-02T10:00:00+02:00"):
        found, bad, _ = invoice_totals(json.dumps(
            {"InvoiceSummaries": [{**good, "IssuedDate": issued}]}).encode())
        assert (found[0].as_of, bad) == (date(2026, 10, 2), 0)


async def test_invoicing_is_asked_for_the_payer_and_an_issue_window() -> None:
    fake = FakeInvoicing.from_fixtures(USD_PAYER)
    await _inv(fake).invoice_summaries(date(2026, 8, 1), date(2026, 10, 28), None)
    sent = json.loads(fake.requests[-1].content)
    assert fake.requests[-1].headers["x-amz-target"] == "Invoicing.ListInvoiceSummaries"
    assert sent["Selector"] == {"ResourceType": "ACCOUNT_ID", "Value": USD_PAYER}
    window = sent["Filter"]["TimeInterval"]
    assert window["StartDate"] == int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
    assert window["EndDate"] > int(datetime(2026, 10, 28, 23, tzinfo=UTC).timestamp())


def test_a_body_that_is_not_json_is_malformed() -> None:
    with pytest.raises(MalformedRecordError):
        cost_totals(b"<html>")
    with pytest.raises(MalformedRecordError):
        invoice_totals(b"[]")


# --- the shared merge -----------------------------------------------------------------------


def test_figures_for_one_key_in_two_currencies_are_rejected() -> None:
    a = billing.Total("123456789012", billing.INVOICE, "2026-09", date(2026, 10, 2),
                      Decimal("10"), "USD", "A")
    b = billing.Total("123456789012", billing.INVOICE, "2026-09", date(2026, 10, 2),
                      Decimal("9"), "EUR", "B")
    c = billing.Total("210987654321", billing.INVOICE, "2026-09", date(2026, 10, 2),
                      Decimal("5"), "GBP", "C")
    merged, rejected = billing.merge([a, b, c])
    assert (merged, rejected) == ([c], 2)
    with pytest.raises(ValueError, match="kind"):
        billing.Total("123456789012", "estimate", "2026-09", date(2026, 10, 2), Decimal(1),
                      "USD")


# --- the source: scopes, paging and bronze ------------------------------------------------------


@pytest.mark.parametrize("entity", [mapping.COST_TOTAL, mapping.INVOICE])
async def test_billing_pages_are_in_bronze_before_parsing_and_page_with_tokens(
    entity: str
) -> None:
    ce, inv = FakeCostExplorer.from_fixtures(USD_PAYER), FakeInvoicing.from_fixtures(USD_PAYER)
    ce.page_size = inv.page_size = 1
    raw = MemoryRawStore()
    source = _source(ce, inv)
    assert await source.scopes(None, entity) == [USD_PAYER]  # type: ignore[arg-type]
    acts = ConnectorActivities(None, raw, source)  # type: ignore[arg-type]
    env = ActivityEnvironment()
    cursor, page, records = None, 0, 0
    while True:
        result = await env.run(acts.fetch_page, FetchRequest(
            "run-1", entity, USD_PAYER, T0.isoformat(), cursor, page))
        meta = raw.metadata[result.raw_ref]
        assert (meta["payer"], meta["end"]) == (USD_PAYER, BILLING_DAY)
        records += result.records
        page += 1
        cursor = result.next_cursor
        if cursor is None:
            break
    assert page > 1, "one figure per page"
    expected = 4 if entity == mapping.INVOICE else 3
    assert records == expected
    sent = [json.loads(r.content) for r in (ce if entity == mapping.COST_TOTAL
                                            else inv).requests]
    if entity == mapping.COST_TOTAL:
        assert {json.dumps(s["TimePeriod"], sort_keys=True) for s in sent} == {
            json.dumps({"Start": "2026-10-01", "End": BILLING_DAY}, sort_keys=True)}, (
            "every page, one period")
    first: dict[str, str] = {}
    for op, ref in raw.events:
        first.setdefault(ref, op)
    assert set(first.values()) == {"put"}


async def test_an_unconfigured_payer_is_refused() -> None:
    source = _source(FakeCostExplorer.from_fixtures(USD_PAYER),
                     FakeInvoicing.from_fixtures(USD_PAYER))
    with pytest.raises(ValueError, match="payer"):
        await source.fetch(mapping.COST_TOTAL, EUR_PAYER, T0, None)


# --- the fixtures ----------------------------------------------------------------------------


def test_the_fixtures_match_the_cur_except_one_drifting_account() -> None:
    truth = [e for e in build().billing if e.tenant_code is not None]
    drifting = [e for e in truth if abs(e.amount - e.ledger) > e.ledger * Decimal("0.005")]
    assert [(e.tenant_code, e.kind) for e in drifting] == [("FAB", "provider_total")]
    assert all(e.amount == e.ledger for e in truth if e not in drifting)
    assert {e.currency for e in truth if e.kind == "provider_total"} == {"USD", "EUR"}
    assert any(e.tenant_code is None for e in build().billing), "an account to quarantine"
    assert UNREGISTERED in {e.account for e in build().billing}
    assert date.fromisoformat(BILLING_DAY) - timedelta(days=1) == date(2026, 10, 27)
