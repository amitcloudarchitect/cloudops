"""Azure Cost Management exports (FOCUS 1.0) -> ``cloudops.cost_fact``.

A scheduled export of the "Cost and usage (FOCUS)" dataset writes, per run, a
folder under the export root:

    <root>/<yyyymmdd-yyyymmdd>/<runId>/manifest.json
    <root>/<yyyymmdd-yyyymmdd>/<runId>/part_0_0001.csv.gz ...

``manifest.json`` names the run (``runInfo.runId``, ``submittedTime``,
``startDate``/``endDate``) and its partitioned gzip CSV parts (``blobs[].blobName``).
Every run is a complete statement of its period, so it replaces what the last
one said: the same staged swap as the AWS CUR (``core/assembly.py``).

Columns, parsed with ``Decimal`` only, never ``float``:

    SubAccountId        the subscription: an exact alias of ``azure`` (the
                        ``/subscriptions/`` prefix, if sent, is dropped; GUIDs
                        compare lowercased)
    ResourceId          linked within the account, case-insensitively (ARM ids are)
    ChargePeriodStart   usage_date (UTC)
    BillingPeriodStart  invoice_period ``YYYY-MM``; must be the run's period
    BilledCost          unblended_cost
    EffectiveCost       amortized_cost
    BillingCurrency     currency
    ServiceName         service_native
    Tags                tags_snapshot (a JSON object)

Every other column with a value is kept in ``raw_extra``; ``charge_class``
comes from the fixed rule in ``charge_class()``. Tags, names and descriptions
are customer- or publisher-written: data, never instruction (CLAUDE.md rule 4).
FOCUS date-times are UTC; one sent without a zone is read as UTC, as the
specification says it is.
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
from email.utils import parsedate_to_datetime
from typing import Any, Final
from xml.etree.ElementTree import Element

from cloudops_connectors.core.xmlrec import parse

SUBSCRIPTION: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CURRENCY: Final = re.compile(r"[A-Z]{3}")
MONEY: Final = Decimal("0.000001")  # numeric(18,6)
MAX_EXTRA_VALUES: Final = 10

# Columns that become a cost_fact column of their own.
MAPPED: Final = frozenset({
    "SubAccountId", "ResourceId", "ChargePeriodStart", "BillingPeriodStart", "BilledCost",
    "EffectiveCost", "BillingCurrency", "ServiceName", "Tags",
})
# Read by the charge-class rule; kept in raw_extra too, so the class can be checked.
RULE_INPUTS: Final = frozenset({
    "ChargeCategory", "CommitmentDiscountType", "CommitmentDiscountStatus", "x_PublisherType",
    "PublisherName", "x_SkuMeterCategory",
})
KNOWN: Final = MAPPED | RULE_INPUTS
# On every FOCUS row: a part without one has drifted.
EXPECTED: Final = frozenset({
    "SubAccountId", "ChargePeriodStart", "BillingPeriodStart", "BilledCost", "EffectiveCost",
    "BillingCurrency", "ChargeCategory",
})


class MalformedRecordError(ValueError):
    """A manifest or row without what keys it. Kept in bronze, counted, skipped."""


# --- the manifest ----------------------------------------------------------------------


@dataclass(frozen=True)
class Manifest:
    run_id: str
    submitted: datetime
    invoice_period: str  # YYYY-MM, of runInfo.startDate
    blobs: tuple[str, ...]  # blob names, relative to the container
    export_name: str | None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _moment(value: Any, name: str) -> datetime:
    text = _text(value)
    if text is None:
        raise MalformedRecordError(f"{name} is required")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedRecordError(f"{name} is not an ISO date-time") from None
    # FOCUS date-times are UTC by definition; one without a zone is read so.
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def manifest(body: bytes) -> Manifest:
    try:
        doc = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MalformedRecordError("manifest is not JSON") from None
    if not isinstance(doc, Mapping):
        raise MalformedRecordError("manifest is not an object")
    run = doc.get("runInfo")
    run = run if isinstance(run, Mapping) else {}
    run_id = _text(run.get("runId"))
    blobs = doc.get("blobs")
    if run_id is None or not isinstance(blobs, list):
        raise MalformedRecordError("runInfo.runId and blobs are required")
    names = []
    for b in blobs:
        name = _text(b.get("blobName")) if isinstance(b, Mapping) else None
        if name is None or name.startswith("/") or ".." in name.split("/"):
            raise MalformedRecordError("every blob needs a relative blobName")
        names.append(name)
    start = _moment(run.get("startDate"), "runInfo.startDate")
    _moment(run.get("endDate"), "runInfo.endDate")
    config = doc.get("exportConfig")
    export = _text(config.get("exportName")) if isinstance(config, Mapping) else None
    return Manifest(run_id, _moment(run.get("submittedTime"), "runInfo.submittedTime"),
                    f"{start:%Y-%m}", tuple(names), export)


def manifest_pattern(root: str) -> re.Pattern[str]:
    """``root`` is the export root within the container."""
    return re.compile(re.escape(root) + r"/\d{8}-\d{8}/[^/]+/manifest\.json")


def split_scope(scope: str) -> tuple[str, str]:
    """An export root scope is ``<container>/<root path>``."""
    container, _, root = scope.partition("/")
    if not container or not root or root.startswith("/") or root.endswith("/") or (
            ".." in root.split("/")):
        raise ValueError("an export root is named <container>/<path>")
    return container, root


@dataclass(frozen=True)
class Listed:
    name: str
    last_modified: datetime


def _child(el: Element, name: str) -> Element | None:
    for c in el:
        if c.tag.rsplit("}", 1)[-1] == name:
            return c
    return None


def _child_text(el: Element | None, name: str) -> str | None:
    c = None if el is None else _child(el, name)
    return None if c is None else (c.text or "").strip()


def listing(body: bytes) -> tuple[list[Listed], str | None]:
    """A List Blobs page (XML, parsed safely): the blobs and the next marker."""
    root = parse(body)
    out: list[Listed] = []
    container = _child(root, "Blobs")
    for b in [] if container is None else list(container):
        name = _child_text(b, "Name")
        modified = _child_text(_child(b, "Properties"), "Last-Modified")
        if not name or not modified:
            continue
        try:
            moment = parsedate_to_datetime(modified)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            continue
        out.append(Listed(name, moment.astimezone(UTC)))
    marker = _child_text(root, "NextMarker")
    return out, marker or None


def manifests(blobs: Iterable[Listed], root: str, since: datetime) -> list[Listed]:
    """Run manifests changed after ``since``, oldest first: the order they settled."""
    pattern = manifest_pattern(root)
    found = [b for b in blobs if pattern.fullmatch(b.name) and b.last_modified > since]
    return sorted(found, key=lambda b: (b.last_modified, b.name))


# --- the charge-class rule ----------------------------------------------------------------


def _marketplace(publisher_type: str, publisher: str) -> bool:
    if publisher_type.lower() == "marketplace":
        return True
    return bool(publisher) and not publisher.lower().startswith("microsoft")


def _support(meter_category: str, service: str) -> bool:
    return "support" in meter_category.lower() or "support" in service.lower()


def _commitment(discount_type: str) -> str | None:
    kind = discount_type.lower().replace(" ", "")
    return {"reservation": "reservation", "savingsplan": "savings_plan"}.get(kind)


def charge_class(
    category: str, *, billed: Decimal, discount_type: str = "", discount_status: str = "",
    publisher_type: str = "", publisher: str = "", meter_category: str = "",
    service: str = "",
) -> str:
    """The fixed rule (HLD D8: licence spend is separated from consumption).
    Its inputs are columns of the export: data, never instruction.

    - ``Tax``: tax. ``Credit``: credit. ``Adjustment``: credit when it reduces
      the bill, other otherwise.
    - ``Purchase``: a Reservation or Savings Plan commitment by its type; else a
      Marketplace publisher is marketplace; a support SKU support; else other.
    - ``Usage``: an *unused* commitment is that commitment's class; a
      Marketplace publisher is marketplace; a meter category ending in
      ``Licenses`` is licence; a support SKU support; else consumption.
    - Any other category: other.
    """
    cat = category.strip().lower()
    if cat == "tax":
        return "tax"
    if cat == "credit":
        return "credit"
    if cat == "adjustment":
        return "credit" if billed < 0 else "other"
    commitment = _commitment(discount_type)
    if cat == "purchase":
        if commitment is not None:
            return commitment
        if _marketplace(publisher_type, publisher):
            return "marketplace"
        return "support" if _support(meter_category, service) else "other"
    if cat == "usage":
        if discount_status.strip().lower() == "unused" and commitment is not None:
            return commitment
        if _marketplace(publisher_type, publisher):
            return "marketplace"
        if meter_category.strip().lower().endswith("licenses"):
            return "licence"
        return "support" if _support(meter_category, service) else "consumption"
    return "other"


# --- rows ------------------------------------------------------------------------------------


def table(body: bytes) -> tuple[list[str], list[dict[str, str] | None]]:
    """A part's header and rows (None for a row whose field count differs from
    the header's). ``body`` is the gzip blob exactly as it was stored."""
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


