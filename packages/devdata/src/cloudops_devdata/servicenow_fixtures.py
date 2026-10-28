"""Recorded ServiceNow fixtures, built from the synthetic dataset.

    python -m cloudops_devdata.servicenow_fixtures      # rewrites the fixture files

Table API records (``sysparm_display_value=all`` shape) for incidents and
change tasks, anonymised by construction: every company, person and host is
synthetic. Built from ``synthetic.generate(FIXTURE_ANCHOR, 7)``, the same
dataset the local seed and the connector integration test load, so CI sys_ids
and company spellings line up with the inventory and aliases in the database.

``expectations()`` is the ground truth a correct connector must reproduce:
which tenant each ticket lands in (or quarantine), which resource it links to,
and which records are rejected as malformed. The committed fixture files must
equal ``build()`` exactly (a test checks), so they cannot drift from it.
"""

from __future__ import annotations

import json
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from cloudops_devdata.synthetic import (
    NAMESPACE,
    CiOutcome,
    CiTruth,
    Dataset,
    WorkItemRow,
    generate,
)

FIXTURE_ANCHOR: Final = date(2026, 10, 27)
FIXTURE_SEED: Final = 7
ENTITY_KIND: Final = {"incident": "incident", "change_task": "change_task"}
TIME: Final = "%Y-%m-%d %H:%M:%S"
FIXTURE_DIR: Final = (
    Path(__file__).resolve().parents[4]
    / "connectors/src/cloudops_connectors/servicenow/fixtures"
)
STATE_VALUE: Final = {
    "New": "1", "In Progress": "2", "On Hold": "3", "Awaiting Caller": "3",
    "Resolved": "6", "Closed": "7",
}


@dataclass(frozen=True)
class Expected:
    number: str
    entity: str
    tenant_code: str | None  # None: quarantined
    resource_id: uuid.UUID | None
    malformed: bool = False


def _sys_id(*parts: object) -> str:
    return uuid.uuid5(NAMESPACE, "snow:" + ":".join(str(p) for p in parts)).hex


def _f(value: object, display: object | None = None) -> dict[str, str]:
    v = "" if value is None else str(value)
    d = v if display is None else ("" if display is None else str(display))
    return {"value": v, "display_value": d}


def _t(moment: datetime | None) -> str:
    return "" if moment is None else moment.astimezone(UTC).strftime(TIME)


def _bool(value: bool | None) -> str:
    return "" if value is None else str(value).lower()


def _record(
    entity: str, w: WorkItemRow, truth: CiTruth, ds: Dataset, rng: random.Random
) -> dict[str, Any]:
    resources = {r.id: r for r in ds.resources}
    code = next(t.short_code for t in ds.tenants if t.id == w.tenant_id)
    updated = (w.closed_at or w.opened_at) + timedelta(minutes=rng.randrange(1, 90))
    horizon = datetime.combine(FIXTURE_ANCHOR, datetime.max.time(), tzinfo=UTC)
    updated = min(updated, horizon)
    # cmdb_ci: the inventory's own sys_id where it has one (external-key match),
    # otherwise a CMDB sys_id the inventory has never seen (name match or nothing).
    if truth.outcome is CiOutcome.LINKED and truth.resource_id is not None:
        known = resources[truth.resource_id].servicenow_ci_sys_id
        ci = _f(known or _sys_id("ci", truth.value), truth.value)
    elif truth.outcome in (CiOutcome.AMBIGUOUS, CiOutcome.UNKNOWN):
        ci = _f(_sys_id("ci", code, truth.value), truth.value)
    elif truth.outcome is CiOutcome.JUNK:
        ci = _f("", truth.value)
    else:
        ci = _f("")
    duration = None
    if w.duration_seconds is not None:
        duration = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=w.duration_seconds)
    record: dict[str, Any] = {
        "sys_id": _f(_sys_id(w.number)),
        "number": _f(w.number),
        "company": _f(_sys_id("company", code), w.company_raw),
        "opened_at": _f(_t(w.opened_at)),
        "closed_at": _f(_t(w.closed_at)),
        "sys_updated_on": _f(_t(updated)),
        "priority": _f(w.priority[0], w.priority),
        "state": _f(STATE_VALUE[w.state], w.state),
        "assigned_to": _f(_sys_id("user", w.assigned_to) if w.assigned_to else "", w.assigned_to),
        "assignment_group": _f(_sys_id("group", w.assignment_workgroup), w.assignment_workgroup),
        "short_description": _f(w.short_description),
        "cmdb_ci": ci,
        "close_code": _f(w.closure_code),
        "close_notes": _f(w.close_notes),
        "made_sla": _f(_bool(w.made_sla)),
        "u_resolve_sla": _f(_bool(w.resolve_sla)),
        "u_response_sla": _f(_bool(w.response_sla)),
        "u_sla_breached": _f(_bool(w.sla_breached)),
        "calendar_duration": _f(_t(duration)),
        "calendar_stc": _f(w.resolve_time_seconds),
        "u_pending_total": _f(w.pending_total_seconds),
        # Fields the connector does not map: they must survive in raw_extra.
        "u_customer_reference": _f(f"CR-{w.number[-5:]}"),
        "x_gtn_region": _f(rng.choice(["EMEA-North", "EMEA-South", "APAC"])),
        "sys_mod_count": _f(rng.randrange(1, 30)),
        "sys_class_name": _f(entity),
    }
    if entity == "change_task":
        record["change_request"] = _f(_sys_id("chg", w.change_request), w.change_request)
        record["change_task_type"] = _f(w.change_type)
        record["u_planned_hours"] = _f(w.planned_hours)
        record["u_actual_hours"] = _f(w.actual_hours)
    return record


