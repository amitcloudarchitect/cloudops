"""The simulation, built in memory. Pure apart from reading the estate it is
given and its own package files; deterministic from (anchor, seed, estate).

    sim = build(date(2026, 10, 30), 11, load_estate(DEFAULT_ESTATE))

One customer, "Azure Sponsorship", whose estate is a subscription's VMs as the
Azure portal exports them. The real one, the lab subscription, is read at run
time from a local, gitignored file and never committed. Tests use the invented
``example_estate()``. Everything else is invented, shaped by ``params.json``:
distributions taken once from the operations workbook, with no name or text
from it. Nothing here reads the workbook.

The tickets are built once and rendered twice: as ServiceNow import rows
(``servicenow.py``) and as the platform's ``work_item`` rows (``rows``), so the
console shows locally what the ServiceNow connector would ingest from the dev
instance, number for number.
"""

from __future__ import annotations

import csv
import io
import json
import math
import random
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Final

from cloudops_devdata.simulation.triage import TriageIncident, bind

COMPANY: Final = "Azure Sponsorship"
SHORT_CODE: Final = "AZSPONSOR"  # access.onboard_tenant: 2 to 10 capitals or digits
# As the real subscription tags a customer's hosts (ADR 0028): generic tags every host
# carries, and the customer's name as a plain tag, resolved through its qualys alias.
QUALYS_TAGS: Final = ("Cloud Agent", "Default", COMPANY)
DAYS: Final = 90
NAMESPACE: Final = uuid.UUID("0b6c4a0e-5d3e-4f43-8f39-8a9b7cf1d2a4")
# Correlation fields on every imported record: how the README finds them to undo.
CORRELATION_DISPLAY: Final = "cloudops-simulation"
# Every simulated record carries this: as its correlation_id prefix (tasks, CIs),
# or in its notes or description (company, groups, which have no correlation_id).
SIM_MARKER: Final = "SIM-AZSPONSOR"
# Number ranges far above the synthetic seed's (40000s), and above what a
# personal developer instance reaches, so an import never meets a real record.
NUMBER_BASE: Final = 900_000

GROUPS: Final = ("GTN Cloud Ops L2", "GTN Windows Platform", "GTN Linux Platform")
PEOPLE: Final = ("Tamsin Reyes", "Oskar Lindqvist", "Ines Duarte", "Kenji Arai",
                 "Leila Haddad", "Mateo Varga")


def _id(*parts: object) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, ":".join(str(p) for p in parts))


@cache
def params() -> dict[str, Any]:
    raw = resources.files(__package__).joinpath("params.json").read_text(encoding="utf-8")
    out: dict[str, Any] = json.loads(raw)
    return out


# The estate -------------------------------------------------------------------------


@dataclass(frozen=True)
class Vm:
    """One VM of the lab estate, as the portal export lists it, plus what the
    simulation adds (marked): a private address and an OS version."""

    name: str
    created: datetime
    os_family: str  # Windows | Linux
    resource_group: str
    vnet: str
    location: str  # display name, "West Europe"
    status: str  # "Running" | "Stopped (deallocated)"
    size: str
    public_ip: str | None
    disks: int
    private_ip: str  # simulated: the export has no private addresses
    os_version: str  # simulated: the export says only Windows or Linux

    @property
    def running(self) -> bool:
        return self.status == "Running"

    @property
    def region(self) -> str:
        return self.location.lower().replace(" ", "")

    def native_id(self, subscription: str) -> str:
        return (f"/subscriptions/{subscription}/resourceGroups/{self.resource_group}"
                f"/providers/Microsoft.Compute/virtualMachines/{self.name}")

    @property
    def ips(self) -> list[str]:
        return [self.private_ip, *([self.public_ip] if self.public_ip else [])]


_WINDOWS: Final = (("Microsoft Windows Server 2022 Datacenter", 0.66),
                   ("Microsoft Windows Server 2019 Datacenter", 0.30),
                   ("Microsoft Windows Server 2025 Datacenter", 0.04))
_LINUX: Final = (("Ubuntu Linux 24.04 Server", 0.6), ("Red Hat Enterprise Linux 9.6", 0.4))


def _weighted(rng: random.Random, pairs: Sequence[tuple[str, float]]) -> str:
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def _mix(rng: random.Random, mix: Mapping[str, Any]) -> str:
    items = [(k, float(v)) for k, v in mix.items() if not k.startswith("_")]
    return _weighted(rng, items)


