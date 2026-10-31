"""Contract tests for the AWS connector (Config and CUR) against recorded
fixtures. No database, no Temporal server: mapping, Decimal parsing, the charge
class table, the clients' paging and throttling backoff, the S3 queue, bronze
ordering, and the fixtures themselves."""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from cloudops_connectors.aws import mapping
from cloudops_connectors.aws.client import ConfigClient, NoSuchKeyError, S3Client, ThrottledError
from cloudops_connectors.aws.fake import (
    AGGREGATOR,
    CONFIG_HOST,
    REGION,
    S3_HOST,
    FakeConfig,
    FakeS3,
    load_fixture,
)
from cloudops_connectors.aws.source import AwsSource
from cloudops_connectors.core.ingest import ConnectorActivities, FetchRequest
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.core.xmlrec import MalformedXmlError
from cloudops_devdata.aws_fixtures import (
    EUR_REPORT,
    INJECTION,
    UNKNOWN_RESOURCE,
    UNREGISTERED,
    USD_REPORT,
    CostKey,
    build,
)
from cloudops_devdata.servicenow_fixtures import FIXTURE_ANCHOR, FIXTURE_SEED
from cloudops_devdata.synthetic import generate

T0 = datetime(2026, 1, 1, tzinfo=UTC)

ITEM: dict[str, Any] = {
    "accountId": "499836243985", "awsRegion": "eu-west-1",
    "arn": "arn:aws:ec2:eu-west-1:499836243985:instance/i-0abc", "resourceId": "i-0abc",
    "resourceType": "AWS::EC2::Instance", "availabilityZone": "eu-west-1b",
    "configurationItemCaptureTime": "2026-10-27T06:00:00.000Z",
    "configurationItemStatus": "OK", "resourceCreationTime": "2025-01-02T03:04:05Z",
    "tags": [{"key": "Name", "value": "nwl-weu-web-09"}, {"key": "env", "value": INJECTION}],
    "configuration": {"privateIpAddress": "10.1.2.3", "platformDetails": "Windows",
                      "state": {"code": 16, "name": "running"}, "instanceType": "m5.large"},
}


async def _no_sleep(_: float) -> None:
    return None


def _config(fake: FakeConfig, **kwargs: Any) -> ConfigClient:
    kwargs.setdefault("sleep", _no_sleep)
    return ConfigClient(REGION, AGGREGATOR, base_url=CONFIG_HOST, transport=fake.transport(),
                        **kwargs)


def _s3(fake: FakeS3, **kwargs: Any) -> S3Client:
    kwargs.setdefault("sleep", _no_sleep)
    return S3Client(fake.bucket, REGION, base_url=S3_HOST, transport=fake.transport(), **kwargs)


# --- Config mapping -------------------------------------------------------------------


def test_a_config_item_maps_to_a_resource() -> None:
    i = mapping.normalise_config(ITEM)
    assert (i.account, i.arn, i.resource_id) == (
        "499836243985", ITEM["arn"], "i-0abc")
    assert i.name == "nwl-weu-web-09", "EC2 has no resourceName: the Name tag names it"
    assert (i.type_native, i.type_canonical) == ("AWS::EC2::Instance", "compute.vm")
    assert (i.ip, i.operating_system, i.state) == ("10.1.2.3", "Windows", "running")
    assert i.captured_at == datetime(2026, 10, 27, 6, tzinfo=UTC)
    assert i.created_at == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert not i.deleted
    assert i.tags["env"] == INJECTION  # data, stored verbatim, never followed


def test_unmapped_fields_are_kept_in_raw_extra() -> None:
    i = mapping.normalise_config(ITEM)
    assert i.raw_extra == {"availabilityZone": "eu-west-1b",
                           "configuration": {"instanceType": "m5.large"}}


def test_name_falls_back_to_resource_name_then_id() -> None:
    assert mapping.normalise_config({**ITEM, "resourceName": "web"}).name == "web"
    assert mapping.normalise_config({**ITEM, "tags": []}).name == "i-0abc"


