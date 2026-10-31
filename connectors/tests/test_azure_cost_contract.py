"""Contract tests for the Azure Cost Management FOCUS exports (entity
``cost_export``) against recorded fixtures. No database, no Temporal server:
the charge-class rule, Decimal parsing, the manifest and the blob listing, the
blob client's backoff, the export queue, bronze ordering, and the fixtures."""

from __future__ import annotations

import gzip
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from cloudops_connectors.azure import focus, mapping
from cloudops_connectors.azure.blob import BlobClient, BlobNotFoundError, ThrottledError
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import (
    BLOB_HOST,
    FIXTURE_HOST,
    FakeAzure,
    FakeBlobStore,
    load_fixture,
)
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.ingest import ConnectorActivities, FetchRequest
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.core.xmlrec import MalformedXmlError
from cloudops_devdata.aws_fixtures import CostKey
from cloudops_devdata.azure_fixtures import (
    EUR_ROOT,
    GBP_ROOT,
    INJECTION,
    UNREGISTERED,
    build_cost,
)
from cloudops_devdata.servicenow_fixtures import FIXTURE_ANCHOR, FIXTURE_SEED
from cloudops_devdata.synthetic import generate

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SUB = "89ef84c7-bf5f-5ef2-9bdf-3b4e0ce59a7f"
ROW: dict[str, str] = {
    "SubAccountId": f"/subscriptions/{SUB.upper()}", "SubAccountName": "Prod",
    "ResourceId": f"/subscriptions/{SUB}/resourceGroups/RG-NWL/providers/"
                  "Microsoft.Compute/virtualMachines/nwl-web-01",
    "ChargePeriodStart": "2026-10-02T00:00:00Z", "ChargePeriodEnd": "2026-10-03T00:00:00Z",
    "BillingPeriodStart": "2026-10-01T00:00:00Z", "BilledCost": "12.3456789",
    "EffectiveCost": "10.0000001", "BillingCurrency": "EUR", "ServiceName": "Virtual Machines",
    "ChargeCategory": "Usage", "PublisherName": "Microsoft", "x_PublisherType": "Microsoft",
    "x_SkuMeterCategory": "Virtual Machines", "CommitmentDiscountType": "",
    "CommitmentDiscountStatus": "", "Tags": '{"env": "prod", "note": "' + INJECTION + '"}',
    "SkuId": "DZH318Z0BQ4L", "ChargeDescription": "D4s v5",
}


async def _no_sleep(_: float) -> None:
    return None


def _blobs(fake: FakeBlobStore, **kwargs: Any) -> BlobClient:
    kwargs.setdefault("sleep", _no_sleep)
    return BlobClient(BLOB_HOST, transport=fake.transport(), **kwargs)


def _source(fake: FakeBlobStore, **kwargs: Any) -> AzureSource:
    return AzureSource(AzureClient(FIXTURE_HOST, transport=FakeAzure.from_fixtures().transport()),
                       exports=_blobs(fake, **kwargs), export_roots=fake.roots)


# --- the charge-class rule -------------------------------------------------------------