@dataclass(frozen=True)
class Estate:
    """The VMs of one subscription, as the Azure portal's VM export lists them.

    Never committed: the real estate is read at run time from a local file
    (``DEFAULT_ESTATE``, gitignored), and later from the resources discovered
    for the tenant. Tests use ``example_estate()``, which is invented."""

    subscription_id: str
    subscription_name: str
    rows: tuple[Mapping[str, str], ...]


DEFAULT_ESTATE: Final = Path("build/simulation/estate.csv")
_SUBSCRIPTION: Final = re.compile(r"/subscriptions/([0-9a-fA-F-]{36})/", re.IGNORECASE)
_COLUMNS: Final = ("NAME", "SUBSCRIPTION", "TIME CREATED", "OPERATING SYSTEM", "RESOURCE GROUP",
                   "VIRTUAL NETWORK", "LOCATION", "STATUS", "SIZE", "PUBLIC IP ADDRESS", "DISKS",
                   "RESOURCE LINK")


def read_estate(text: str) -> Estate:
    """The portal export (CSV, as downloaded: a BOM and quoted fields)."""
    rows = list(csv.DictReader(io.StringIO(text.removeprefix("\ufeff"))))
    if not rows or any(c not in rows[0] for c in _COLUMNS):
        raise ValueError("not an Azure portal VM export: columns " + ", ".join(_COLUMNS))
    subs = {r["SUBSCRIPTION"] for r in rows}
    ids = {m.group(1).lower() for r in rows if (m := _SUBSCRIPTION.search(r["RESOURCE LINK"]))}
    if len(subs) != 1 or len(ids) != 1:
        raise ValueError("the export must cover exactly one subscription")
    return Estate(ids.pop(), subs.pop(), tuple(rows))


def load_estate(path: Path) -> Estate:
    try:
        return read_estate(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{path} not found: save the Azure portal's VM export there (it is gitignored), "
            "or pass --estate") from None


def example_estate() -> Estate:
    """An invented estate for tests and the golden set: no real name or address."""
    return read_estate(resources.files(__package__).joinpath("example_estate.csv")
                       .read_text(encoding="utf-8"))


def vms(estate: Estate, seed: int) -> list[Vm]:
    """Private addresses (RFC 1918) and OS versions are simulated, from the seed;
    everything else is the export's."""
    rng = random.Random(f"{seed}:estate")  # noqa: S311 - simulated data, not security
    vnets = sorted({r["VIRTUAL NETWORK"] for r in estate.rows}, key=str.lower)
    hosts: dict[str, int] = {}
    out = []
    for r in estate.rows:
        vnet = r["VIRTUAL NETWORK"]
        hosts[vnet] = hosts.get(vnet, 3) + 1
        family = r["OPERATING SYSTEM"]
        public = r["PUBLIC IP ADDRESS"].strip()
        out.append(Vm(
            name=r["NAME"],
            created=datetime.fromisoformat(r["TIME CREATED"]).replace(tzinfo=UTC),
            os_family=family, resource_group=r["RESOURCE GROUP"], vnet=vnet,
            location=r["LOCATION"], status=r["STATUS"], size=r["SIZE"],
            public_ip=None if public in ("", "-") else public, disks=int(r["DISKS"]),
            private_ip=f"10.{40 + vnets.index(vnet)}.0.{hosts[vnet]}",
            os_version=_weighted(rng, _WINDOWS if family == "Windows" else _LINUX),
        ))
    return out


# Tickets ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    """One ServiceNow record, before rendering. ``table`` is ServiceNow's."""

    table: str
    number: str
    opened_at: datetime
    state: str  # display value, as the connector stores it
    short_description: str
    description: str
    priority: str | None
    assignment_group: str
    assigned_to: str | None
    ci: str | None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    close_code: str | None = None
    close_notes: str | None = None
    sla_breached: bool | None = None
    pending_seconds: int | None = None
    planned_hours: Decimal | None = None
    actual_hours: Decimal | None = None
    change_request: str | None = None  # change_task: its parent
    change_type: str | None = None
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    contact_type: str | None = None
    triage: TriageIncident | None = None

    @property
    def kind(self) -> str | None:
        """work_item.kind; None for tables the platform does not mirror."""
        return {"incident": "incident", "change_task": "change_task",
                "sc_req_item": "service_request", "pm_project_task": "project_task"}.get(
                    self.table)