@pytest.mark.parametrize("status", ["ResourceDeleted", "ResourceDeletedNotRecorded"])
def test_a_deleted_item_is_deleted_at_its_capture_time(status: str) -> None:
    i = mapping.normalise_config({**ITEM, "configurationItemStatus": status})
    assert i.deleted
    assert i.state == "deleted"


@pytest.mark.parametrize(("rtype", "canonical"), [
    ("AWS::EC2::Instance", "compute.vm"), ("AWS::RDS::DBInstance", "database.sql"),
    ("AWS::S3::Bucket", "storage.account"), ("AWS::Lambda::Function", "compute.function"),
    ("AWS::EC2::SecurityGroup", "other"),
])
def test_resource_types_map_through_a_fixed_table(rtype: str, canonical: str) -> None:
    assert mapping.normalise_config({**ITEM, "resourceType": rtype}).type_canonical == canonical


def test_the_synthetic_types_are_all_mapped() -> None:
    ds = generate(FIXTURE_ANCHOR, FIXTURE_SEED)
    for r in ds.resources:
        if r.type_native.startswith("AWS::"):
            assert mapping.TYPE_CANONICAL[r.type_native] == r.type_canonical


@pytest.mark.parametrize("change", [
    {"accountId": "4998"}, {"accountId": None}, {"arn": ""}, {"resourceType": None},
    {"resourceId": " "}, {"configurationItemCaptureTime": "2026-10-27T06:00:00"},
    {"configurationItemCaptureTime": "last tuesday"},
])
def test_items_without_their_keys_are_malformed(change: dict[str, Any]) -> None:
    with pytest.raises(mapping.MalformedRecordError):
        mapping.normalise_config({**ITEM, **change})


def test_results_that_are_not_json_objects_are_counted() -> None:
    records, bad = mapping.config_records([json.dumps(ITEM), "{not json", "[1]"])
    assert len(records) == 1
    assert bad == 2


def test_the_expression_selects_by_capture_time() -> None:
    sql = mapping.expression(datetime(2026, 10, 27, 5, 55, tzinfo=UTC))
    assert sql.endswith("WHERE configurationItemCaptureTime >= '2026-10-27T05:55:00.000Z'")
    assert "configuration.privateIpAddress" in sql
    with pytest.raises(ValueError, match="timezone"):
        mapping.expression(datetime(2026, 10, 27))


# --- Config client: paging and throttling --------------------------------------------