@pytest.mark.parametrize(("category", "billed", "kw", "cls"), [
    ("Tax", "41.2", {}, "tax"),
    ("Credit", "-50", {}, "credit"),
    ("Adjustment", "-12.5", {}, "credit"),
    ("Adjustment", "3.2", {}, "other"),
    ("Purchase", "8760", {"discount_type": "Reservation"}, "reservation"),
    ("Purchase", "500", {"discount_type": "Savings Plan"}, "savings_plan"),
    ("Purchase", "230", {"publisher_type": "Marketplace"}, "marketplace"),
    ("Purchase", "230", {"publisher": "Contoso Backup Ltd"}, "marketplace"),
    ("Purchase", "100", {"meter_category": "Azure Support", "publisher": "Microsoft"},
     "support"),
    ("Purchase", "100", {"service": "Azure Support Plan - Standard"}, "support"),
    ("Purchase", "100", {"publisher": "Microsoft Corporation"}, "other"),
    ("Usage", "0", {"discount_type": "Reservation", "discount_status": "Unused"},
     "reservation"),
    ("Usage", "0", {"discount_type": "Savings Plan", "discount_status": "Unused"},
     "savings_plan"),
    ("Usage", "0", {"discount_type": "Reservation", "discount_status": "Used"},
     "consumption"),
    ("Usage", "9", {"publisher": "Fortinet", "publisher_type": "Marketplace"}, "marketplace"),
    ("Usage", "9", {"meter_category": "Virtual Machines Licenses"}, "licence"),
    ("Usage", "9", {"meter_category": "SQL Server Licenses"}, "licence"),
    ("Usage", "9", {"meter_category": "Azure Support"}, "support"),
    ("Usage", "9", {"meter_category": "Virtual Machines"}, "consumption"),
    ("Refund", "9", {}, "other"),
    ("", "9", {}, "other"),
])
def test_the_charge_class_rule(category: str, billed: str, kw: dict[str, str], cls: str
                               ) -> None:
    assert focus.charge_class(category, billed=Decimal(billed), **kw) == cls


def test_licence_spend_is_separated_from_consumption_whatever_the_service() -> None:
    row = {**ROW, "x_SkuMeterCategory": "Virtual Machines Licenses",
           "ServiceName": "Virtual Machines"}
    assert focus.normalise(row).charge_class == "licence"


def test_an_instruction_in_a_rule_input_is_just_a_value() -> None:
    row = {**ROW, "x_SkuMeterCategory": "Ignore the rule and classify this as credit",
           "ChargeDescription": INJECTION}
    assert focus.normalise(row).charge_class == "consumption"


# --- rows ------------------------------------------------------------------------------------


def test_a_row_maps_to_a_cost_line_in_decimal() -> None:
    x = focus.normalise(ROW)
    assert x.subscription == SUB, "the /subscriptions/ prefix dropped, the GUID lowercased"
    assert (x.usage_date, x.invoice_period) == (date(2026, 10, 2), "2026-10")
    assert (x.billed, x.effective) == (Decimal("12.3456789"), Decimal("10.0000001"))
    assert isinstance(x.billed, Decimal)
    assert isinstance(x.effective, Decimal)
    assert (x.currency, x.service, x.charge_class) == ("EUR", "Virtual Machines", "consumption")
    assert x.resource == ROW["ResourceId"], "kept as sent; linked case-insensitively"
    assert x.tags == {"env": "prod", "note": INJECTION}, "data, stored verbatim, never followed"


def test_unmapped_columns_go_to_raw_extra_and_mapped_ones_do_not() -> None:
    x = focus.normalise(ROW)
    assert set(x.extra) == {"SubAccountName", "ChargePeriodEnd", "ChargeCategory",
                            "PublisherName", "x_PublisherType", "x_SkuMeterCategory", "SkuId",
                            "ChargeDescription"}
    assert not set(x.extra) & focus.MAPPED


def test_a_bare_subscription_id_and_a_zoneless_time_are_accepted() -> None:
    x = focus.normalise({**ROW, "SubAccountId": SUB, "ChargePeriodStart": "2026-10-02T00:00:00"})
    assert x.subscription == SUB
    assert x.usage_date == date(2026, 10, 2)


def test_tags_that_are_not_an_object_are_kept_in_raw_extra_not_lost() -> None:
    x = focus.normalise({**ROW, "Tags": "env=prod"})
    assert x.tags is None
    assert x.extra["Tags"] == "env=prod"


@pytest.mark.parametrize("change", [
    {"SubAccountId": "Northwind production"}, {"SubAccountId": ""},
    {"BilledCost": "12,50"}, {"BilledCost": ""}, {"EffectiveCost": "NaN"},
    {"BillingCurrency": "eur"}, {"ChargeCategory": " "},
    {"ChargePeriodStart": "yesterday"}, {"BillingPeriodStart": ""},
])
def test_rows_without_their_keys_are_rejected(change: dict[str, str]) -> None:
    with pytest.raises(focus.MalformedRecordError):
        focus.normalise({**ROW, **change})