@dataclass
class Simulation:
    anchor: date
    seed: int
    subscription_id: str
    subscription_name: str
    vms: list[Vm]
    tickets: list[Ticket] = field(default_factory=list)

    @property
    def start(self) -> date:
        return self.anchor - timedelta(days=DAYS - 1)

    def table(self, name: str) -> list[Ticket]:
        return [t for t in self.tickets if t.table == name]


def _lognormal(rng: random.Random, spec: Mapping[str, Any]) -> float:
    """A draw with the given median and 90th percentile."""
    median, p90 = float(spec["median"]), float(spec["p90"])
    sigma = math.log(p90 / median) / 1.2816
    return median * math.exp(rng.gauss(0, sigma))


def _hours(value: float) -> Decimal:
    return Decimal(str(round(max(value, 0.25) * 4) / 4))  # quarter hours, as people book


def _opened(rng: random.Random, day: date, p: Mapping[str, Any]) -> datetime:
    hour = rng.choices(range(24), weights=p["hour_of_day_weights"])[0]
    return datetime.combine(day, time(hour, rng.randrange(60), rng.randrange(60)), UTC)


def _days(rng: random.Random, sim: Simulation, n: int, weekday_w: Sequence[int]) -> list[date]:
    all_days = [sim.start + timedelta(days=i) for i in range(DAYS)]
    weights = [weekday_w[d.weekday()] for d in all_days]
    return sorted(rng.choices(all_days, weights=weights, k=n))


# Text: every phrase invented. Monitoring labels are Azure Monitor's own names.
_METRICS: Final = {
    "availability": ("VM Availability", "Unavailable", "Available"),
    "script_error": ("Custom script check", "exit code 2", "exit code 0"),
    "memory": ("Available Memory Percent", "4%", "10%"),
    "disk": ("OS Disk Used Percent", "93%", "90%"),
    "agent": ("Heartbeat", "missing for 15 minutes", "5 minutes"),
    "cpu": ("Percentage CPU", "96%", "90% for 15 minutes"),
    "service": ("Service state", "Stopped", "Running"),
    "k8s_volume": ("Free inodes percent", "6%", "10%"),
}
_HUMAN: Final = {
    "certificate": ("Certificate on {vm} expires in {n} days",
                    "Certificate monitor reports the certificate expires soon."),
    "access": ("Cannot sign in to {vm} over RDP, account locked",
               "User reports the account locks after one attempt."),
    "backup": ("Backup job failed on {vm}", "Vault reports the job failed with a timeout."),
    "availability": ("{vm} not reachable from the jump host", "Ping and RDP both time out."),
    "memory": ("Applications on {vm} slow, memory looks exhausted",
               "Task manager shows memory near the limit."),
    "disk": ("Low disk space warning on {vm}", "The system drive is almost full."),
    "cpu": ("{vm} very slow since this morning", "CPU stays high with no obvious process."),
    "agent": ("Monitoring shows {vm} grey, please check the agent",
              "No data from the agent since last night."),
    "service": ("Scheduled task failing on {vm}", "The nightly task ends with an error."),
    "script_error": ("Health check script failing on {vm}",
                     "The check returns a non-zero exit code."),
    "k8s_volume": ("Node volume filling up on {vm}", "Free inodes keep dropping."),
}
_CLOSE: Final = {
    "auto_healed": ("Alert cleared on its own at {t}; {m} back within threshold. "
                    "No action needed, closing."),
    "parent_copy": "Duplicate of {parent}; the resolution is recorded there. Closing this one.",
    "monitored_normal": ("Watched {vm} for 45 minutes after the alert; {m} stayed within "
                         "threshold. Closing."),
    "cancelled_note": "Raised during planned maintenance on {vm}; cancelled.",
    "structured": ("Cause: {cause}\nAction: {action}\nResult: {m} back to normal on {vm}.\n"
                   "Follow-up: none"),
}
_CAUSES: Final = (("a log file grew after a failed rotation", "rotated and compressed logs"),
                  ("a stuck process held the CPU", "ended the process and confirmed the cause"),
                  ("the agent lost its connection", "restarted the agent service"),
                  ("a service stopped after updates", "started the service, set it to restart"),
                  ("temporary files filled the disk", "cleared temporary files"))
_CLOSE_CODE: Final = {"workaround": "Workaround provided", "permanent": "Solution provided",
                      "auto_close": "Solution provided", "not_resolved": "No resolution provided"}


