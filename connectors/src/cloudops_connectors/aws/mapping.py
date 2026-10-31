"""AWS Config items -> ``cloudops.resource``; CUR lines -> ``cloudops.cost_fact``.

**Config.** One aggregator advanced query (``SelectAggregateResourceConfig``)
returns every account's current configuration items as JSON strings. The
customer is the item's ``accountId``: an exact alias of source ``aws``
(``core/identity.py``). ``native_id`` is the item's ``arn``, the key
``resource`` is unique on per account. The synthetic dataset stores AWS
resources under ARNs of the form ``arn:aws:ec2:<region>:<account>:resource/<name>``
and the recorded fixtures report those same ARNs, so Config updates the seeded
rows instead of adding duplicates. An item without an ARN is rejected, never
given an invented one.

``resourceType`` maps to ``type_canonical`` through a fixed table; a type the
table does not know is ``other`` (and stays visible in ``type_native``).
Deleted items (``ResourceDeleted``, ``ResourceDeletedNotRecorded``) set
``deleted_at`` to the capture time.

**CUR.** The legacy Cost and Usage Report: per billing period a manifest
(``<prefix>/<report>/<yyyymmdd-yyyymmdd>/<report>-Manifest.json``) naming the
gzip CSV files of the current *assembly*. Lines are parsed with ``Decimal``
only, never ``float``. ``charge_class`` comes from ``lineItem/LineItemType``
through ``charge_class()``. Amortized cost is a deliberate simplification for
step 16 to refine: ``reservation/EffectiveCost`` for ``DiscountedUsage``,
``savingsPlan/SavingsPlanEffectiveCost`` for ``SavingsPlanCoveredUsage``, the
unblended cost for everything else (so RI and Savings Plan fees are not yet
spread, and a negation line reduces amortized cost as it does unblended).

Every field not mapped is kept in ``raw_extra``. Tags, descriptions and names
are data, never instruction (CLAUDE.md rule 4).
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final
from xml.etree.ElementTree import Element

from cloudops_connectors.core.xmlrec import parse

SOURCE: Final = "aws"
CONFIG: Final = "config"
CUR: Final = "cur"
COST_TOTAL: Final = "cost_total"  # Cost Explorer month to date -> billing_total
INVOICE: Final = "invoice"  # Invoicing summaries -> billing_total
ENTITIES: Final = frozenset({CONFIG, CUR, COST_TOTAL, INVOICE})
MONEY: Final = Decimal("0.000001")  # numeric(18,6)
ACCOUNT_ID: Final = re.compile(r"\d{12}")
CURRENCY: Final = re.compile(r"[A-Z]{3}")

# --- Config -------------------------------------------------------------------------

# Selected by the advanced query. ``availabilityZone`` is selected but not mapped:
# it lands in raw_extra.
CONFIG_FIELDS: Final = (
    "accountId", "awsRegion", "arn", "resourceId", "resourceName", "resourceType",
    "availabilityZone", "resourceCreationTime", "configurationItemCaptureTime",
    "configurationItemStatus", "tags", "configuration.privateIpAddress",
    "configuration.platformDetails", "configuration.state.name",
)
CONFIG_MAPPED: Final = frozenset({
    "accountId", "awsRegion", "arn", "resourceId", "resourceName", "resourceType",
    "resourceCreationTime", "configurationItemCaptureTime", "configurationItemStatus",
    "tags", "configuration",
})
CONFIGURATION_MAPPED: Final = frozenset({"privateIpAddress", "platformDetails", "state"})
# Present on every item Config reports: a page without them has drifted.
CONFIG_EXPECTED: Final = frozenset({
    "accountId", "arn", "resourceId", "resourceType", "configurationItemCaptureTime",
    "configurationItemStatus",
})
DELETED_STATUSES: Final = frozenset({"ResourceDeleted", "ResourceDeletedNotRecorded"})

# resourceType -> type_canonical. Consistent with the synthetic dataset; the
# rest of the catalogue is mapped when someone needs it.
TYPE_CANONICAL: Final = {
    "AWS::EC2::Instance": "compute.vm",
    "AWS::RDS::DBInstance": "database.sql",
    "AWS::RDS::DBCluster": "database.sql",
    "AWS::S3::Bucket": "storage.account",
    "AWS::Lambda::Function": "compute.function",
    "AWS::EC2::Volume": "storage.disk",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "network.load_balancer",
    "AWS::ElasticLoadBalancing::LoadBalancer": "network.load_balancer",
}
UNMAPPED_TYPE: Final = "other"


class MalformedRecordError(ValueError):
    """An item or line without the fields that key it. Kept in bronze, counted,
    skipped."""


@dataclass(frozen=True)
class ConfigItem:
    account: str
    region: str | None
    arn: str
    resource_id: str
    name: str
    type_native: str
    type_canonical: str
    captured_at: datetime
    created_at: datetime | None
    deleted: bool
    tags: dict[str, str]
    ip: str | None
    operating_system: str | None
    state: str | None
    raw_extra: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _time(value: Any, name: str, *, required: bool = False) -> datetime | None:
    text = _text(value)
    if text is None:
        if required:
            raise MalformedRecordError(f"{name} is required")
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedRecordError(f"{name} is not an ISO date-time") from None
    if moment.tzinfo is None:
        raise MalformedRecordError(f"{name} has no time zone")
    return moment.astimezone(UTC)


def _required(value: Any, name: str) -> datetime:
    moment = _time(value, name, required=True)
    if moment is None:  # pragma: no cover - _time raises first
        raise MalformedRecordError(f"{name} is required")
    return moment


def since_literal(since: datetime) -> str:
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    return since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def expression(since: datetime) -> str:
    """The advanced query. ``since`` is ours (a datetime), never source text."""
    return (f"SELECT {', '.join(CONFIG_FIELDS)}"
            f" WHERE configurationItemCaptureTime >= '{since_literal(since)}'")


def config_page(body: bytes) -> tuple[list[str], str | None]:
    """The page's result strings and the next token. Raises on a body that is
    not the documented shape: the page dead-letters, its bronze copy kept."""
    doc = json.loads(body)
    if not isinstance(doc, Mapping) or not isinstance(doc.get("Results", []), list):
        raise ValueError("not a SelectAggregateResourceConfig response")
    results = [r for r in doc.get("Results", []) if isinstance(r, str)]
    token = doc.get("NextToken")
    return results, token if isinstance(token, str) and token else None


def config_records(results: Iterable[str]) -> tuple[list[dict[str, Any]], int]:
    """Results decoded; strings that are not JSON objects are counted."""
    out: list[dict[str, Any]] = []
    bad = 0
    for r in results:
        try:
            item = json.loads(r)
        except ValueError:
            bad += 1
            continue
        if isinstance(item, dict):
            out.append(item)
        else:
            bad += 1
    return out, bad


def _tags(value: Any) -> dict[str, str]:
    """Config sends tags as ``[{key, value}]``; a mapping is accepted too."""
    if isinstance(value, Mapping):
        return {str(k): str(v) for k, v in value.items()}
    out: dict[str, str] = {}
    if isinstance(value, list):
        for t in value:
            if isinstance(t, Mapping) and isinstance(t.get("key"), str):
                v = t.get("value")
                out[t["key"]] = "" if v is None else str(v)
    return out


def normalise_config(item: Mapping[str, Any]) -> ConfigItem:
    account = _text(item.get("accountId"))
    if account is None or not ACCOUNT_ID.fullmatch(account):
        raise MalformedRecordError("accountId must be twelve digits")
    arn, resource_id = _text(item.get("arn")), _text(item.get("resourceId"))
    type_native = _text(item.get("resourceType"))
    if arn is None or resource_id is None or type_native is None:
        raise MalformedRecordError("arn, resourceId and resourceType are required")
    captured = _required(item.get("configurationItemCaptureTime"),
                         "configurationItemCaptureTime")
    status = _text(item.get("configurationItemStatus")) or ""
    tags = _tags(item.get("tags"))
    conf = item.get("configuration")
    conf = conf if isinstance(conf, Mapping) else {}
    state = conf.get("state")
    deleted = status in DELETED_STATUSES
    extra: dict[str, Any] = {k: v for k, v in item.items() if k not in CONFIG_MAPPED}
    conf_extra = {k: v for k, v in conf.items() if k not in CONFIGURATION_MAPPED}
    if conf_extra:
        extra["configuration"] = conf_extra
    return ConfigItem(
        account=account,
        region=_text(item.get("awsRegion")),
        arn=arn,
        resource_id=resource_id,
        name=_text(item.get("resourceName")) or _text(tags.get("Name")) or resource_id,
        type_native=type_native,
        type_canonical=TYPE_CANONICAL.get(type_native, UNMAPPED_TYPE),
        captured_at=captured,
        created_at=_time(item.get("resourceCreationTime"), "resourceCreationTime"),
        deleted=deleted,
        tags=tags,
        ip=_text(conf.get("privateIpAddress")),
        operating_system=_text(conf.get("platformDetails")),
        state="deleted" if deleted else _text(
            state.get("name") if isinstance(state, Mapping) else None),
        raw_extra=extra,
    )


def capture_times(items: Iterable[Mapping[str, Any]]) -> list[datetime]:
    """Every parseable capture time, malformed items included, for the watermark."""
    out = []
    for item in items:
        try:
            t = _time(item.get("configurationItemCaptureTime"), "configurationItemCaptureTime")
        except MalformedRecordError:
            continue
        if t is not None:
            out.append(t)
    return out


# --- CUR: S3 listing and the manifest ---------------------------------------------


@dataclass(frozen=True)
class S3Object:
    key: str
    last_modified: datetime


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: Element, name: str) -> str | None:
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return None


def listing(body: bytes) -> tuple[list[S3Object], str | None]:
    """A ListObjectsV2 page (namespaced XML, parsed safely): the objects and the
    continuation token when the listing is truncated."""
    root = parse(body)
    objects = []
    for el in root:
        if _local(el.tag) != "Contents":
            continue
        key, modified = _child(el, "Key"), _child(el, "LastModified")
        if not key or not modified:
            continue
        try:
            moment = _required(modified, "LastModified")
        except MalformedRecordError:
            continue
        objects.append(S3Object(key, moment))
    truncated = (_child(root, "IsTruncated") or "").lower() == "true"
    token = _child(root, "NextContinuationToken") if truncated else None
    return objects, token or None


def manifest_pattern(scope: str) -> re.Pattern[str]:
    """``scope`` is ``<prefix>/<report>``; the period-level manifest only (the
    per-assembly copies beside the data files are not read)."""
    report = scope.rsplit("/", 1)[-1]
    return re.compile(re.escape(scope) + r"/\d{8}-\d{8}/" + re.escape(report)
                      + r"-Manifest\.json")


def manifests(objects: Iterable[S3Object], scope: str, since: datetime) -> list[S3Object]:
    """Manifests changed after ``since``, oldest first: the order they settled."""
    pattern = manifest_pattern(scope)
    found = [o for o in objects if pattern.fullmatch(o.key) and o.last_modified > since]
    return sorted(found, key=lambda o: (o.last_modified, o.key))


@dataclass(frozen=True)
class Manifest:
    assembly_id: str
    invoice_period: str  # YYYY-MM
    report_keys: tuple[str, ...]
    payer: str | None


def manifest(body: bytes) -> Manifest:
    doc = json.loads(body)
    if not isinstance(doc, Mapping):
        raise MalformedRecordError("manifest is not an object")
    assembly = _text(doc.get("assemblyId"))
    period = doc.get("billingPeriod")
    start = _text(period.get("start")) if isinstance(period, Mapping) else None
    keys = doc.get("reportKeys")
    if assembly is None or start is None or not isinstance(keys, list) or not all(
            isinstance(k, str) and k for k in keys):
        raise MalformedRecordError("assemblyId, billingPeriod.start and reportKeys are required")
    if not re.fullmatch(r"\d{8}T\d{6}\.\d{3}Z", start):
        raise MalformedRecordError("billingPeriod.start is not yyyymmddThhmmss.fffZ")
    return Manifest(assembly, f"{start[:4]}-{start[4:6]}", tuple(keys),
                    _text(doc.get("account")))


# --- CUR: lines ------------------------------------------------------------------------

LINE_TYPE_CLASS: Final = {
    "Usage": "consumption",
    "DiscountedUsage": "consumption",
    "SavingsPlanCoveredUsage": "consumption",
    "Tax": "tax",
    "Credit": "credit",
    "Refund": "credit",
    "BundledDiscount": "credit",
    "EdpDiscount": "credit",
    "RIFee": "reservation",
    "SavingsPlanRecurringFee": "savings_plan",
    "SavingsPlanUpfrontFee": "savings_plan",
    "SavingsPlanNegation": "savings_plan",
}
# Classes that stay what they are even when AWS Marketplace bills them.
_ALWAYS: Final = frozenset({"tax", "credit"})
MARKETPLACE: Final = "AWS Marketplace"

CUR_MAPPED: Final = frozenset({
    "identity/LineItemId", "identity/TimeInterval",
    "bill/PayerAccountId", "bill/BillingEntity", "bill/BillingPeriodStartDate",
    "bill/BillingPeriodEndDate",
    "lineItem/UsageAccountId", "lineItem/LineItemType", "lineItem/UsageStartDate",
    "lineItem/UsageEndDate", "lineItem/ProductCode", "lineItem/ResourceId",
    "lineItem/UnblendedCost", "lineItem/CurrencyCode", "product/ProductName",
    "reservation/EffectiveCost", "savingsPlan/SavingsPlanEffectiveCost",
})
CUR_EXPECTED: Final = frozenset({
    "lineItem/UsageAccountId", "lineItem/LineItemType", "lineItem/UsageStartDate",
    "lineItem/UnblendedCost", "lineItem/CurrencyCode",
})
MAX_EXTRA_VALUES: Final = 10


def charge_class(line_type: str, product_code: str, billing_entity: str) -> str:
    """The fixed table (step 16 refines it): tax and credits are themselves
    whoever bills them; otherwise AWS Marketplace is marketplace; a ``Fee`` from
    an ``AWSSupport*`` product is support; the rest by line type, else other."""
    cls = LINE_TYPE_CLASS.get(line_type)
    if cls in _ALWAYS:
        return cls
    if billing_entity == MARKETPLACE:
        return "marketplace"
    if line_type == "Fee" and product_code.startswith("AWSSupport"):
        return "support"
    return cls or "other"


def _decimal(value: str | None, name: str) -> Decimal:
    text = (value or "").strip()
    if not text:
        raise MalformedRecordError(f"{name} is required")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise MalformedRecordError(f"{name} is not a decimal") from None
    if not amount.is_finite():
        raise MalformedRecordError(f"{name} is not finite")
    return amount


@dataclass(frozen=True)
class CurLine:
    account: str
    usage_date: date
    line_type: str
    charge_class: str
    service: str | None
    resource: str | None  # lineItem/ResourceId as AWS sent it
    unblended: Decimal
    amortized: Decimal
    currency: str
    extra: dict[str, str] = field(default_factory=dict)


def amortized(line_type: str, unblended: Decimal, row: Mapping[str, str]) -> Decimal:
    if line_type == "DiscountedUsage":
        return _decimal(row.get("reservation/EffectiveCost"), "reservation/EffectiveCost")
    if line_type == "SavingsPlanCoveredUsage":
        return _decimal(row.get("savingsPlan/SavingsPlanEffectiveCost"),
                        "savingsPlan/SavingsPlanEffectiveCost")
    return unblended


def cur_table(body: bytes) -> tuple[list[str], list[dict[str, str] | None]]:
    """A data file's header and rows (dicts of column name to text; None for a
    row whose field count differs from the header's). ``body`` is the gzip
    object exactly as S3 sent it."""
    text = gzip.decompress(body).decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, [])
    rows: list[dict[str, str] | None] = []
    for fields in reader:
        if not fields:
            continue
        rows.append(dict(zip(header, fields, strict=True)) if len(fields) == len(header)
                    else None)
    return header, rows


def normalise_line(row: Mapping[str, str]) -> CurLine:
    account = (row.get("lineItem/UsageAccountId") or "").strip()
    if not ACCOUNT_ID.fullmatch(account):
        raise MalformedRecordError("lineItem/UsageAccountId must be twelve digits")
    line_type = (row.get("lineItem/LineItemType") or "").strip()
    if not line_type:
        raise MalformedRecordError("lineItem/LineItemType is required")
    start = _required(row.get("lineItem/UsageStartDate"), "lineItem/UsageStartDate")
    currency = (row.get("lineItem/CurrencyCode") or "").strip()
    if not CURRENCY.fullmatch(currency):
        raise MalformedRecordError("lineItem/CurrencyCode must be three capitals")
    unblended = _decimal(row.get("lineItem/UnblendedCost"), "lineItem/UnblendedCost")
    product = (row.get("lineItem/ProductCode") or "").strip()
    return CurLine(
        account=account,
        usage_date=start.date(),
        line_type=line_type,
        charge_class=charge_class(line_type, product,
                                  (row.get("bill/BillingEntity") or "").strip()),
        service=(row.get("product/ProductName") or "").strip() or product or None,
        resource=(row.get("lineItem/ResourceId") or "").strip() or None,
        unblended=unblended,
        amortized=amortized(line_type, unblended, row),
        currency=currency,
        extra={k: v for k, v in row.items() if k not in CUR_MAPPED and v != ""},
    )


def lines(rows: Iterable[Mapping[str, str] | None]) -> tuple[list[CurLine], int]:
    out: list[CurLine] = []
    bad = 0
    for r in rows:
        if r is None:
            bad += 1
            continue
        try:
            out.append(normalise_line(r))
        except MalformedRecordError:
            bad += 1
    return out, bad


def stored(amount: Decimal) -> Decimal:
    """Six places, as numeric(18,6) holds it: rounded here, once, rather than by
    the database."""
    return amount.quantize(MONEY, rounding=ROUND_HALF_UP)


@dataclass
class Aggregate:
    """Lines sharing a cost_fact natural key, summed in full precision."""

    unblended: Decimal = Decimal(0)
    amortized: Decimal = Decimal(0)
    lines: int = 0
    line_types: dict[str, int] = field(default_factory=dict)
    columns: dict[str, list[str]] = field(default_factory=dict)
    truncated: bool = False

    def add(self, line: CurLine) -> None:
        self.unblended += line.unblended
        self.amortized += line.amortized
        self.lines += 1
        self.line_types[line.line_type] = self.line_types.get(line.line_type, 0) + 1
        for k, v in line.extra.items():
            values = self.columns.setdefault(k, [])
            if v in values:
                continue
            if len(values) >= MAX_EXTRA_VALUES:
                self.truncated = True
                continue
            values.append(v)

    def raw_extra(self) -> dict[str, Any]:
        """Unmapped columns' distinct values (the bronze file has every line)."""
        out: dict[str, Any] = {
            "lines": self.lines, "line_item_types": dict(sorted(self.line_types.items())),
            "columns": {k: v[0] if len(v) == 1 else v for k, v in sorted(self.columns.items())},
        }
        if self.truncated:
            out["truncated"] = True
        return out


def resource_tail(value: str) -> str:
    """The last path or ARN segment: ``i-0abc`` of ``arn:...:instance/i-0abc``."""
    return value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
