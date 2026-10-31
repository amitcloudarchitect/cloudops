"""The simulation generator (docs/simulation): deterministic, invented text only,
money with its currency, every VM of the estate as a CI and a resource, and the golden
set in step with the open incidents it describes.

Two checks read the user's own files when they are present, and skip otherwise
(neither file is ever in the repository):

    CLOUDOPS_SIM_WORKBOOK=/path/to/report.xlsx \\
    CLOUDOPS_SIM_VM_CSV=/path/to/AzureVirtualMachines.csv \\
        uv run --with openpyxl pytest packages/devdata/tests/test_simulation.py
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import fields
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cloudops_agents.triage import injection_signals
from cloudops_devdata.simulation import qualys
from cloudops_devdata.simulation.model import (
    COMPANY,
    CORRELATION_DISPLAY,
    DAYS,
    Simulation,
    build,
    example_estate,
    load_estate,
    params,
    read_estate,
    vms,
)
from cloudops_devdata.simulation.rows import platform_rows
from cloudops_devdata.simulation.servicenow import FILES, STATE, import_files
from cloudops_devdata.simulation.triage import KNOWLEDGE, TRIAGE, bind
from cloudops_devdata.synthetic import generate

ANCHOR = date(2026, 10, 30)
REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "packages/devdata/src/cloudops_devdata/simulation"
EXAMPLE = example_estate()  # invented: the real estate is never committed
GOLDEN = REPO / "evals/golden/simulation.jsonl"


@pytest.fixture(scope="module")
def sim() -> Simulation:
    return build(ANCHOR, 11, EXAMPLE)


def _rendered(s: Simulation) -> dict[str, bytes]:
    out = {k: v.encode() for k, v in import_files(s).items()}
    out["host_detection.xml"] = qualys.detections(s, platform_rows(s, uuid4()))
    return out


def _table(sim: Simulation, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(import_files(sim)[name])))


# Determinism -------------------------------------------------------------------------


def test_the_same_seed_and_anchor_give_byte_identical_output(sim: Simulation) -> None:
    assert _rendered(sim) == _rendered(build(ANCHOR, 11, EXAMPLE))
    tenant = uuid4()
    assert platform_rows(sim, tenant) == platform_rows(build(ANCHOR, 11, EXAMPLE), tenant)


def test_a_different_seed_gives_different_history(sim: Simulation) -> None:
    other = _rendered(build(ANCHOR, 12, EXAMPLE))
    assert other["04_incident.csv"] != _rendered(sim)["04_incident.csv"]


# Shape -------------------------------------------------------------------------------


def test_volumes_are_the_workbook_per_customer_rates_over_ninety_days(sim: Simulation) -> None:
    per_month = params()["volumes_per_month"]
    history = [t for t in sim.table("incident") if t.triage is None]
    assert len(history) == round(per_month["incident"] * DAYS / 30)
    for table in ("change_request", "sc_req_item", "pm_project_task"):
        assert len(sim.table(table)) == round(per_month[table] * DAYS / 30), table
    assert len(sim.table("change_task")) == len(sim.table("change_request"))
    assert all(sim.start <= t.opened_at.date() <= ANCHOR for t in sim.tickets)
    assert (ANCHOR - sim.start).days == DAYS - 1


def test_priority_mix_follows_the_parameters(sim: Simulation) -> None:
    history = [t for t in sim.table("incident") if t.triage is None]
    for priority, share in params()["incident"]["priority_mix"].items():
        seen = sum(1 for t in history if t.priority == priority) / len(history)
        assert abs(seen - share) < 0.08, priority


def test_no_ticket_is_raised_against_a_vm_before_it_existed(sim: Simulation) -> None:
    created = {v.name: v.created for v in sim.vms}
    for t in sim.tickets:
        if t.ci and t.triage is None:
            oldest = min(created.values())
            assert t.opened_at >= created[t.ci] or created[t.ci] == oldest, t.number


def test_numbers_are_unique_and_clear_of_the_synthetic_seed(sim: Simulation) -> None:
    numbers = [t.number for t in sim.tickets]
    assert len(numbers) == len(set(numbers))
    seed_numbers = {w.number for w in generate(ANCHOR, 7).work_items}
    assert not set(numbers) & seed_numbers
    changes = {t.number for t in sim.table("change_request")}
    assert {t.change_request for t in sim.table("change_task")} == changes


def test_import_files_reference_only_what_an_earlier_file_creates(sim: Simulation) -> None:
    names = [f[0] for f in FILES]
    assert names == sorted(names), "load order is the file order"
    vms = {r["name"] for r in _table(sim, "03_cmdb_ci_vm_instance.csv")}
    groups = {r["name"] for r in _table(sim, "02_sys_user_group.csv")}
    changes = {r["number"] for r in _table(sim, "05_change_request.csv")}
    for name, table, coalesce, _ in FILES[3:]:
        rows = _table(sim, name)
        assert rows, name
        assert len({r[coalesce] for r in rows}) == len(rows), f"{name}: coalesce not unique"
        for r in rows:
            assert r["company"] == COMPANY
            assert r["correlation_display"] == CORRELATION_DISPLAY
            assert int(r["state"]) in STATE[table].values()
            assert r["assignment_group"] in groups
            ci = r.get("cmdb_ci") or r.get("configuration_item")
            assert not ci or ci in vms, (name, ci)
            if table == "change_task":
                assert r["change_request"] in changes


def test_open_triage_incidents_cover_every_action_class(sim: Simulation) -> None:
    golden = [t for t in TRIAGE if t.golden]
    assert 28 <= len(TRIAGE) <= 35
    assert {t.action_class for t in golden} == {"A", "B", "C", "D", "E"}
    assert sum(1 for t in TRIAGE if t.golden == "injection") >= 3
    assert sum(1 for t in TRIAGE if t.action_class == "E") >= 3
    open_numbers = {t.number for t in sim.table("incident") if t.state in ("New", "In Progress")}
    assert {t.number for t in TRIAGE} <= open_numbers
    names = {v.name for v in sim.vms}
    bound = bind(sim.vms)
    assert all(b.ci is None or b.ci in names for b in bound)
    assert all("{" not in b.short_description + b.description for b in bound)


def test_every_incident_binds_on_any_estate_however_small() -> None:
    one = vms(EXAMPLE, 11)[:1]
    bound = bind(one)
    assert len(bound) == len(TRIAGE)
    assert {b.ci for b in bound} <= {one[0].name, None}


def test_roles_bind_to_vms_that_fit_them(sim: Simulation) -> None:
    by_name = {v.name: v for v in sim.vms}
    for b in bind(sim.vms):
        role = b.incident.role
        if role is None or b.ci is None:
            continue
        vm = by_name[b.ci]
        assert vm.os_family == ("Windows" if role.startswith("win") else "Linux"), b.incident
        assert vm.running == ("_on" in role), b.incident


def test_planted_instructions_trip_the_heuristic_only_where_planted() -> None:
    for t in TRIAGE:
        hit = injection_signals({"short_description": t.short_description})
        assert bool(hit) == (t.golden == "injection"), t.number
    # The description-only plants are there for when triage reads descriptions:
    # one the heuristic would catch, one written to slip past it (only a model,
    # or a schema, can stop that one).
    described = [t for t in TRIAGE if t.golden is None]
    caught = [bool(injection_signals({"description": t.description})) for t in described]
    assert sorted(caught) == [False, True]


# Money -------------------------------------------------------------------------------


def _decimals(row: Any) -> dict[str, Decimal]:
    return {f.name: getattr(row, f.name) for f in fields(row)
            if isinstance(getattr(row, f.name), Decimal)}


def test_every_money_value_is_a_decimal_beside_its_currency(sim: Simulation) -> None:
    data = platform_rows(sim, uuid4())
    money_rows: list[Any] = [*data.sows, *data.costs, *data.billing_totals]
    assert data.sows
    assert data.costs
    assert data.billing_totals
    for row in money_rows:
        assert re.fullmatch(r"[A-Z]{3}", row.currency), row
        for name, value in _decimals(row).items():
            if name in ("target_margin", "consumption_uplift"):
                continue  # fractions, not money
            assert value == value.quantize(Decimal("0.000001")), (name, value)
    currencies = {r.currency for r in money_rows}
    assert currencies == {"EUR", "USD"}, "contract in EUR, Azure billing in USD"
    for table in (data.sows, data.costs, data.billing_totals, data.work_items):
        for row in table:
            assert not any(isinstance(getattr(row, f.name), float) for f in fields(row))


def test_provider_totals_agree_with_the_line_items(sim: Simulation) -> None:
    data = platform_rows(sim, uuid4())
    for b in (b for b in data.billing_totals if b.kind == "provider_total"):
        lines = sum((c.unblended_cost for c in data.costs if c.invoice_period == b.invoice_period
                     and c.usage_date <= b.as_of), Decimal(0))
        assert lines == b.amount
        assert b.currency == data.accounts[0].billing_currency


def test_hours_are_decimal_and_mostly_present_on_closed_work(sim: Simulation) -> None:
    items = platform_rows(sim, uuid4()).work_items
    closed = [w for w in items if w.closed_at is not None]
    with_hours = [w for w in closed if w.actual_hours is not None]
    assert len(with_hours) / len(closed) > 0.9
    tasks = [w for w in items if w.kind in ("change_task", "project_task")]
    assert all(isinstance(w.planned_hours, Decimal) for w in tasks)


# The lab estate ----------------------------------------------------------------------


def test_every_vm_becomes_one_ci_and_one_resource(sim: Simulation) -> None:
    cis = {r["name"]: r for r in _table(sim, "03_cmdb_ci_vm_instance.csv")}
    resources = {r.name: r for r in platform_rows(sim, uuid4()).resources}
    assert len(EXAMPLE.rows) == len(cis) == len(resources)
    for v in EXAMPLE.rows:
        ci = cis[v["NAME"]]
        assert ci["company"] == COMPANY
        assert ci["state"] == ("on" if v["STATUS"] == "Running" else "off")
        assert int(ci["disks"]) == int(v["DISKS"])
        assert f"/resourceGroups/{v['RESOURCE GROUP']}/" in ci["object_id"]
        assert EXAMPLE.subscription_id in ci["object_id"]
        r = resources[v["NAME"]]
        assert r.region == v["LOCATION"].lower().replace(" ", "")
        public = v["PUBLIC IP ADDRESS"].strip()
        if public not in ("", "-"):
            assert public in r.ip_addresses


def test_the_example_estate_is_invented() -> None:
    """RFC 5737 public addresses, RFC 1918 private ones, made-up names."""
    for v in EXAMPLE.rows:
        public = v["PUBLIC IP ADDRESS"].strip()
        assert public in ("", "-") or public.startswith(("192.0.2.", "198.51.100.", "203.0.113."))
        assert v["NAME"].startswith("vm-")
    assert all(vm.private_ip.startswith("10.") for vm in vms(EXAMPLE, 11))


def test_an_export_of_more_than_one_subscription_is_refused() -> None:
    text = (PACKAGE / "example_estate.csv").read_text(encoding="utf-8")
    lines = text.splitlines()
    other = lines[1].replace(EXAMPLE.subscription_id, "00000000-0000-4000-8000-000000000000")
    with pytest.raises(ValueError, match="exactly one subscription"):
        read_estate("\n".join([*lines, other]))
    with pytest.raises(ValueError, match="portal VM export"):
        read_estate("NAME,SIZE\nx,y\n")


def test_findings_name_the_vms_by_name_and_address(sim: Simulation) -> None:
    from cloudops_connectors.qualys import mapping

    data = platform_rows(sim, uuid4())
    by_name = {v.name.lower(): v for v in sim.vms}
    hosts = mapping.hosts(qualys.detections(sim, data))
    assert len(hosts) == len({f.asset_name for f in data.vulnerabilities})
    found = 0
    for host in hosts:
        detections, _ = mapping.normalise(host)
        for d in detections:
            vm = by_name[d.asset_name]
            assert d.ip == vm.private_ip
            assert d.customer_raw == COMPANY
            found += 1
    assert found == len(data.vulnerabilities)
    severities = {f.severity for f in data.vulnerabilities}
    assert {3, 4, 5} <= severities


_REAL_CSV = os.environ.get("CLOUDOPS_SIM_VM_CSV", "")
_NO_CSV = "CLOUDOPS_SIM_VM_CSV not set: the real VM export is never in the repo"


@pytest.mark.skipif(not _REAL_CSV, reason=_NO_CSV)
def test_the_real_export_loads_vm_for_vm() -> None:
    rows = list(csv.DictReader(io.StringIO(Path(_REAL_CSV).read_text(encoding="utf-8-sig"))))
    estate = load_estate(Path(_REAL_CSV))
    mine = {v.name: v for v in vms(estate, 11)}
    assert len(mine) == len(rows)
    for r in rows:
        v = mine[r["NAME"]]
        ip = r["PUBLIC IP ADDRESS"].strip()
        assert (v.os_family, v.resource_group, v.vnet, v.location, v.status, v.size, v.disks) \
            == (r["OPERATING SYSTEM"], r["RESOURCE GROUP"], r["VIRTUAL NETWORK"],
                r["LOCATION"], r["STATUS"], r["SIZE"], int(r["DISKS"]))
        assert v.public_ip == (None if ip in ("", "-") else ip)
        assert estate.subscription_id in r["RESOURCE LINK"].lower()
    sim = build(ANCHOR, 11, estate)
    assert len(_table(sim, "03_cmdb_ci_vm_instance.csv")) == len(rows)
    assert all(b.ci in mine for b in bind(sim.vms) if b.ci)


# Every file this change adds or edits: none may carry a fact of the real estate.
_CHANGED = ("packages/devdata", "packages/evals", "evals", "docs/simulation",
            "docs/adr/0025-simulation-dataset.md", "tools/simulation",
            "db/tests/test_evaluation.py", "db/tests/test_simulation_load.py", "Makefile")


@pytest.mark.skipif(not _REAL_CSV, reason=_NO_CSV)
def test_no_fact_of_the_real_estate_is_in_the_repository() -> None:
    estate = load_estate(Path(_REAL_CSV))
    facts = {estate.subscription_id, estate.subscription_name.lower()}
    for r in estate.rows:
        facts |= {r[c].strip().lower() for c in ("NAME", "RESOURCE GROUP", "VIRTUAL NETWORK",
                                                 "PUBLIC IP ADDRESS")}
    facts = {f for f in facts if len(f) >= 4 and f != "-"}
    found = []
    for root in _CHANGED:
        path = REPO / root
        files = [path] if path.is_file() else [
            p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        for f in files:
            try:
                text = f.read_text(encoding="utf-8").lower()
            except UnicodeDecodeError:
                continue
            found += [f"{f.relative_to(REPO)}: a VM fact" for fact in facts if fact in text]
    assert found == []


# Golden set --------------------------------------------------------------------------


def test_the_golden_set_describes_the_open_incidents_exactly() -> None:
    """The golden set is ``bind`` over the invented example estate."""
    cases = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()]
    by_number = {c["ticket"]["number"]: c for c in cases}
    bound = [b for b in bind(vms(EXAMPLE, 11)) if b.incident.golden]
    assert len(cases) == len(bound)
    for b in bound:
        t, c = b.incident, by_number[b.incident.number]
        assert c["kind"] == t.golden
        assert c["tenant"] == "azsponsor"
        assert c["ticket"]["short_description"] == b.short_description
        assert c["ticket"].get("configuration_item") == b.ci
        assert c["ticket"]["company"] == COMPANY
        exp = c["expected"]
        assert exp["route"] == t.route
        assert exp.get("reason") == t.reason
        if t.reason != "low_confidence":
            assert (exp["intent"], exp["action_class"]) == (t.intent, t.action_class)
    cited = {b.incident.runbook for b in bound if b.incident.runbook}
    assert cited <= {ref for ref, *_ in KNOWLEDGE}


# No workbook text --------------------------------------------------------------------

# Columns of the workbook that name people, customers or hosts, and those that hold
# free text. Used only to build the denylist at test time.
_PEOPLE_AND_CUSTOMERS = ("Company", "Customer", "Customer Name", "Client Name",
                         "Partner Name", "Recipient Company", "Assigned to", "Requested for",
                         "Resolved by", "Closed by", "Updated by", "Created By", "Completed By",
                         "Recipient", "Assigned To")
_IDENTIFIERS = ("Name", "Account Name", "Asset Name", "Object Name", "Email",
                "Assignment Workgroup", "Assignment Group", "Configuration item")
_TEXT_COLUMNS = ("Short description", "Description", "Close notes", "Comment", "Subject",
                 "Task Name", "Notes", "Results", "Threat", "Solution", "Resolve SLA",
                 "Response SLA")
# Words too common to mean anything when they appear on their own.
# The last few are summary-row labels, and "mark" the verb.
_COMMON = frozenset([
    "limited", "ltd", "group", "services", "service", "insurance", "credit", "cloud", "azure",
    "server", "servers", "windows", "linux", "microsoft", "datacenter", "standard", "public",
    "private", "network", "team", "support", "global", "india", "solutions",
    "international", "holding", "holdings", "company", "systems", "system", "data", "america",
    "europe", "north", "south", "east", "west", "admin", "user", "users", "test", "prod",
    "production", "level", "critical", "warning", "alert", "monitoring", "status", "client",
    "resource", "account", "closure", "incident", "labels", "managed", "parts", "plus",
    "professional", "mark",
])


def _workbook_denylist(path: Path) -> tuple[set[str], set[str]]:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    phrases: set[str] = set()
    words: set[str] = set()
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header: list[str] = []
        for r in rows:
            if r and sum(1 for c in r if c is not None) >= 3:
                header = [str(c).strip() if c is not None else "" for c in r]
                break
        for r in rows:
            for col, value in zip(header, r, strict=False):
                if not isinstance(value, str) or not value.strip():
                    continue
                v = value.strip()
                if col in _PEOPLE_AND_CUSTOMERS and len(v) >= 4 and v.lower() not in _COMMON:
                    phrases.add(v.lower())
                    words.update(w for w in re.findall(r"[a-z]{4,}", v.lower())
                                 if w not in _COMMON)
                elif col in _IDENTIFIERS and len(v) >= 6 and re.search(r"[\d_.@-]|\s", v):
                    phrases.add(v.lower())  # a host, group or address; not a lone word
                elif col in _TEXT_COLUMNS and len(v) >= 25:
                    phrases.add(v.lower())
    return phrases, words


@pytest.mark.skipif(not os.environ.get("CLOUDOPS_SIM_WORKBOOK"),
                    reason="CLOUDOPS_SIM_WORKBOOK not set: the workbook is not in the repo")
def test_no_workbook_name_or_text_reaches_the_output(sim: Simulation) -> None:
    phrases, words = _workbook_denylist(Path(os.environ["CLOUDOPS_SIM_WORKBOOK"]))
    assert phrases, "the denylist came out empty: wrong file?"
    assert words
    corpus = "\n".join([
        *(b.decode() for b in _rendered(sim).values()),
        (PACKAGE / "params.json").read_text(encoding="utf-8"),
        (PACKAGE / "triage.py").read_text(encoding="utf-8"),
        (PACKAGE / "model.py").read_text(encoding="utf-8"),
        GOLDEN.read_text(encoding="utf-8"),
    ]).lower()
    leaked_phrases = sorted(p for p in phrases if p in corpus)
    tokens = set(re.findall(r"[a-z]{4,}", corpus))
    leaked_words = sorted(words & tokens)
    assert leaked_phrases == [], leaked_phrases[:30]
    assert leaked_words == [], leaked_words[:30]


def test_the_window_ends_at_the_anchor() -> None:
    s = build(ANCHOR + timedelta(days=1), 11, EXAMPLE)
    assert s.start == ANCHOR + timedelta(days=1) - timedelta(days=DAYS - 1)
