"""Event Hubs payloads -> the canonical metric model.

Two shapes arrive on ``evh-metrics``:

- **Azure Monitor** diagnostic settings: one event is ``{"records": [...]}``,
  each record ``{time, resourceId, metricName, timeGrain, count, total,
  minimum, maximum, average}``. Resource ids come upper-cased; they are
  compared case-insensitively.
- **AWS CloudWatch metric streams** (JSON output format): one record per line,
  ``{account_id, region, namespace, metric_name, dimensions, timestamp (ms),
  value: {max, min, sum, count}, unit}``.

``METRICS`` maps each provider metric to a canonical name, unit and the value
to take. A metric not in it is not an error: it is counted and left in the
bronze capture, like a canonical metric that is not on the allowlist (LLD open
item L3, ``staging.metric_allowlist``). A record without the fields that key it
is malformed: counted, never guessed.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

AZURE: Final = "azure"
AWS: Final = "aws"


class MalformedEventError(ValueError):
    """Not a payload this consumer reads, or a record without its keys."""


@dataclass(frozen=True, slots=True)
class RawMetric:
    provider: str
    account: str  # subscription GUID, lower-cased / 12-digit account id
    resource_key: str  # lower-cased Azure resource id / AWS resource id or ARN
    metric: str  # canonical
    unit: str
    ts: datetime
    value: float
    fields: frozenset[str]  # the record's field names, for drift


def _avg(r: Mapping[str, Any]) -> float:
    return float(r["average"])


def _total(r: Mapping[str, Any]) -> float:
    return float(r["total"])


def _maximum(r: Mapping[str, Any]) -> float:
    return float(r["maximum"])


def _avg_pct(r: Mapping[str, Any]) -> float:
    return float(r["average"]) * 100.0


def _cw_avg(v: Mapping[str, Any]) -> float:
    return float(v["sum"]) / float(v["count"])


def _cw_sum(v: Mapping[str, Any]) -> float:
    return float(v["sum"])


def _cw_up(v: Mapping[str, Any]) -> float:
    """StatusCheckFailed is 1 when failing: availability is its complement."""
    return (1.0 - float(v["sum"]) / float(v["count"])) * 100.0


Pick = Callable[[Mapping[str, Any]], float]

# (provider, namespace or "", native metric) -> (canonical metric, unit, value)
METRICS: Final[dict[tuple[str, str, str], tuple[str, str, Pick]]] = {
    (AZURE, "", "Percentage CPU"): ("cpu.utilisation", "percent", _avg),
    (AZURE, "", "Available Memory Bytes"): ("memory.available", "bytes", _avg),
    (AZURE, "", "Disk Read Operations/Sec"): ("disk.read_ops", "count_per_second", _avg),
    (AZURE, "", "Disk Write Operations/Sec"): ("disk.write_ops", "count_per_second", _avg),
    (AZURE, "", "Network In Total"): ("network.in", "bytes", _total),
    (AZURE, "", "Network Out Total"): ("network.out", "bytes", _total),
    (AZURE, "", "VmAvailabilityMetric"): ("availability", "percent", _avg_pct),
    (AZURE, "", "OS Disk Queue Depth"): ("disk.queue_depth", "count", _avg),
    (AZURE, "", "cpu_percent"): ("cpu.utilisation", "percent", _avg),
    (AZURE, "", "storage_percent"): ("storage.used", "percent", _maximum),
    (AZURE, "", "connection_failed"): ("connections.failed", "count", _total),
    (AZURE, "", "availability"): ("availability", "percent", _avg),
    (AZURE, "", "Availability"): ("availability", "percent", _avg),
    (AZURE, "", "SuccessE2ELatency"): ("latency.e2e", "milliseconds", _avg),
    (AZURE, "", "UsedCapacity"): ("capacity.used", "bytes", _avg),
    (AZURE, "", "VipAvailability"): ("availability", "percent", _avg),
    (AZURE, "", "DipAvailability"): ("health_probe", "percent", _avg),
    (AWS, "AWS/EC2", "CPUUtilization"): ("cpu.utilisation", "percent", _cw_avg),
    (AWS, "AWS/EC2", "NetworkIn"): ("network.in", "bytes", _cw_sum),
    (AWS, "AWS/EC2", "NetworkOut"): ("network.out", "bytes", _cw_sum),
    (AWS, "AWS/EC2", "StatusCheckFailed"): ("availability", "percent", _cw_up),
    (AWS, "AWS/RDS", "CPUUtilization"): ("cpu.utilisation", "percent", _cw_avg),
    (AWS, "AWS/RDS", "FreeStorageSpace"): ("storage.free", "bytes", _cw_avg),
}

# Dimension naming the resource, per CloudWatch namespace.
AWS_RESOURCE_DIMENSION: Final = {"AWS/EC2": "InstanceId", "AWS/RDS": "DBInstanceIdentifier"}


@dataclass
class Parsed:
    metrics: list[RawMetric]
    records: int = 0
    malformed: int = 0
    unmapped: int = 0


def _time(value: Any) -> datetime:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000.0, UTC)
    if isinstance(value, str):
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise MalformedEventError("time without a zone")
        return moment.astimezone(UTC)
    raise MalformedEventError("no time")


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise MalformedEventError("value is not finite")
    return value


def _azure(record: Mapping[str, Any], out: Parsed) -> None:
    rid = record.get("resourceId")
    name = record.get("metricName")
    if not isinstance(rid, str) or not isinstance(name, str):
        raise MalformedEventError("resourceId and metricName are required")
    parts = rid.strip("/").split("/")
    if len(parts) < 2 or parts[0].lower() != "subscriptions":
        raise MalformedEventError("resourceId is not an Azure resource id")
    mapped = METRICS.get((AZURE, "", name))
    if mapped is None:
        out.unmapped += 1
        return
    metric, unit, pick = mapped
    try:
        value = _finite(pick(record))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise MalformedEventError("the value field is missing") from None
    out.metrics.append(RawMetric(AZURE, parts[1].lower(), rid.lower(), metric, unit,
                                 _time(record.get("time")), value, frozenset(record)))


def _aws(record: Mapping[str, Any], out: Parsed) -> None:
    account, ns, name = record.get("account_id"), record.get("namespace"), record.get(
        "metric_name")
    dims = record.get("dimensions")
    if not (isinstance(account, str) and isinstance(ns, str) and isinstance(name, str)
            and isinstance(dims, Mapping)):
        raise MalformedEventError("account_id, namespace, metric_name, dimensions required")
    mapped = METRICS.get((AWS, ns, name))
    if mapped is None:
        out.unmapped += 1
        return
    key = dims.get(AWS_RESOURCE_DIMENSION.get(ns, ""))
    if not isinstance(key, str) or not key:
        raise MalformedEventError("no resource dimension")
    metric, unit, pick = mapped
    value_block = record.get("value")
    try:
        value = _finite(pick(value_block if isinstance(value_block, Mapping) else {}))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise MalformedEventError("the value block is missing") from None
    out.metrics.append(RawMetric(AWS, account, key.lower(), metric, unit,
                                 _time(record.get("timestamp")), value, frozenset(record)))


def parse(body: bytes) -> Parsed:
    """One event's records. A body that is neither shape is one malformed record."""
    out = Parsed([])
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        out.records, out.malformed = 1, 1
        return out
    stripped = text.strip()
    try:
        whole = json.loads(stripped)
    except ValueError:
        whole = None
    if isinstance(whole, Mapping) and isinstance(whole.get("records"), list):
        records: list[Any] = list(whole["records"])
        handler = _azure
    else:
        records = []
        for line in stripped.splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                records.append(None)
        handler = _aws
    for record in records:
        out.records += 1
        try:
            if not isinstance(record, Mapping):
                raise MalformedEventError("not an object")
            handler(record, out)
        except MalformedEventError:
            out.malformed += 1
    return out