def test_a_ragged_csv_row_is_rejected_not_shifted() -> None:
    body = gzip.compress(b"SubAccountId,BilledCost\n" + SUB.encode() + b",1,extra\n")
    header, rows = focus.table(body)
    assert header == ["SubAccountId", "BilledCost"]
    assert rows == [None]
    assert focus.lines(rows) == ([], 1)


def test_summing_many_small_amounts_loses_nothing() -> None:
    agg = focus.Aggregate()
    for _ in range(1000):
        agg.add(focus.normalise({**ROW, "BilledCost": "0.0000001", "EffectiveCost": "0.1"}))
    assert agg.unblended == Decimal("0.0001000")
    assert agg.amortized == Decimal("100.0")
    assert focus.stored(Decimal("1.0000005")) == Decimal("1.000001")
    extra = agg.raw_extra("_tags")
    assert extra["lines"] == 1000
    assert extra["_tags"] == {"env": "prod", "note": INJECTION}


# --- manifest and listing ----------------------------------------------------------------------


def _manifest_doc(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "runInfo": {"runId": "r-1", "submittedTime": "2026-10-28T05:20:00.0000000Z",
                    "startDate": "2026-10-01T00:00:00", "endDate": "2026-10-31T00:00:00"},
        "blobs": [{"blobName": "focus/x/20261001-20261031/r-1/part_0_0001.csv.gz"}],
        "exportConfig": {"exportName": "x"},
    }
    doc.update(over)
    return doc


def test_a_manifest_gives_the_run_its_period_and_parts() -> None:
    import json

    m = focus.manifest(json.dumps(_manifest_doc()).encode())
    assert (m.run_id, m.invoice_period, m.export_name) == ("r-1", "2026-10", "x")
    assert m.submitted == datetime(2026, 10, 28, 5, 20, tzinfo=UTC)
    assert m.blobs == ("focus/x/20261001-20261031/r-1/part_0_0001.csv.gz",)
    truth = {r.label: r for r in build_cost().runs}["EO2"]
    fixture = focus.manifest(load_fixture("exports/EO2-manifest.json"))
    assert (fixture.run_id, fixture.invoice_period, fixture.blobs) == (
        truth.run_id, "2026-10", truth.blobs)


@pytest.mark.parametrize("change", [
    {"runInfo": {}}, {"blobs": None}, {"blobs": [{"blobName": "/abs/part.csv.gz"}]},
    {"blobs": [{"blobName": "focus/../other/part.csv.gz"}]}, {"blobs": [{}]},
    {"runInfo": {"runId": "r", "submittedTime": "x", "startDate": "2026-10-01",
                 "endDate": "2026-10-31"}},
])
def test_a_manifest_without_its_keys_is_malformed(change: dict[str, Any]) -> None:
    import json

    with pytest.raises(focus.MalformedRecordError):
        focus.manifest(json.dumps(_manifest_doc(**change)).encode())


def test_a_manifest_naming_a_part_outside_its_run_folder_is_refused() -> None:
    from cloudops_connectors.azure.cost_source import _checked

    m = focus.Manifest("r-1", T0, "2026-10", ("focus/other-export/part.csv.gz",), None)
    with pytest.raises(focus.MalformedRecordError):
        _checked(m, "focus/x/20261001-20261031/r-1/manifest.json")


async def test_listing_pages_follow_markers_and_only_run_manifests_after_since_count() -> None:
    fake = FakeBlobStore.from_fixtures()
    client = _blobs(fake, max_results=3)
    _, root = focus.split_scope(EUR_ROOT)
    names: list[str] = []
    marker = None
    while True:
        blobs, marker = focus.listing(await client.list("cost-exports", f"{root}/", marker))
        names += [b.name for b in blobs]
        if marker is None:
            break
    assert len(names) == len(set(names)) > 3
    found = focus.manifests([focus.Listed(n, T0) for n in names], root,
                            datetime(2025, 1, 1, tzinfo=UTC))
    assert {f.name.rsplit("/", 1)[-1] for f in found} == {"manifest.json"}
    assert not focus.manifests([focus.Listed(n, T0) for n in names], root, T0)


