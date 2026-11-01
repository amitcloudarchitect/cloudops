"""The simulation as ServiceNow System Import Sets files, one CSV per target table.

    files = import_files(sim)          # {file name: CSV text}, in load order

Column names are the target tables' field names, so a transform map's "Auto
Map Matching Fields" maps them. ``priority`` rides beside ``impact`` and
``urgency`` because history is imported with business rules off, which would
otherwise compute it (``docs/simulation/README.md`` lists each map,
its coalesce field and the load order). Choice fields carry choice *values*
(``state`` 7, ``impact`` 2) and reference fields the referenced record's display
value (``company`` "Azure Sponsorship", ``cmdb_ci`` "vm-dc-ea-01"). Times are UTC,
``yyyy-MM-dd HH:mm:ss``. Columns starting ``u_`` are fields the platform's
connector reads from the production instance; map them only where the target
field exists.

Every task record carries ``correlation_display`` = ``cloudops-simulation`` and
a ``correlation_id`` naming it (``SIM-AZSPONSOR-<number>``), every CI a
``correlation_id``, and the company and groups the marker in their notes or
description, so the records can be found, and removed, as a set.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from cloudops_devdata.simulation.model import (
    COMPANY,
    CORRELATION_DISPLAY,
    GROUPS,
    SIM_MARKER,
    Simulation,
    Ticket,
    Vm,
)

TIME: Final = "%Y-%m-%d %H:%M:%S"

# priority -> (impact, urgency), the base instance's priority lookup
PRIORITY: Final = {"1 - Critical": (1, 1), "2 - High": (1, 2), "3 - Moderate": (2, 2),
                   "4 - Low": (2, 3), "5 - Planning": (3, 3)}
STATE: Final = {
    "incident": {"New": 1, "In Progress": 2, "On Hold": 3, "Resolved": 6, "Closed": 7,
                 "Canceled": 8},
    "change_request": {"Scheduled": -2, "Closed": 3},
    "change_task": {"Open": 1, "Closed": 3},
    "sc_req_item": {"Work in Progress": 2, "Closed Complete": 3, "Closed Incomplete": 4,
                    "Closed Skipped": 7},
    "pm_project_task": {"Work in Progress": 2, "Closed Complete": 3},
}
CONTACT: Final = {"self-service": "self-service", "email": "email", "proactive": "monitoring"}


def _t(moment: datetime | None) -> str:
    return "" if moment is None else moment.strftime(TIME)


def _h(value: Decimal | None) -> str:
    return "" if value is None else str(value)


def _b(value: bool | None) -> str:
    return "" if value is None else str(value).lower()


def _csv(header: Sequence[str], rows: Sequence[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(header), lineterminator="\r\n",
                       quoting=csv.QUOTE_MINIMAL)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in header})
    return buf.getvalue()


def _task(t: Ticket) -> dict[str, Any]:
    return {
        "number": t.number, "company": COMPANY, "short_description": t.short_description,
        "description": t.description, "state": STATE[t.table][t.state],
        "assignment_group": t.assignment_group, "assigned_to": t.assigned_to or "",
        "opened_at": _t(t.opened_at), "closed_at": _t(t.closed_at),
        "close_notes": t.close_notes or "", "correlation_id": f"{SIM_MARKER}-{t.number}",
        "correlation_display": CORRELATION_DISPLAY,
    }


def company() -> str:
    return _csv(("name", "customer", "notes"), [{
        "name": COMPANY, "customer": "true",
        "notes": "Simulated managed-service customer for CloudOps end-to-end testing "
                 f"({CORRELATION_DISPLAY}). Remove with the rest of the simulation. "
                 f"[{SIM_MARKER}]"}])


def groups() -> str:
    return _csv(("name", "description", "active"), [
        {"name": g,
         "description": f"Simulated assignment group ({CORRELATION_DISPLAY}) [{SIM_MARKER}]",
         "active": "true"} for g in GROUPS])


def cis(sim: Simulation) -> str:
    def row(v: Vm) -> dict[str, Any]:
        public = f", public {v.public_ip}" if v.public_ip else ""
        return {
            "name": v.name, "company": COMPANY,
            "object_id": v.native_id(sim.subscription_id),
            "vm_inst_id": v.native_id(sim.subscription_id),
            "correlation_id": f"{SIM_MARKER}-{v.name}",
            "state": "on" if v.running else "off",
            "operational_status": 1 if v.running else 2, "install_status": 1,
            "ip_address": v.private_ip, "disks": v.disks,
            "short_description": (f"{v.size} in {v.location}, resource group "
                                  f"{v.resource_group}, network {v.vnet}{public}, "
                                  f"{v.os_version}"),
        }

    return _csv(("name", "company", "object_id", "vm_inst_id", "correlation_id", "state",
                 "operational_status", "install_status", "ip_address", "disks",
                 "short_description"), [row(v) for v in sim.vms])


def incidents(sim: Simulation) -> str:
    def row(t: Ticket) -> dict[str, Any]:
        impact, urgency = PRIORITY[t.priority or "4 - Low"]
        return {**_task(t), "impact": impact, "urgency": urgency,
                "priority": (t.priority or "4 - Low")[0], "cmdb_ci": t.ci or "",
                "contact_type": "monitoring" if "alert:" in t.short_description else "email",
                "resolved_at": _t(t.resolved_at), "close_code": t.close_code or "",
                "made_sla": _b(None if t.sla_breached is None else not t.sla_breached),
                "u_sla_breached": _b(t.sla_breached), "u_actual_hours": _h(t.actual_hours)}

    return _csv(("number", "company", "opened_at", "impact", "urgency", "priority", "state",
                 "short_description", "description", "cmdb_ci", "assignment_group",
                 "assigned_to", "contact_type", "resolved_at", "closed_at", "close_code",
                 "close_notes", "made_sla", "u_sla_breached", "u_actual_hours",
                 "correlation_id", "correlation_display"),
                [row(t) for t in sim.table("incident")])


def changes(sim: Simulation) -> str:
    def row(t: Ticket) -> dict[str, Any]:
        return {**_task(t), "type": t.change_type, "cmdb_ci": t.ci or "",
                "start_date": _t(t.planned_start), "end_date": _t(t.planned_end),
                "work_end": _t(t.closed_at), "close_code": t.close_code or ""}

    return _csv(("number", "company", "type", "opened_at", "state", "short_description",
                 "description", "cmdb_ci", "assignment_group", "assigned_to", "start_date",
                 "end_date", "work_end", "closed_at", "close_code", "close_notes",
                 "correlation_id", "correlation_display"),
                [row(t) for t in sim.table("change_request")])


def change_tasks(sim: Simulation) -> str:
    def row(t: Ticket) -> dict[str, Any]:
        return {**_task(t), "change_request": t.change_request, "cmdb_ci": t.ci or "",
                "change_task_type": t.change_type, "planned_start_date": _t(t.planned_start),
                "planned_end_date": _t(t.planned_end), "close_code": t.close_code or "",
                "u_planned_hours": _h(t.planned_hours), "u_actual_hours": _h(t.actual_hours)}

    return _csv(("number", "change_request", "company", "change_task_type", "opened_at",
                 "state", "short_description", "description", "cmdb_ci", "assignment_group",
                 "assigned_to", "planned_start_date", "planned_end_date", "closed_at",
                 "close_code", "close_notes", "u_planned_hours", "u_actual_hours",
                 "correlation_id", "correlation_display"),
                [row(t) for t in sim.table("change_task")])


def requested_items(sim: Simulation) -> str:
    def row(t: Ticket) -> dict[str, Any]:
        return {**_task(t), "priority": 5, "configuration_item": t.ci or "",
                "contact_type": CONTACT.get(t.contact_type or "", "email"),
                "u_sla_breached": _b(t.sla_breached), "u_actual_hours": _h(t.actual_hours)}

    return _csv(("number", "company", "opened_at", "priority", "state", "short_description",
                 "description", "configuration_item", "assignment_group", "assigned_to",
                 "contact_type", "closed_at", "u_sla_breached", "u_actual_hours",
                 "correlation_id", "correlation_display"),
                [row(t) for t in sim.table("sc_req_item")])


def project_tasks(sim: Simulation) -> str:
    def row(t: Ticket) -> dict[str, Any]:
        return {**_task(t), "u_planned_hours": _h(t.planned_hours),
                "u_actual_hours": _h(t.actual_hours)}

    return _csv(("number", "company", "opened_at", "state", "short_description", "description",
                 "assignment_group", "assigned_to", "closed_at", "u_planned_hours",
                 "u_actual_hours", "correlation_id", "correlation_display"),
                [row(t) for t in sim.table("pm_project_task")])


# (file name, target table, coalesce field, renderer), in load order: references first.
FILES: Final[tuple[tuple[str, str, str, Callable[[Simulation], str]], ...]] = (
    ("01_core_company.csv", "core_company", "name", lambda _: company()),
    ("02_sys_user_group.csv", "sys_user_group", "name", lambda _: groups()),
    ("03_cmdb_ci_vm_instance.csv", "cmdb_ci_vm_instance", "object_id", cis),
    ("04_incident.csv", "incident", "number", incidents),
    ("05_change_request.csv", "change_request", "number", changes),
    ("06_change_task.csv", "change_task", "number", change_tasks),
    ("07_sc_req_item.csv", "sc_req_item", "number", requested_items),
    ("08_pm_project_task.csv", "pm_project_task", "number", project_tasks),
)


def import_files(sim: Simulation) -> dict[str, str]:
    return {name: render(sim) for name, _table, _coalesce, render in FILES}