def _pick(rng: random.Random, pool: Sequence[Vm], when: datetime) -> Vm:
    """A VM that existed at the time; the oldest one if none did yet."""
    alive = [v for v in pool if v.created <= when]
    return rng.choice(alive) if alive else min(pool, key=lambda v: v.created)


def _short(theme: str, vm: Vm, rng: random.Random, p: Mapping[str, Any]) -> tuple[str, str]:
    metric_share = sum(float(w) for k, w in p["theme_mix"].items() if k in _METRICS)
    if theme in _METRICS and rng.random() < float(p["alert_shaped_share"]) / metric_share:
        metric, value, threshold = _METRICS[theme]
        tag = "[AzMon] " if rng.random() < float(p["bracket_tag_share"]) else ""
        level = "Critical" if rng.random() < 0.86 else "Warning"
        sd = f"{tag}{level} alert: {metric} | Resource: {vm.name} | Status: {level}"
        desc = (f"Alert details\nResource: {vm.name}\nResource group: {vm.resource_group}\n"
                f"Metric: {metric}\nObserved: {value}\nThreshold: {threshold}\n"
                f"Source: Azure Monitor\nCustomer: {COMPANY}")
        return sd, desc
    sd, desc = _HUMAN[theme]
    return sd.format(vm=vm.name, n=rng.randrange(3, 30)), desc


def _incidents(sim: Simulation, rng: random.Random) -> None:
    p = params()["incident"]
    n = round(params()["volumes_per_month"]["incident"] * DAYS / 30)
    group_w = params()["assignment_group_shares"]
    all_vms = sim.vms
    for day in _days(rng, sim, n, p["weekday_weights"]):
        opened = _opened(rng, day, p)
        theme = _mix(rng, p["theme_mix"])
        if theme == "k8s_volume":
            pool = [v for v in all_vms if v.os_family == "Linux"] or all_vms
        elif theme == "service":
            pool = [v for v in all_vms if v.os_family == "Windows"] or all_vms
        else:
            pool = all_vms
        vm = _pick(rng, pool, opened)
        sd, desc = _short(theme, vm, rng, p)
        priority = _mix(rng, p["priority_mix"])
        resolve_h = min(_lognormal(rng, p["resolve_hours_lognormal"][priority]),
                        float(p["resolve_hours_cap"]))
        due = opened + timedelta(hours=resolve_h)
        closure = _mix(rng, p["closure_mix"])
        state = "Canceled" if closure == "cancelled" else _mix(rng, p["state_mix_closed"])
        # Resolved incidents close three days later (the instance default).
        resolved: datetime | None = due
        closed = None if state == "Resolved" else due + (
            timedelta(days=3) if state == "Closed" else timedelta(0))
        end = datetime.combine(sim.anchor, time(23, 59, 59), UTC)
        if due > end:
            # Still open at the anchor: not every history ticket is closed.
            state, resolved, closed, closure = "In Progress", None, None, "open"
        elif closed is not None and closed > end:
            state, closed = "Resolved", None
        shape = "cancelled_note" if closure == "cancelled" else _mix(
            rng, {k: w for k, w in p["close_note_shape_mix"].items() if k != "cancelled_note"})
        metric = _METRICS.get(theme, ("the reading", "", ""))[0]
        cause, action = rng.choice(_CAUSES)
        notes = None if resolved is None else _CLOSE[shape].format(
            t=f"{resolved:%H:%M}", m=metric, vm=vm.name, parent="{parent}", cause=cause,
            action=action)
        pending = (int(_lognormal(rng, p["pending_hours_lognormal"]) * 3600)
                   if resolved and rng.random() < float(p["pending_share"]) else None)
        hours = (None if resolved is None or rng.random() < float(p["missing_hours_rate"])
                 else _hours(_lognormal(rng, p["actual_hours_lognormal"])))
        sim.tickets.append(Ticket(
            "incident", "", opened, state, sd, desc, priority,
            GROUPS[rng.choices(range(len(GROUPS)), weights=group_w)[0]],
            rng.choice(PEOPLE), None if rng.random() < float(p["ci_empty_rate"]) else vm.name,
            resolved_at=resolved, closed_at=closed,
            close_code=None if resolved is None or closure == "cancelled" else _CLOSE_CODE[closure],
            close_notes=notes,
            sla_breached=None if resolved is None else rng.random() < float(p["sla_breach_rate"]),
            pending_seconds=pending, actual_hours=hours,
        ))