def subscription_id(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if text.startswith("/subscriptions/"):
        text = text.removeprefix("/subscriptions/").split("/", 1)[0]
    return text if SUBSCRIPTION.fullmatch(text) else None


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


def _tags(value: str | None) -> dict[str, str] | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        raise MalformedRecordError("Tags is not a JSON object") from None
    if not isinstance(doc, Mapping):
        raise MalformedRecordError("Tags is not a JSON object")
    return {str(k): "" if v is None else str(v) for k, v in doc.items()} or None


@dataclass(frozen=True)
class FocusLine:
    subscription: str
    usage_date: date
    invoice_period: str
    category: str
    charge_class: str
    service: str | None
    resource: str | None  # ResourceId as sent
    billed: Decimal
    effective: Decimal
    currency: str
    tags: dict[str, str] | None
    extra: dict[str, str] = field(default_factory=dict)


def normalise(row: Mapping[str, str]) -> FocusLine:
    sub = subscription_id(row.get("SubAccountId"))
    if sub is None:
        raise MalformedRecordError("SubAccountId must be a subscription id")
    category = (row.get("ChargeCategory") or "").strip()
    if not category:
        raise MalformedRecordError("ChargeCategory is required")
    currency = (row.get("BillingCurrency") or "").strip()
    if not CURRENCY.fullmatch(currency):
        raise MalformedRecordError("BillingCurrency must be three capitals")
    billed = _decimal(row.get("BilledCost"), "BilledCost")
    effective = _decimal(row.get("EffectiveCost"), "EffectiveCost")
    usage = _moment(row.get("ChargePeriodStart"), "ChargePeriodStart")
    period = _moment(row.get("BillingPeriodStart"), "BillingPeriodStart")
    service = (row.get("ServiceName") or "").strip()
    raw_tags = row.get("Tags")
    try:
        tags = _tags(raw_tags)
    except MalformedRecordError:
        tags = None  # kept verbatim in raw_extra instead
    extra = {k: v for k, v in row.items() if k not in MAPPED and v != ""}
    if tags is None and (raw_tags or "").strip():
        extra["Tags"] = raw_tags or ""
    return FocusLine(
        subscription=sub,
        usage_date=usage.date(),
        invoice_period=f"{period:%Y-%m}",
        category=category,
        charge_class=charge_class(
            category, billed=billed,
            discount_type=row.get("CommitmentDiscountType") or "",
            discount_status=row.get("CommitmentDiscountStatus") or "",
            publisher_type=row.get("x_PublisherType") or "",
            publisher=(row.get("PublisherName") or "").strip(),
            meter_category=row.get("x_SkuMeterCategory") or "", service=service),
        service=service or None,
        resource=(row.get("ResourceId") or "").strip() or None,
        billed=billed,
        effective=effective,
        currency=currency,
        tags=tags,
        extra=extra,
    )


def lines(rows: Iterable[Mapping[str, str] | None]) -> tuple[list[FocusLine], int]:
    out: list[FocusLine] = []
    bad = 0
    for r in rows:
        if r is None:
            bad += 1
            continue
        try:
            out.append(normalise(r))
        except MalformedRecordError:
            bad += 1
    return out, bad


def stored(amount: Decimal) -> Decimal:
    """Six places, as numeric(18,6) holds it: rounded here, once."""
    return amount.quantize(MONEY, rounding=ROUND_HALF_UP)


@dataclass
class Aggregate:
    """Lines sharing a cost_fact natural key, summed in full precision."""

    unblended: Decimal = Decimal(0)
    amortized: Decimal = Decimal(0)
    lines: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    columns: dict[str, list[str]] = field(default_factory=dict)
    tags: dict[str, str] | None = None
    truncated: bool = False

    def add(self, line: FocusLine) -> None:
        self.unblended += line.billed
        self.amortized += line.effective
        self.lines += 1
        self.categories[line.category] = self.categories.get(line.category, 0) + 1
        if self.tags is None and line.tags:
            self.tags = line.tags
        for k, v in line.extra.items():
            values = self.columns.setdefault(k, [])
            if v in values:
                continue
            if len(values) >= MAX_EXTRA_VALUES:
                self.truncated = True
                continue
            values.append(v)

    def raw_extra(self, tags_key: str) -> dict[str, Any]:
        """Unmapped columns' distinct values (the bronze part has every line);
        the tags under ``tags_key``, for the swap to move to tags_snapshot."""
        out: dict[str, Any] = {
            "lines": self.lines, "charge_categories": dict(sorted(self.categories.items())),
            "columns": {k: v[0] if len(v) == 1 else v for k, v in sorted(self.columns.items())},
        }
        if self.truncated:
            out["truncated"] = True
        if self.tags:
            out[tags_key] = dict(sorted(self.tags.items()))
        return out
