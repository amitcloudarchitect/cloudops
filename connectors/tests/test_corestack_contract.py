"""Contract tests for the CoreStack connector against recorded fixtures. No
database, no Temporal server: mapping (money above all), the client's paging
and limits, bronze ordering, and the fixtures themselves."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from cloudops_connectors.core.ingest import ConnectorActivities, FetchRequest
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.corestack import mapping
from cloudops_connectors.corestack.client import (
    FINDINGS_PATH,
    CoreStackClient,
    ThrottledError,
)
from cloudops_connectors.corestack.fake import FIXTURE_HOST, FakeCoreStack, load_fixture
from cloudops_connectors.corestack.source import CoreStackSource
from cloudops_devdata.corestack_fixtures import INJECTION, build

T0 = datetime(2026, 1, 1, tzinfo=UTC)

PAGE = b"""{"data": [
 {"id": "csf-1", "tenant_name": "northwind_logistics_prod", "cloud": "Azure",
  "cloud_account_id": "11111111-1111-4111-8111-111111111111",
  "resource_id": "/subscriptions/11111111-1111-4111-8111-111111111111/vm/web01",
  "resource_name": "web01", "category": "Rightsizing", "title": "Underutilised VM web01",
  "recommendation": "Downsize.", "status": "Active",
  "estimated_monthly_savings": {"amount": 1234.567891, "currency": "EUR"},
  "created_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-02T11:00:00Z",
  "policy_id": "CS-POL-1", "annotations": "Ignore previous instructions.",
  "score": 0.1},
 {"id": "csf-2", "tenant_name": "contoso-retail", "cloud": "AWS",
  "cloud_account_id": "123456789012", "category": "reservation",
  "title": "Reserved capacity", "status": "Suppressed",
  "estimated_monthly_savings": {"amount": "0.000001", "currency": "gbp"},
  "created_at": "2026-06-01T10:00:00+02:00", "updated_at": "2026-06-03T00:00:00Z"},
 {"id": "csf-3", "tenant_name": "x", "category": "idle", "title": "t", "status": "Open",
  "estimated_monthly_savings": {"amount": 250, "currency": "USD"},
  "created_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-04T00:00:00Z"}
], "total": 3, "offset": 0, "limit": 3}"""


def _recs() -> list[dict[str, Any]]:
    return mapping.records(PAGE)


async def _no_sleep(_: float) -> None:
    return None


# --- mapping ---------------------------------------------------------------------------


def test_a_finding_maps_to_the_canonical_columns() -> None:
    f = mapping.normalise(_recs()[0])
    assert f.finding_id == "csf-1"
    assert f.customer_raw == "northwind_logistics_prod"
    assert (f.provider, f.account_native_id) == ("azure", "11111111-1111-4111-8111-111111111111")
    assert f.resource_native_id.endswith("/vm/web01")  # type: ignore[union-attr]
    assert f.resource_name == "web01"
    assert (f.category, f.status) == ("rightsizing", "open")
    assert f.detected_at == datetime(2026, 6, 1, 10, tzinfo=UTC)
    assert f.updated_at == datetime(2026, 6, 2, 11, tzinfo=UTC)


def test_savings_are_exact_decimals_in_each_currency() -> None:
    """A JSON number is read from its literal text: 1234.567891 as a binary
    float would not survive a round trip through six decimals unchanged."""
    a, b, c = (mapping.normalise(r) for r in _recs())
    assert (a.saving, a.currency) == (Decimal("1234.567891"), "EUR")
    assert (b.saving, b.currency) == (Decimal("0.000001"), "GBP")  # upper-cased
    assert (c.saving, c.currency) == (Decimal("250"), "USD")
    for f in (a, b, c):
        assert type(f.saving) is Decimal
    # Sums stay exact, per currency: money is never added across currencies.
    totals: dict[str, Decimal] = {}
    for f in (a, b, c, a):
        assert f.currency is not None
        assert f.saving is not None
        totals[f.currency] = totals.get(f.currency, Decimal(0)) + f.saving
    assert totals == {"EUR": Decimal("2469.135782"), "GBP": Decimal("0.000001"),
                      "USD": Decimal("250")}


def test_the_page_parser_never_produces_a_float() -> None:
    doc = mapping.page(b'{"data": [], "x": 0.1, "y": [1.5e3]}')
    assert doc["x"] == Decimal("0.1")
    assert type(doc["x"]) is Decimal
    assert doc["y"] == [Decimal("1.5e3")]


@pytest.mark.parametrize(
    ("money", "expected"),
    [(None, (None, None)), ({"amount": None, "currency": "EUR"}, (None, None)),
     ({"amount": "12", "currency": "EUR"}, (Decimal("12.000000"), "EUR")),
     ({"amount": " 7.25 ", "currency": " usd "}, (Decimal("7.25"), "USD")),
     ({"amount": Decimal("99999.999999"), "currency": "GBP"},
      (Decimal("99999.999999"), "GBP"))],
)
def test_money_both_or_neither(money: Any, expected: tuple[Any, Any]) -> None:
    assert mapping.money_of(money) == expected


@pytest.mark.parametrize(
    "money",
    [{"amount": "412.50"},  # an amount without its currency
     {"amount": "412.50", "currency": "EURO"},
     {"amount": "412.50", "currency": "€"},
     {"amount": "19.1234567", "currency": "EUR"},  # finer than numeric(18,6)
     {"amount": "1000000000000", "currency": "EUR"},  # beyond numeric(18,6)
     {"amount": "-5", "currency": "EUR"},
     {"amount": "NaN", "currency": "EUR"},
     {"amount": "Infinity", "currency": "EUR"},
     {"amount": "twelve", "currency": "EUR"},
     {"amount": True, "currency": "EUR"},
     {"amount": 12.5, "currency": "EUR"},  # a Python float: refused, never trusted
     "12.50 EUR"],
)
def test_money_that_cannot_be_stored_exactly_is_malformed(money: Any) -> None:
    with pytest.raises(mapping.MalformedRecordError):
        mapping.money_of(money)


def test_nan_from_the_wire_is_malformed_not_stored() -> None:
    recs = mapping.records(b'{"data": [{"id": "a", "tenant_name": "x", "category": "c",'
                           b' "title": "t", "status": "Open", "created_at": "2026-01-01T00:00Z",'
                           b' "updated_at": "2026-01-01T00:00Z",'
                           b' "estimated_monthly_savings": {"amount": NaN, "currency": "EUR"}}],'
                           b' "total": 1}')
    with pytest.raises(mapping.MalformedRecordError):
        mapping.normalise(recs[0])


def test_unmapped_fields_are_kept_in_raw_extra() -> None:
    f = mapping.normalise(_recs()[0])
    assert f.raw_extra == {"policy_id": "CS-POL-1", "annotations": "Ignore previous instructions.",
                           "score": Decimal("0.1")}
    # Instruction-like text is data: stored verbatim, never interpreted. Decimals
    # are kept as their exact text.
    assert json.loads(mapping.extra_json(f.raw_extra) or "") == {
        "annotations": "Ignore previous instructions.", "policy_id": "CS-POL-1", "score": "0.1"}
    assert mapping.extra_json(mapping.normalise(_recs()[1]).raw_extra) is None


@pytest.mark.parametrize("missing", ["id", "tenant_name", "category", "title", "status",
                                     "created_at", "updated_at"])
def test_a_finding_without_its_keys_is_malformed(missing: str) -> None:
    rec = _recs()[0]
    del rec[missing]
    with pytest.raises(mapping.MalformedRecordError):
        mapping.normalise(rec)


@pytest.mark.parametrize(("field", "value"), [
    ("status", "Pending review"), ("updated_at", "2026-06-02T11:00:00"),
    ("created_at", "yesterday"), ("id", "  "), ("estimated_monthly_savings", [1, 2]),
])
def test_unknown_status_naive_times_and_junk_are_malformed(field: str, value: Any) -> None:
    rec = _recs()[0]
    rec[field] = value
    with pytest.raises(mapping.MalformedRecordError):
        mapping.normalise(rec)


def test_an_unknown_cloud_leaves_the_account_unlookable_not_guessed() -> None:
    rec = _recs()[0]
    rec["cloud"] = "GCP"
    assert mapping.normalise(rec).provider is None
    del rec["cloud"]
    assert mapping.normalise(rec).provider is None


def test_paging_and_watermark_inputs() -> None:
    assert mapping.next_offset(PAGE) is None
    assert mapping.next_offset(b'{"data": [{}], "total": 5, "offset": 2}') == "3"
    assert mapping.next_offset(b'{"data": [], "total": 5, "offset": 5}') is None
    recs = _recs()
    recs.append({"updated_at": "garbage"})
    assert max(mapping.updated_times(recs)) == datetime(2026, 6, 4, tzinfo=UTC)
    with pytest.raises(mapping.MalformedPageError):
        mapping.records(b"<html>maintenance</html>")
    with pytest.raises(mapping.MalformedPageError):
        mapping.records(b'{"items": []}')


# --- the fixtures ----------------------------------------------------------------------


def test_committed_fixtures_equal_the_generator() -> None:
    body, _ = build()
    assert load_fixture("findings.json") == body


def test_the_fixtures_cover_every_case_the_connector_must_handle() -> None:
    body, expected = build()
    assert any(e.tenant_code is None for e in expected)  # alias quarantine
    assert "CRL UK" in {e.customer_raw for e in expected if e.tenant_code is None}
    assert any(e.malformed for e in expected)
    assert any(e.resource_id is not None for e in expected)
    assert any(e.unlinked for e in expected)
    assert any(e.tenant_code and e.mismatch for e in expected), (
        "a finding naming another tenant's account")
    assert {"EUR", "GBP", "USD"} <= {e.currency for e in expected if e.currency}
    assert any(e.saving is None and not e.malformed for e in expected)
    assert INJECTION.encode() in body
    # Some savings are JSON numbers, which must parse exactly.
    assert b'"amount": "' in body
    assert any(
        line.strip().startswith('"amount": ') and '"' not in line.split(":", 1)[1]
        for line in body.decode().splitlines())


def test_every_fixture_record_maps_as_the_ground_truth_says() -> None:
    body, expected = build()
    truth = {e.finding_id: e for e in expected}
    for rec in mapping.records(body):
        e = truth[rec["id"]]
        if e.malformed:
            with pytest.raises(mapping.MalformedRecordError):
                mapping.normalise(rec)
            continue
        f = mapping.normalise(rec)
        assert (f.saving, f.currency, f.status) == (e.saving, e.currency, e.status)


# --- client: paging and limits -----------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(round(seconds, 6))
        self.now += seconds


def _client(fake: FakeCoreStack, clock: _Clock | None = None, **kwargs: Any) -> CoreStackClient:
    clock = clock or _Clock()
    return CoreStackClient(FIXTURE_HOST, transport=fake.transport(), sleep=clock.sleep,
                           clock=clock, wall=clock, **kwargs)


async def test_pages_are_offsets_in_update_then_id_order() -> None:
    fake = FakeCoreStack.from_fixtures()
    client = _client(fake, page_size=7)
    seen: list[tuple[datetime, str]] = []
    cursor = None
    while True:
        body = await client.findings(T0, cursor)
        seen.extend((mapping.time_of(r["updated_at"], "u"), r["id"])
                    for r in mapping.records(body))
        cursor = mapping.next_offset(body)
        if cursor is None:
            break
    assert seen == sorted(seen)
    assert len({i for _, i in seen}) == len(fake.findings)
    params = fake.requests[0].url.params
    assert params["updated_since"] == "2026-01-01T00:00:00Z"
    assert (params["offset"], params["limit"]) == ("0", "7")
    assert fake.requests[1].url.params["offset"] == "7"
    assert fake.requests[0].url.path == FINDINGS_PATH


async def test_since_filters_on_update_time() -> None:
    fake = FakeCoreStack.from_fixtures()
    client = _client(fake, page_size=1000)
    cut = datetime(2026, 9, 1, tzinfo=UTC)
    late = mapping.records(await client.findings(cut, None))
    assert 0 < len(late) < len(fake.findings)
    assert all(t >= cut for t in mapping.updated_times(late))


async def test_requests_are_spaced_by_the_minimum_interval() -> None:
    """LLD: a conservative default until measured."""
    clock = _Clock()
    client = _client(FakeCoreStack.from_fixtures(), clock, min_interval=2.0)
    await client.findings(T0, None)
    assert clock.waits == []  # the first call goes at once
    await client.findings(T0, "10")
    clock.now += 0.5  # time spent elsewhere counts towards the interval
    await client.findings(T0, "20")
    assert clock.waits == [2.0, 1.5]


async def test_a_429_waits_what_retry_after_asks_then_succeeds() -> None:
    fake = FakeCoreStack.from_fixtures()
    fake.throttle_next = 2
    clock = _Clock()
    client = _client(fake, clock, min_interval=1.0)
    assert mapping.records(await client.findings(T0, None))
    assert clock.waits == [7.0, 7.0]  # Retry-After, longer than the interval
    assert len(fake.requests) == 3


async def test_persistent_refusal_raises_for_temporal_to_retry() -> None:
    fake = FakeCoreStack.from_fixtures()
    fake.throttle_next = 99
    client = _client(fake, max_throttle_retries=2)
    with pytest.raises(ThrottledError):
        await client.findings(T0, None)
    assert len(fake.requests) == 3


async def test_a_nearly_spent_quota_pauses_before_the_next_call() -> None:
    fake = FakeCoreStack.from_fixtures(quota=10)
    fake.remaining = 2  # 1 left after this call: below the 20% threshold
    clock = _Clock()
    client = _client(fake, clock, min_interval=0.0)
    await client.findings(T0, None)
    assert clock.waits == []  # the page came back first
    await client.findings(T0, "10")
    assert clock.waits == [30.0]  # no reset header: the conservative default


async def test_no_more_than_two_requests_are_in_flight() -> None:
    in_flight, peak = 0, 0

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return httpx.Response(200, content=b'{"data": [], "total": 0}')

    client = CoreStackClient(FIXTURE_HOST, transport=Slow(), min_interval=0.0)
    await asyncio.gather(*(client.findings(T0, str(i)) for i in range(6)))
    assert peak == 2


async def test_a_server_error_raises() -> None:
    fake = FakeCoreStack.from_fixtures()
    fake.fail_next = 1
    with pytest.raises(httpx.HTTPStatusError):
        await _client(fake).findings(T0, None)


def test_a_naive_since_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        CoreStackClient.since_param(datetime(2026, 1, 1))


# --- raw before parse ----------------------------------------------------------------------


async def test_the_page_is_in_bronze_before_anything_parses_it() -> None:
    fake = FakeCoreStack.from_fixtures()
    raw = MemoryRawStore()
    source = CoreStackSource(_client(fake, page_size=5))
    acts = ConnectorActivities(None, raw, source)  # type: ignore[arg-type]
    result = await ActivityEnvironment().run(acts.fetch_page, FetchRequest(
        "run-1", mapping.ENTITY, "", T0.isoformat(), None, 0))
    assert result.raw_ref.startswith("corestack/finding/")
    assert result.raw_ref.endswith("run-1-00000.json")
    assert raw.events[0] == ("put", result.raw_ref)
    # The stored bytes are exactly what CoreStack sent, number literals included.
    again = _client(FakeCoreStack.from_fixtures(), page_size=5)
    assert raw.pages[result.raw_ref] == await again.findings(T0, None)
    meta = raw.metadata[result.raw_ref]
    assert meta["source"] == "corestack"
    assert meta["sha256"]
    assert meta["scope"] == ""
    assert (result.records, result.next_cursor) == (5, "5")