_CHANGES: Final = (
    "Apply the {month} security updates to {vm}",
    "Extend the OS disk of {vm} by 64 GB",
    "Rotate the TLS certificate on {vm}",
    "Update network security group rules for {vnet}",
    "Install the monitoring agent extension on {vm}",
    "Resize {vm} to the next size in its family",
    "Enable boot diagnostics on {vm}",
    "Change the backup policy for {vm} to 30-day retention",
)


def _changes(sim: Simulation, rng: random.Random) -> None:
    p = params()["change_request"]
    n = round(params()["volumes_per_month"]["change_request"] * DAYS / 30)
    for day in _days(rng, sim, n, params()["incident"]["weekday_weights"]):
        created = _opened(rng, day, params()["incident"])
        vm = _pick(rng, sim.vms, created)
        window = max(_lognormal(rng, p["window_hours_lognormal"]), float(p["window_hours_floor"]))
        start = created + timedelta(hours=rng.randrange(4, 72))
        end = start + timedelta(hours=window)
        work_end = end + (timedelta(hours=rng.uniform(1, 24))
                          if rng.random() < float(p["overrun_rate"]) else timedelta(0))
        sd = rng.choice(_CHANGES).format(month=f"{start:%B}", vm=vm.name, vnet=vm.vnet)
        done = work_end <= datetime.combine(sim.anchor, time(23, 59, 59), UTC)
        code = _mix(rng, p["close_mix"]) if done else None
        group = rng.choice(GROUPS)
        planned = _hours(min(window, 8.0))
        ratio = _ratio(rng, p["task_actual_over_planned"])
        chg = Ticket(
            "change_request", "", created, "Closed" if done else "Scheduled", sd,
            f"Planned work for {COMPANY}: {sd.lower()}.", "4 - Low", group, rng.choice(PEOPLE),
            vm.name, closed_at=work_end if done else None, close_code=code,
            close_notes=None if not done else (
                "Implemented as planned; checks passed." if code == "successful" else
                "Implemented with issues; see the change task notes."
                if code == "successful_issues" else "Backed out; rescheduled."),
            change_type=_mix(rng, p["type_mix"]), planned_start=start, planned_end=end)
        sim.tickets.append(chg)
        sim.tickets.append(Ticket(
            "change_task", "", created, "Closed" if done else "Open", f"Implement: {sd}",
            "Carry out the change in the window and record the result.", "4 - Low", group,
            chg.assigned_to, vm.name, closed_at=chg.closed_at,
            close_code=None if not done else "successful",
            close_notes=None if not done else "Done in the window.",
            planned_hours=planned,
            actual_hours=_hours(float(planned) * ratio) if done else None,
            change_request="", change_type="implementation", planned_start=start,
            planned_end=end))


def _ratio(rng: random.Random, q: Mapping[str, Any]) -> float:
    """Around p50, within [p10, p90] most of the time."""
    return float(q["p50"]) + rng.gauss(0, (float(q["p90"]) - float(q["p10"])) / 2.56)


_REQUESTS: Final = (
    "Snapshot {vm} before the application upgrade",
    "Open port 443 between {vnet} and the shared services network",
    "Create a local service account on {vm}",
    "Send the monthly patch compliance report",
    "Add {vm} to the weekend shutdown schedule",
    "Increase backup retention for {vm} to 90 days",
    "Provide read access to the {rg} resource group for the auditors",
    "Install the latest .NET runtime on {vm}",
)


def _requests(sim: Simulation, rng: random.Random) -> None:
    p = params()["sc_req_item"]
    n = round(params()["volumes_per_month"]["sc_req_item"] * DAYS / 30)
    end = datetime.combine(sim.anchor, time(23, 59, 59), UTC)
    for day in _days(rng, sim, n, params()["incident"]["weekday_weights"]):
        opened = _opened(rng, day, params()["incident"])
        vm = _pick(rng, sim.vms, opened)
        closed: datetime | None = opened + timedelta(
            hours=_lognormal(rng, p["open_to_close_hours_lognormal"]))
        state = _mix(rng, p["state_mix"])
        if closed is not None and closed > end:
            closed, state = None, "Work in Progress"
        sd = rng.choice(_REQUESTS).format(vm=vm.name, vnet=vm.vnet, rg=vm.resource_group)
        sim.tickets.append(Ticket(
            "sc_req_item", "", opened, state, sd, f"Requested through the portal: {sd}.",
            "5 - Planning", GROUPS[0], rng.choice(PEOPLE), vm.name if "{vm}" in sd else None,
            closed_at=closed, sla_breached=(None if closed is None else
                                            rng.random() < float(p["sla_breach_rate"])),
            actual_hours=(None if closed is None else
                          _hours(_lognormal(rng, p["actual_hours_lognormal"]))),
            contact_type=_mix(rng, p["contact_type_mix"])))