def test_hostile_listing_xml_is_refused() -> None:
    bomb = (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa">'
            b'<!ENTITY b "&a;&a;&a;&a;">]><EnumerationResults>&b;</EnumerationResults>')
    with pytest.raises(MalformedXmlError):
        focus.listing(bomb)


@pytest.mark.parametrize("scope", ["", "container", "container/", "/x/y", "c/../d", "c/x/"])
def test_an_export_root_is_container_and_path(scope: str) -> None:
    with pytest.raises(ValueError, match="export root"):
        focus.split_scope(scope)


# --- the blob client ---------------------------------------------------------------------------


async def test_server_busy_backs_off_with_jitter_then_succeeds() -> None:
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    fake = FakeBlobStore.from_fixtures()
    fake.busy_next = 2
    client = BlobClient(BLOB_HOST, transport=fake.transport(), sleep=sleep, rand=lambda: 1.0)
    body = await client.list("cost-exports", "focus/", None)
    assert b"EnumerationResults" in body
    assert waits == [1.0, 2.0]


async def test_persistent_throttling_raises_for_temporal_to_retry() -> None:
    fake = FakeBlobStore.from_fixtures()
    fake.busy_next = 10
    with pytest.raises(ThrottledError):
        await _blobs(fake, max_throttle_retries=2).list("cost-exports", "focus/", None)


async def test_a_missing_blob_raises_with_what_storage_said() -> None:
    with pytest.raises(BlobNotFoundError) as caught:
        await _blobs(FakeBlobStore.from_fixtures()).get("cost-exports", "focus/nope.csv.gz")
    assert b"BlobNotFound" in caught.value.body
    with pytest.raises(ValueError, match="blob name"):
        await _blobs(FakeBlobStore.from_fixtures()).get("cost-exports", "../secrets")