async def test_pages_follow_next_token_until_the_last() -> None:
    fake = FakeConfig.from_fixtures()
    client = _config(fake, limit=4)
    seen: list[str] = []
    token = None
    while True:
        results, token = mapping.config_page(await client.select(T0, token))
        seen.extend(results)
        if token is None:
            break
    assert seen == fake.results, "every result once, in the order served"
    assert len(fake.requests) == -(-len(fake.results) // 4)
    first = json.loads(fake.requests[0].content)
    assert fake.requests[0].headers["X-Amz-Target"] == (
        "StarlingDoveService.SelectAggregateResourceConfig")
    assert first["ConfigurationAggregatorName"] == AGGREGATOR
    assert first["Limit"] == 4
    assert "NextToken" not in first
    assert "NextToken" in json.loads(fake.requests[1].content)


async def test_since_filters_by_capture_time() -> None:
    fake = FakeConfig.from_fixtures()
    client = _config(fake, limit=100)
    late = datetime(2026, 10, 27, 10, tzinfo=UTC)
    results, _ = mapping.config_page(await client.select(late, None))
    records, _ = mapping.config_records(results)
    assert 0 < len(records) < len(fake.results)
    assert all(t >= late for t in mapping.capture_times(records))


async def test_throttling_backs_off_exponentially_with_jitter_then_succeeds() -> None:
    fake = FakeConfig.from_fixtures()
    fake.throttle_next = 3
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    client = _config(fake, sleep=sleep, rand=lambda: 0.5, backoff_base=2.0)
    assert mapping.config_page(await client.select(T0, None))[0]
    # Half fixed, half jitter: 2*2^n * (0.5 + 0.5*0.5).
    assert waits == [1.5, 3.0, 6.0]
    assert len(fake.requests) == 4


async def test_backoff_is_capped() -> None:
    fake = FakeConfig.from_fixtures()
    client = _config(fake, rand=lambda: 1.0, backoff_base=1.0, backoff_cap=5.0)
    assert [client.backoff.delay(n) for n in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]


async def test_persistent_throttling_raises_for_temporal_to_retry() -> None:
    fake = FakeConfig.from_fixtures()
    fake.throttle_next = 99
    client = _config(fake, max_throttle_retries=2)
    with pytest.raises(ThrottledError):
        await client.select(T0, None)
    assert len(fake.requests) == 3


async def test_a_server_error_raises() -> None:
    fake = FakeConfig.from_fixtures()
    fake.fail_next = 1
    with pytest.raises(httpx.HTTPStatusError):
        await _config(fake).select(T0, None)


# --- S3: listing, manifests and the queue ----------------------------------------------


async def test_listing_pages_follow_continuation_tokens() -> None:
    fake = FakeS3.from_fixtures()
    client = _s3(fake, max_keys=3)
    keys: list[str] = []
    token = None
    while True:
        objects, token = mapping.listing(await client.list(f"{USD_REPORT}/", token))
        keys.extend(o.key for o in objects)
        if token is None:
            break
    assert keys == sorted(k for k in fake.objects if k.startswith(f"{USD_REPORT}/"))
    assert len(fake.requests) > 1


async def test_only_period_manifests_after_since_are_queued_oldest_first() -> None:
    fake = FakeS3.from_fixtures()
    objects, _ = mapping.listing(await _s3(fake).list(f"{USD_REPORT}/", None))
    every = mapping.manifests(objects, USD_REPORT, T0)
    assert [o.key.split("/")[3] for o in every] == ["20260901-20261001", "20261001-20261101"]
    assert all(o.key.count("/") == 4 for o in every), "not the per-assembly copies"
    later = mapping.manifests(objects, USD_REPORT, every[0].last_modified)
    assert later == every[1:]


def test_hostile_listing_xml_is_refused() -> None:
    bomb = (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa">'
            b'<!ENTITY b "&a;&a;&a;&a;">]><ListBucketResult>&b;</ListBucketResult>')
    with pytest.raises(MalformedXmlError):
        mapping.listing(bomb)


def test_a_manifest_gives_the_assembly_period_and_files() -> None:
    m = mapping.manifest(load_fixture("cur/S1-Manifest.json"))
    truth = {a.label: a for a in build().cur}["S1"]
    assert (m.assembly_id, m.invoice_period, m.report_keys) == (
        truth.assembly_id, "2026-09", truth.report_keys)
    with pytest.raises(mapping.MalformedRecordError):
        mapping.manifest(b'{"assemblyId": "x", "reportKeys": []}')


async def test_a_missing_object_raises_with_what_s3_said() -> None:
    fake = FakeS3.from_fixtures()
    with pytest.raises(NoSuchKeyError) as caught:
        await _s3(fake).get(f"{EUR_REPORT}/nope.csv.gz")
    assert b"NoSuchKey" in caught.value.body


async def test_s3_slowdown_backs_off() -> None:
    fake = FakeS3.from_fixtures()
    fake.throttle_next = 2
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    key = next(k for k in fake.objects if k.endswith(".csv.gz"))
    obj = await _s3(fake, sleep=sleep, rand=lambda: 0.0).get(key)
    assert waits == [0.5, 1.0]
    assert obj.last_modified == fake.objects[key].last_modified
    assert gzip.decompress(obj.body) == fake.files[fake.objects[key].file]


# --- CUR lines -----------------------------------------------------------------------------


@pytest.mark.parametrize(("line_type", "code", "entity", "cls"), [
    ("Usage", "AmazonEC2", "AWS", "consumption"),
    ("DiscountedUsage", "AmazonEC2", "AWS", "consumption"),
    ("SavingsPlanCoveredUsage", "AmazonEC2", "AWS", "consumption"),
    ("Tax", "AmazonEC2", "AWS", "tax"),
    ("Tax", "prodview-x", "AWS Marketplace", "tax"),
    ("Credit", "AmazonEC2", "AWS", "credit"),
    ("Refund", "AmazonEC2", "AWS", "credit"),
    ("BundledDiscount", "AmazonEC2", "AWS", "credit"),
    ("EdpDiscount", "AmazonEC2", "AWS", "credit"),
    ("RIFee", "AmazonEC2", "AWS", "reservation"),
    ("SavingsPlanRecurringFee", "ComputeSavingsPlans", "AWS", "savings_plan"),
    ("SavingsPlanUpfrontFee", "ComputeSavingsPlans", "AWS", "savings_plan"),
    ("SavingsPlanNegation", "ComputeSavingsPlans", "AWS", "savings_plan"),
    ("Fee", "AWSSupportBusiness", "AWS", "support"),
    ("Fee", "AWSSupportEnterprise", "AWS", "support"),
    ("Fee", "AmazonRoute53", "AWS", "other"),
    ("Usage", "prodview-x", "AWS Marketplace", "marketplace"),
    ("Fee", "prodview-x", "AWS Marketplace", "marketplace"),
    ("VolumeDiscount", "AmazonEC2", "AWS", "other"),
])
def test_the_charge_class_table(line_type: str, code: str, entity: str, cls: str) -> None:
    assert mapping.charge_class(line_type, code, entity) == cls


LINE = {
    "lineItem/UsageAccountId": "742218507629", "lineItem/LineItemType": "Usage",
    "lineItem/UsageStartDate": "2026-10-02T00:00:00Z", "lineItem/ProductCode": "AmazonEC2",
    "lineItem/UnblendedCost": "0.1234567891", "lineItem/CurrencyCode": "USD",
    "lineItem/ResourceId": "i-0abc", "product/ProductName": "Amazon EC2",
    "bill/BillingEntity": "AWS", "reservation/EffectiveCost": "",
    "savingsPlan/SavingsPlanEffectiveCost": "", "resourceTags/user:Team": "ops",
    "lineItem/LineItemDescription": INJECTION, "lineItem/UsageAmount": "",
}


def test_amounts_are_decimal_at_full_precision_never_float() -> None:
    line = mapping.normalise_line(LINE)
    assert isinstance(line.unblended, Decimal)
    assert isinstance(line.amortized, Decimal)
    assert line.unblended == Decimal("0.1234567891")
    assert mapping.stored(line.unblended) == Decimal("0.123457")
    assert (line.account, line.usage_date, line.service, line.resource, line.currency) == (
        "742218507629", date(2026, 10, 2), "Amazon EC2", "i-0abc", "USD")
    # Unmapped, non-empty columns are kept; the description is data.
    assert line.extra == {"resourceTags/user:Team": "ops",
                          "lineItem/LineItemDescription": INJECTION}


def test_summing_many_small_lines_loses_nothing() -> None:
    agg = mapping.Aggregate()
    for _ in range(10):
        agg.add(mapping.normalise_line({**LINE, "lineItem/UnblendedCost": "0.1"}))
    assert agg.unblended == Decimal("1.0")
    assert agg.lines == 10
    assert agg.raw_extra()["line_item_types"] == {"Usage": 10}


@pytest.mark.parametrize(("line_type", "column", "expected"), [
    ("DiscountedUsage", "reservation/EffectiveCost", Decimal("0.05")),
    ("SavingsPlanCoveredUsage", "savingsPlan/SavingsPlanEffectiveCost", Decimal("0.07")),
])
def test_amortized_uses_the_effective_cost_for_covered_usage(
    line_type: str, column: str, expected: Decimal
) -> None:
    line = mapping.normalise_line({**LINE, "lineItem/LineItemType": line_type,
                                   column: str(expected)})
    assert line.unblended == Decimal("0.1234567891")
    assert line.amortized == expected


def test_amortized_is_unblended_for_everything_else() -> None:
    line = mapping.normalise_line({**LINE, "lineItem/LineItemType": "RIFee",
                                   "reservation/EffectiveCost": "99"})
    assert line.amortized == line.unblended


@pytest.mark.parametrize("change", [
    {"lineItem/UnblendedCost": "12.5O"}, {"lineItem/UnblendedCost": ""},
    {"lineItem/UnblendedCost": "NaN"}, {"lineItem/UsageAccountId": ""},
    {"lineItem/CurrencyCode": "eur"}, {"lineItem/UsageStartDate": "2026-10-02"},
    {"lineItem/LineItemType": ""},
    {"lineItem/LineItemType": "DiscountedUsage", "reservation/EffectiveCost": ""},
])
def test_malformed_lines_are_rejected(change: dict[str, str]) -> None:
    with pytest.raises(mapping.MalformedRecordError):
        mapping.normalise_line({**LINE, **change})


def test_a_ragged_csv_row_is_rejected_not_shifted() -> None:
    body = gzip.compress(b"lineItem/UsageAccountId,lineItem/UnblendedCost\n"
                         b"742218507629,1.00\n742218507629,1.00,extra\n")
    header, rows = mapping.cur_table(body)
    assert header == ["lineItem/UsageAccountId", "lineItem/UnblendedCost"]
    assert rows[1] is None
    assert mapping.lines(rows)[1] == 2  # the first lacks its other keys


def test_resource_ids_link_on_their_last_segment() -> None:
    assert mapping.resource_tail("arn:aws:ec2:eu-west-1:1:instance/i-0abc") == "i-0abc"
    assert mapping.resource_tail("arn:aws:rds:eu-west-1:1:db:orders") == "orders"
    assert mapping.resource_tail("my-bucket") == "my-bucket"


# --- the fixtures -------------------------------------------------------------------------


def test_committed_fixtures_equal_the_generator() -> None:
    for name, body in build().files.items():
        assert load_fixture(name) == body, name


def test_fixture_cur_lines_aggregate_to_the_ground_truth() -> None:
    """Every assembly's files, parsed and summed, give its expected rows (resource
    linking aside: that needs the inventory and is checked against the database)."""
    ds = generate(FIXTURE_ANCHOR, FIXTURE_SEED)
    tail = {(r.native_id.split(":")[4], mapping.resource_tail(r.native_id)): r.id
            for r in ds.resources if r.native_id.startswith("arn:")}
    fake = FakeS3.from_fixtures()
    fake.advance()
    for a in build().cur:
        sums: dict[CostKey, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
        bad = unknown = 0
        for key in a.report_keys:
            name = next(s[key].file for s in fake.stages if key in s)
            body = gzip.compress(fake.files[name], mtime=0)
            found, rejected = mapping.lines(mapping.cur_table(body)[1])
            bad += rejected
            for x in found:
                if x.account == UNREGISTERED:
                    unknown += 1
                    continue
                rid = None if x.resource is None else tail.get(
                    (x.account, mapping.resource_tail(x.resource)))
                k = CostKey(x.account, x.usage_date, rid, x.service, x.charge_class)
                sums[k][0] += x.unblended
                sums[k][1] += x.amortized
                assert x.currency == ("EUR" if a.report == EUR_REPORT else "USD")
        assert (bad, unknown) == (a.malformed_lines, a.unknown_account_lines), a.label
        assert {k: (v.unblended, v.amortized) for k, v in a.rows.items()} == {
            k: (v[0], v[1]) for k, v in sums.items()}, a.label


def test_the_fixtures_cover_every_case_the_connector_must_handle() -> None:
    f = build()
    assert any(e.tenant_code is None and not e.malformed for e in f.config)  # quarantine
    assert sum(e.malformed for e in f.config) == 2
    assert any(e.deleted for e in f.config)
    assert any(e.resource_id is None and e.tenant_code and not e.malformed for e in f.config)
    assert any(e.type_canonical == "other" for e in f.config)
    cur = {a.label: a for a in f.cur}
    assert cur["O1"].period == cur["O2"].period
    assert cur["O2"].stage > cur["O1"].stage
    assert set(cur["O1"].rows) - set(cur["O2"].rows), "the restatement drops a row"
    assert cur["E1"].late_keys
    assert not cur["S1"].late_keys
    assert {m.currency for a in f.cur for m in a.rows.values()} == {"USD", "EUR"}
    classes = {k.charge_class for a in f.cur for k in a.rows}
    assert classes == {"consumption", "support", "tax", "savings_plan", "credit",
                       "reservation", "marketplace", "other"}
    assert all(a.malformed_lines for a in f.cur)
    assert all(a.unlinked[UNKNOWN_RESOURCE] for a in f.cur if a.report == USD_REPORT)
    assert INJECTION.encode() in load_fixture("config/results.json")


# --- raw before parse ------------------------------------------------------------------------


def _source(config: FakeConfig, s3: FakeS3, **kwargs: Any) -> AwsSource:
    return AwsSource(_config(config, **kwargs), _s3(s3), s3.reports)


async def test_a_config_page_is_in_bronze_exactly_as_sent_before_it_is_parsed() -> None:
    fake, raw = FakeConfig.from_fixtures(), MemoryRawStore()
    acts = ConnectorActivities(None, raw, _source(fake, FakeS3.from_fixtures(),  # type: ignore[arg-type]
                                                  limit=5))
    result = await ActivityEnvironment().run(acts.fetch_page, FetchRequest(
        "run-1", mapping.CONFIG, AGGREGATOR, T0.isoformat(), None, 0))
    assert result.raw_ref.startswith("aws/config/")
    assert result.raw_ref.endswith(
        "run-1-00000.json")
    assert raw.events == [("put", result.raw_ref)]
    again = _config(FakeConfig.from_fixtures(), limit=5)
    assert raw.pages[result.raw_ref] == await again.select(T0, None)
    meta = raw.metadata[result.raw_ref]
    assert meta["source"] == "aws"
    assert meta["sha256"]
    assert meta["scope"] == AGGREGATOR
    assert result.records == 5
    assert result.next_cursor is not None


async def test_cur_objects_are_in_bronze_with_their_key_and_time_before_parsing() -> None:
    s3, raw = FakeS3.from_fixtures(), MemoryRawStore()
    acts = ConnectorActivities(None, raw, _source(FakeConfig.from_fixtures(), s3))  # type: ignore[arg-type]
    env = ActivityEnvironment()
    cursor, page, kinds = None, 0, []
    while True:
        result = await env.run(acts.fetch_page, FetchRequest(
            "run-1", mapping.CUR, EUR_REPORT, T0.isoformat(), cursor, page))
        meta = raw.metadata[result.raw_ref]
        kinds.append(meta["kind"])
        if meta["kind"] != "listing":
            assert meta["object_key"]
        if meta["kind"] in ("manifest", "data"):
            assert datetime.fromisoformat(meta["last_modified"]).tzinfo is not None
        page += 1
        cursor = result.next_cursor
        if cursor is None:
            break
    # E1's second file has not landed: the run stops at it, its 404 kept in bronze.
    assert kinds == ["listing", "manifest", "data", "missing"]
    refs = sorted(raw.pages, key=lambda r: r.rsplit("-", 1)[-1])
    assert refs[2].endswith(".csv.gz")
    assert refs[3].endswith(".xml")
    assert b"NoSuchKey" in raw.pages[refs[3]]
    data_key = raw.metadata[refs[2]]["object_key"]
    assert raw.pages[refs[2]] == s3.body(data_key), "the gzip object exactly as S3 sent it"
    assert raw.metadata[refs[2]]["manifest_ref"] == refs[1]
    # Nothing was read back before it was stored.
    first: dict[str, str] = {}
    for op, ref in raw.events:
        first.setdefault(ref, op)
    assert set(first.values()) == {"put"}