_PROJECT: Final = (
    "Landing zone: apply the tagging policy to {rg}",
    "Onboard {rg} to monitoring and alert rules",
    "Design the backup policy for {rg}",
    "Patch baseline and maintenance windows for {rg}",
    "Right-size review for {rg}",
    "Document the network layout of {vnet}",
)


def _project_tasks(sim: Simulation, rng: random.Random) -> None:
    p = params()["pm_project_task"]
    n = round(params()["volumes_per_month"]["pm_project_task"] * DAYS / 30)
    for day in _days(rng, sim, n, params()["incident"]["weekday_weights"]):
        opened = _opened(rng, day, params()["incident"])
        vm = _pick(rng, sim.vms, opened)
        planned = _hours(_lognormal(rng, p["planned_hours_lognormal"]))
        closed = opened + timedelta(days=rng.randrange(1, 10))
        done = closed.date() <= sim.anchor
        sim.tickets.append(Ticket(
            "pm_project_task", "", opened, "Closed Complete" if done else "Work in Progress",
            rng.choice(_PROJECT).format(rg=vm.resource_group, vnet=vm.vnet),
            "Part of the managed service onboarding project.", "4 - Low", GROUPS[0],
            rng.choice(PEOPLE), None, closed_at=closed if done else None,
            planned_hours=planned,
            actual_hours=_hours(float(planned) * _ratio(rng, p["actual_over_planned"]))
            if done else None))


def _triage(sim: Simulation) -> None:
    """The fixed open incidents, opened over the last day before the anchor."""
    base = datetime.combine(sim.anchor, time(6, 0), UTC)
    for i, b in enumerate(bind(sim.vms)):
        t = b.incident
        sim.tickets.append(Ticket(
            "incident", t.number, base + timedelta(minutes=29 * i), t.state,
            b.short_description, b.description, t.priority, GROUPS[0], None, b.ci,
            triage=t))


def _number(sim: Simulation) -> None:
    """ServiceNow numbers rise with time; a change task carries its change's."""
    prefix = {"incident": "INC", "change_request": "CHG", "change_task": "CTASK",
              "sc_req_item": "RITM", "pm_project_task": "PRJTASK"}
    seq: dict[str, int] = {}
    out: list[Ticket] = []
    parent = ""
    for t in sim.tickets:  # creation order: a change is followed by its task
        if t.number:
            out.append(t)
            continue
        seq[t.table] = seq.get(t.table, 0) + 1
        number = f"{prefix[t.table]}{NUMBER_BASE + seq[t.table]:07d}"
        if t.table == "change_request":
            parent = number
        changes: dict[str, Any] = {"number": number}
        if t.table == "change_task":
            changes["change_request"] = parent
        out.append(replace(t, **changes))
    # Close notes that point at a parent incident name a real earlier one.
    incidents = [t for t in out if t.table == "incident" and t.triage is None]
    for i, t in enumerate(out):
        if t.close_notes and "{parent}" in t.close_notes:
            earlier = [x for x in incidents if x.opened_at < t.opened_at]
            ref = earlier[-1].number if earlier else "an earlier incident"
            out[i] = replace(t, close_notes=t.close_notes.replace("{parent}", ref))
    sim.tickets = out


def build(anchor: date, seed: int, estate: Estate) -> Simulation:
    sim = Simulation(anchor, seed, estate.subscription_id, estate.subscription_name,
                     vms(estate, seed))
    _incidents(sim, random.Random(f"{seed}:incident"))  # noqa: S311 - simulated data
    _changes(sim, random.Random(f"{seed}:change"))  # noqa: S311
    _requests(sim, random.Random(f"{seed}:request"))  # noqa: S311
    _project_tasks(sim, random.Random(f"{seed}:project"))  # noqa: S311
    _number(sim)
    _triage(sim)
    return sim