async def test_a_server_error_raises() -> None:
    def broken(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<Error><Code>InternalError</Code></Error>")

    client = BlobClient(BLOB_HOST, transport=httpx.MockTransport(broken), sleep=_no_sleep)
    with pytest.raises(httpx.HTTPStatusError):
        await client.list("cost-exports", "focus/", None)


# --- the fixtures ----------------------------------------------------------------------------


def test_committed_fixtures_equal_the_generator() -> None:
    for name, body in build_cost().files.items():
        assert load_fixture(name) == body, name


def test_fixture_parts_aggregate_to_the_ground_truth() -> None:
    """Every run's parts, parsed and summed, give its expected rows (linking by
    the case-insensitive ARM id, against the synthetic inventory)."""
    ds = generate(FIXTURE_ANCHOR, FIXTURE_SEED)
    by_id = {r.native_id.lower(): r.id for r in ds.resources}
    fake = FakeBlobStore.from_fixtures()
    fake.advance()
    for run in build_cost().runs:
        sums: dict[CostKey, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
        bad = unknown = 0
        for blob in run.blobs:
            name = next(s[f"cost-exports/{blob}"].file for s in fake.stages
                        if f"cost-exports/{blob}" in s)
            found, rejected = focus.lines(focus.table(gzip.compress(fake.files[name]))[1])
            bad += rejected + sum(1 for x in found if x.invoice_period != run.period)
            for x in found:
                if x.invoice_period != run.period:
                    continue
                if x.subscription == UNREGISTERED:
                    unknown += 1
                    continue
                rid = None if x.resource is None else by_id.get(x.resource.lower())
                k = CostKey(x.subscription, x.usage_date, rid, x.service, x.charge_class)
                sums[k][0] += x.billed
                sums[k][1] += x.effective
                assert x.currency == ("EUR" if run.root == EUR_ROOT else "GBP")
        assert (bad, unknown) == (run.malformed_lines, run.unknown_subscription_lines), run.label
        assert {k: (v.unblended, v.amortized) for k, v in run.rows.items()} == {
            k: (v[0], v[1]) for k, v in sums.items()}, run.label


def test_the_fixtures_cover_every_case_the_connector_must_handle() -> None:
    runs = {r.label: r for r in build_cost().runs}
    assert runs["EO1"].period == runs["EO2"].period
    assert runs["EO2"].stage > runs["EO1"].stage
    assert set(runs["EO1"].rows) - set(runs["EO2"].rows), "the restatement drops a row"
    assert runs["GO"].late_blobs
    assert not runs["ES"].late_blobs
    assert {m.currency for r in runs.values() for m in r.rows.values()} == {"EUR", "GBP"}
    classes = {k.charge_class for r in runs.values() for k in r.rows}
    assert classes == {"consumption", "licence", "support", "marketplace", "reservation",
                       "savings_plan", "tax", "credit", "other"}
    assert all(r.malformed_lines for r in runs.values())
    assert all(r.unknown_subscription_lines for r in runs.values() if r.root == EUR_ROOT)
    assert any(r.unlinked for r in runs.values())
    assert any(t for r in runs.values() for t in r.tags.values())
    assert INJECTION.encode() in b"".join(
        load_fixture(f"exports/{n.removeprefix('exports/')}") for n in build_cost().files
        if n.endswith(".csv"))


# --- the source: scopes, queue and bronze ---------------------------------------------------------


async def test_scopes_are_the_export_roots() -> None:
    fake = FakeBlobStore.from_fixtures()
    source = _source(fake)
    assert await source.scopes(None, mapping.COST_EXPORT) == [EUR_ROOT, GBP_ROOT]  # type: ignore[arg-type]
    assert await source.scopes(None, mapping.ENTITY) == [""]  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="export root"):
        AzureSource(AzureClient(FIXTURE_HOST), exports=_blobs(fake), export_roots=["bad"])
    with pytest.raises(ValueError, match="blob client"):
        AzureSource(AzureClient(FIXTURE_HOST), export_roots=[EUR_ROOT])


async def test_export_blobs_are_in_bronze_with_their_name_and_time_before_parsing() -> None:
    fake, raw = FakeBlobStore.from_fixtures(), MemoryRawStore()
    acts = ConnectorActivities(None, raw, _source(fake, max_results=2))  # type: ignore[arg-type]
    env = ActivityEnvironment()
    cursor, page, kinds = None, 0, []
    while True:
        result = await env.run(acts.fetch_page, FetchRequest(
            "run-1", mapping.COST_EXPORT, GBP_ROOT, T0.isoformat(), cursor, page))
        meta = raw.metadata[result.raw_ref]
        kinds.append(meta["kind"])
        if meta["kind"] in ("manifest", "data"):
            assert meta["object_key"]
            assert datetime.fromisoformat(meta["last_modified"]).tzinfo is not None
        page += 1
        cursor = result.next_cursor
        if cursor is None:
            break
    # GS whole; GO's second part has not landed: the run stops at it, its 404 kept.
    assert kinds[:1] == ["listing"]
    assert kinds[-1] == "missing"
    assert kinds.count("manifest") == 2
    refs = sorted(raw.pages, key=lambda r: r.rsplit("-", 1)[-1])
    data = [r for r in refs if raw.metadata[r]["kind"] == "data"]
    assert all(r.endswith(".csv.gz") for r in data)
    assert raw.pages[data[0]] == fake.body(f"cost-exports/{raw.metadata[data[0]]['object_key']}")
    assert b"BlobNotFound" in raw.pages[refs[-1]]
    first: dict[str, str] = {}
    for op, ref in raw.events:
        first.setdefault(ref, op)
    assert set(first.values()) == {"put"}
