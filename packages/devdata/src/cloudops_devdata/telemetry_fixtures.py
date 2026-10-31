"""Recorded telemetry events, built from the synthetic dataset.

    python -m cloudops_devdata.telemetry_fixtures      # rewrites the fixture file

One Event Hubs event per line: ``{"partition_key": ..., "body": "<payload>"}``.
Azure Monitor diagnostic-settings events (``{"records": [...]}``, resource ids
upper-cased as Azure sends them) for VMs and SQL databases, and CloudWatch
metric-stream lines for AWS instances, every five minutes for an hour before
``anchor``. Planted: a subscription nobody mapped, a resource the inventory has
never seen, a metric that is not on the allowlist, one with no mapping at all,
a malformed record and a body that is not JSON.

``expectations(anchor)`` is the ground truth per sample: which tenant and
resource it is stored against, or why it is not. The store expires rows by the
real clock, so integration tests build events relative to now; the committed
file is anchored at ``FIXTURE_ANCHOR`` for the local profile and contract tests.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final

from cloudops_devdata.servicenow_fixtures import FIXTURE_ANCHOR, FIXTURE_SEED
from cloudops_devdata.synthetic import NAMESPACE, generate

FIXTURE_FILE: Final = (
    Path(__file__).resolve().parents[4]
    / "connectors/src/cloudops_connectors/telemetry/fixtures/events.jsonl"
)
STEPS: Final = 12  # one hour at five-minute grain
UNKNOWN_SUBSCRIPTION: Final = str(uuid.uuid5(NAMESPACE, "telemetry:unmapped-subscription"))


@dataclass(frozen=True)
class Expected:
    tenant_code: str | None
    resource_id: uuid.UUID | None
    metric: str
    outcome: str  # stored | not_allowlisted | unmapped | account_quarantine | resource_quarantine


def _azure_record(rid: str, name: str, at: datetime, value: float) -> dict[str, Any]:
    return {"time": at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"), "resourceId": rid.upper(),
            "metricName": name, "timeGrain": "PT1M", "count": 1, "total": value,
            "minimum": value, "maximum": value, "average": value}


def build(anchor: datetime | None = None) -> tuple[list[dict[str, str]], list[Expected]]:
    end = anchor or datetime.combine(FIXTURE_ANCHOR, time(12), UTC)
    times = [end - timedelta(minutes=5 * (STEPS - i)) for i in range(STEPS)]
    ds = generate(FIXTURE_ANCHOR, FIXTURE_SEED)
    code = {t.id: t.short_code for t in ds.tenants}
    accounts = {a.id: a for a in ds.accounts}
    events: list[dict[str, str]] = []
    expected: list[Expected] = []
    live = [r for r in ds.resources if r.deleted_at is None]

    def add(key: str, body: str) -> None:
        events.append({"partition_key": key, "body": body})

    for tenant in ds.tenants:
        mine = [r for r in live if r.tenant_id == tenant.id]
        vms = [r for r in mine if r.type_canonical == "compute.vm"
               and accounts[r.account_id].provider == "azure"][:2]
        sql = [r for r in mine if r.type_canonical == "database.sql"
               and accounts[r.account_id].provider == "azure"][:1]
        for r in vms:
            records = []
            for i, at in enumerate(times):
                records.append(_azure_record(r.native_id, "Percentage CPU", at, 20.0 + i))
                records.append(_azure_record(r.native_id, "OS Disk Queue Depth", at, 1.0))
                expected.append(Expected(code[tenant.id], r.id, "cpu.utilisation", "stored"))
                expected.append(Expected(code[tenant.id], r.id, "disk.queue_depth",
                                         "not_allowlisted"))
            add(r.native_id, json.dumps({"records": records}))
        for r in sql:
            records = [_azure_record(r.native_id, "cpu_percent", at, 5.5) for at in times]
            expected.extend(Expected(code[tenant.id], r.id, "cpu.utilisation", "stored")
                            for _ in times)
            add(r.native_id, json.dumps({"records": records}))
        aws = [r for r in mine if r.type_canonical == "compute.vm"
               and accounts[r.account_id].provider == "aws"][:1]
        for r in aws:
            acct = accounts[r.account_id]
            lines = [json.dumps({
                "metric_stream_name": "cloudops-metrics", "account_id": acct.native_id,
                "region": acct.region_default, "namespace": "AWS/EC2",
                "metric_name": "CPUUtilization",
                "dimensions": {"InstanceId": r.native_id.rsplit("/", 1)[-1]},
                "timestamp": int(at.timestamp() * 1000),
                "value": {"max": 40.0, "min": 30.0, "sum": 210.0, "count": 6.0},
                "unit": "Percent"}) for at in times]
            expected.extend(Expected(code[tenant.id], r.id, "cpu.utilisation", "stored")
                            for _ in times)
            add(r.native_id, "\n".join(lines))

    first_vm = next(r for r in live if r.type_canonical == "compute.vm"
                    and accounts[r.account_id].provider == "azure")
    sub = accounts[first_vm.account_id].native_id
    ghost = (f"/subscriptions/{sub}/resourceGroups/rg-ghost/providers/"
             "Microsoft.Compute/virtualMachines/ghost-vm-01")
    unmapped = (f"/subscriptions/{UNKNOWN_SUBSCRIPTION}/resourceGroups/rg-x/providers/"
                "Microsoft.Compute/virtualMachines/stranger-01")
    add("ghost", json.dumps({"records": [_azure_record(ghost, "Percentage CPU", times[0], 9.0)]}))
    expected.append(Expected(code[first_vm.tenant_id], None, "cpu.utilisation",
                             "resource_quarantine"))
    add("stranger", json.dumps({"records": [
        _azure_record(unmapped, "Percentage CPU", t, 9.0) for t in times[:3]]}))
    expected.extend(Expected(None, None, "cpu.utilisation", "account_quarantine")
                    for _ in range(3))
    add("custom", json.dumps({"records": [
        _azure_record(first_vm.native_id, "Custom Heartbeat", times[0], 1.0),
        {"time": times[0].isoformat(), "metricName": "Percentage CPU", "average": 3.0},
    ]}))
    expected.append(Expected(code[first_vm.tenant_id], first_vm.id, "", "unmapped"))
    expected.append(Expected(None, None, "", "malformed"))
    add("garbage", "this is not json")
    expected.append(Expected(None, None, "", "malformed"))
    return events, expected


def expectations(anchor: datetime | None = None) -> list[Expected]:
    return build(anchor)[1]


def render(events: list[dict[str, str]]) -> str:
    return "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)


def main() -> int:
    events, expected = build()
    FIXTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_FILE.write_text(render(events), encoding="utf-8")
    print(f"wrote {FIXTURE_FILE} ({len(events)} events, {len(expected)} records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