def build() -> tuple[dict[str, list[dict[str, Any]]], list[Expected]]:
    ds = generate(FIXTURE_ANCHOR, FIXTURE_SEED)
    rng = random.Random(FIXTURE_SEED)  # noqa: S311 - test data
    code = {t.id: t.short_code for t in ds.tenants}
    tables: dict[str, list[dict[str, Any]]] = {e: [] for e in ENTITY_KIND}
    expected: list[Expected] = []
    for w, truth in zip(ds.work_items, ds.ci_truth, strict=True):
        entity = next((e for e, k in ENTITY_KIND.items() if k == w.kind), None)
        if entity is None:
            continue
        tables[entity].append(_record(entity, w, truth, ds, rng))
        expected.append(Expected(w.number, entity, code[w.tenant_id], truth.resource_id))

    # Tickets whose company matches no tenant: the connector must quarantine
    # them, with a count, and write nothing.
    seq = 90000
    base = next(w for w, t in zip(ds.work_items, ds.ci_truth, strict=True) if w.kind == "incident")
    for q in ds.alias_quarantine:
        for _ in range(2):
            seq += 1
            number = f"INC{seq:07d}"
            fake = WorkItemRow(**{**base.__dict__, "number": number, "company_raw": q.alias})
            record = _record("incident", fake, CiTruth("", None, CiOutcome.EMPTY, None), ds, rng)
            tables["incident"].append(record)
            expected.append(Expected(number, "incident", None, None))

    # A malformed record: no opened_at, so nothing to key it on. Kept in
    # bronze, counted as rejected, never written.
    broken = dict(tables["incident"][0])
    broken["sys_id"] = _f(_sys_id("broken"))
    broken["number"] = _f("INC0099999")
    broken["opened_at"] = _f("")
    tables["incident"].append(broken)
    expected.append(Expected("INC0099999", "incident", None, None, malformed=True))

    for records in tables.values():
        records.sort(key=lambda r: (r["sys_updated_on"]["value"], r["sys_id"]["value"]))
    return tables, expected


def expectations() -> list[Expected]:
    return build()[1]


def render(records: list[dict[str, Any]]) -> str:
    """One record per line: small, and a changed record is a one-line diff."""
    lines = ",\n".join(json.dumps(r, sort_keys=True, default=str) for r in records)
    return '{"result": [\n' + lines + "\n]}\n"


def main() -> int:
    tables, _ = build()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for entity, records in tables.items():
        path = FIXTURE_DIR / f"{entity}.json"
        path.write_text(render(records), encoding="utf-8")
        print(f"wrote {path} ({len(records)} records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

