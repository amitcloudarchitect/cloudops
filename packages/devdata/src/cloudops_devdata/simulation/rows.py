"""The simulation as platform rows for one tenant. Pure.

    data = platform_rows(sim, tenant_id)

The tenant id is a parameter, never a constant: the loader looks the tenant up
by its short code and passes whatever id it has (``load.py``), so a tenant
onboarded elsewhere (the console's customer mapping, ADR 0015) is the one used.

Money follows CLAUDE.md rule 5: ``Decimal`` at numeric(18,6), each amount stored
in its transaction currency with the currency beside it. The contract is in EUR;
Azure bills this subscription in USD. Nothing is converted here: conversion
belongs to the serving layer, at the rate in force on the transaction date.

The rate card is the platform's own (``cloudops_commercial.ratecard``, versioned
and effective-dated): margin for this customer is priced on it like any other.
The customer's own commercial terms (revenue, target margin, consumption uplift,
professional services) are on its SOW.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from cloudops_devdata.simulation.model import (
    COMPANY,
    DAYS,
    NAMESPACE,
    QUALYS_TAG,
    Simulation,
    Ticket,
    Vm,
    params,
)
from cloudops_devdata.synthetic import (
    _QIDS,
    AccountRow,
    BillingTotalRow,
    CostRow,
    ResourceRow,
    SowRow,
    TenantAliasRow,
    VulnerabilityRow,
    WorkItemRow,
    money,
)

CONFIRMED_BY: Final = "simulation:local-dev"
# Relative VM price weights by size family (a D4 costs about twice a D2): enough
# to spread the month's consumption across VMs plausibly. Not prices.
_VCPU: Final = {"1": 1, "2": 2, "4": 4, "8": 8, "16": 16}


def _id(*parts: object) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, ":".join(str(p) for p in parts))


@dataclass
class PlatformRows:
    tenant_id: uuid.UUID
    aliases: list[TenantAliasRow] = field(default_factory=list)
    sows: list[SowRow] = field(default_factory=list)
    accounts: list[AccountRow] = field(default_factory=list)
    resources: list[ResourceRow] = field(default_factory=list)
    work_items: list[WorkItemRow] = field(default_factory=list)
    vulnerabilities: list[VulnerabilityRow] = field(default_factory=list)
    costs: list[CostRow] = field(default_factory=list)
    billing_totals: list[BillingTotalRow] = field(default_factory=list)

    def by_table(self) -> dict[str, list[Any]]:
        """In load order (foreign keys first), keyed by table. Findings are not
        here: they reach the platform through the Qualys connector (``qualys.py``)."""
        return {
            "cloudops.tenant_alias": self.aliases,
            "cloudops.sow": self.sows,
            "cloudops.cloud_account": self.accounts,
            "cloudops.resource": self.resources,
            "cloudops.work_item": self.work_items,
            "cloudops.cost_fact": self.costs,
            "cloudops.billing_total": self.billing_totals,
        }


def _vcpus(size: str) -> int:
    """Standard_D4s_v5 -> 4; Standard_DS1_v2 -> 1. Unknown -> 2."""
    body = size.removeprefix("Standard_")
    digits = "".join(c for c in body.split("_")[0] if c.isdigit())
    return _VCPU.get(digits, 2)


def platform_rows(sim: Simulation, tenant: uuid.UUID,
                  account_id: uuid.UUID | None = None) -> PlatformRows:
    """``account_id``: the id of an existing account for the subscription, if
    the tenant already has one."""
    out = PlatformRows(tenant)
    since = datetime.combine(sim.start, time.min, UTC) - timedelta(days=200)
    for source, alias in (("servicenow", COMPANY), ("qualys", COMPANY),
                          ("azure", sim.subscription_id)):
        out.aliases.append(TenantAliasRow(tenant, source, alias, Decimal("1.00"),
                                          CONFIRMED_BY, since))
    _sow(sim, out)
    account = AccountRow(account_id or _id("account", sim.subscription_id), tenant, "azure",
                         sim.subscription_id, sim.subscription_name, "nonprod", "westeurope",
                         "healthy", str(params()["commercial"]["billing_currency"]))
    out.accounts.append(account)
    resources = {v.name: _resource(sim, v, account) for v in sim.vms}
    out.resources.extend(resources.values())
    out.work_items.extend(_work_item(t, tenant, resources) for t in sim.tickets if t.kind)
    _vulnerabilities(sim, out, resources)
    _costs(sim, out, account, resources)
    _billing_totals(sim, out, account)
    return out


def _sow(sim: Simulation, out: PlatformRows) -> None:
    c = params()["commercial"]
    revenue = money(c["monthly_manage_revenue"])
    start = date(sim.start.year, 1, 1)
    out.sows.append(SowRow(
        _id("sow", sim.subscription_id), out.tenant_id, f"SOW-AZSPONSOR-{start.year}-01", start,
        None, str(c["contract_currency"]), revenue, hosting_acv=None,
        manage_acv=money(revenue * 12), professional_services=money(c["professional_services"]),
        contracted_ci_count=len(sim.vms), contracted_slab="S1", coverage="8x5",
        support="Silver", target_margin=Decimal(c["target_margin"]),
        consumption_uplift=Decimal(c["consumption_uplift"])))


def _resource(sim: Simulation, v: Vm, account: AccountRow) -> ResourceRow:
    seen = max(v.created, datetime.combine(sim.start, time.min, UTC) - timedelta(days=30))
    return ResourceRow(
        _id("resource", v.name), account.tenant_id, account.id, v.native_id(sim.subscription_id),
        v.name, None, v.ips, "Microsoft.Compute/virtualMachines", "compute.vm", v.region,
        v.os_version, True,
        {"resource_group": v.resource_group, "vnet": v.vnet, "size": v.size,
         "source": "simulation"},
        "running" if v.running else "deallocated", seen,
        datetime.combine(sim.anchor, time(23, 0), UTC), None,
        str(_id("cmdb_ci", v.name)).replace("-", ""), None)


def _work_item(t: Ticket, tenant: uuid.UUID, resources: Mapping[str, ResourceRow]) -> WorkItemRow:
    if t.kind is None:
        raise ValueError(f"{t.table} is not mirrored as a work item")
    done = t.closed_at or t.resolved_at
    resolve = None if t.resolved_at is None else int((t.resolved_at - t.opened_at).total_seconds())
    duration = None if done is None else int((done - t.opened_at).total_seconds())
    return WorkItemRow(
        tenant_id=tenant, kind=t.kind, number=t.number, change_request=t.change_request,
        company_raw=COMPANY, opened_at=t.opened_at, closed_at=t.closed_at,
        priority=t.priority or "4 - Low",
        state=t.state, assigned_to=t.assigned_to, assignment_workgroup=t.assignment_group,
        short_description=t.short_description, configuration_item=t.ci,
        resource_id=resources[t.ci].id if t.ci and t.ci in resources else None,
        change_type=t.change_type, resolve_sla=None, response_sla=None,
        made_sla=None if t.sla_breached is None else not t.sla_breached,
        sla_breached=t.sla_breached, duration_seconds=duration,
        resolve_time_seconds=resolve, pending_total_seconds=t.pending_seconds,
        planned_hours=t.planned_hours, actual_hours=t.actual_hours, closure_code=t.close_code,
        close_notes=t.close_notes)


def _per_asset(rng: random.Random, q: Mapping[str, Any]) -> int:
    """A count with the given 10th, 50th and 90th percentiles (piecewise)."""
    u = rng.random()
    lo, mid, hi = int(q["p10"]), int(q["p50"]), int(q["p90"])
    if u < 0.5:
        return max(1, round(lo + (mid - lo) * u / 0.5))
    return max(1, round(mid + (hi - mid) * (u - 0.5) / 0.5))


def _vulnerabilities(sim: Simulation, out: PlatformRows,
                     resources: Mapping[str, ResourceRow]) -> None:
    """Findings as the Qualys connector would store them; ``qualys.py`` renders
    the same findings as the connector's recorded-response format."""
    p = params()["vulnerability"]
    rng = random.Random(f"{sim.seed}:vulnerability")  # noqa: S311 - simulated data
    end = datetime.combine(sim.anchor, time(4, 0), UTC)
    seen: set[tuple[int, str, datetime]] = set()
    for v in sim.vms:
        family = "windows" if v.os_family == "Windows" else "linux"
        r = resources[v.name]
        scopes = [("os", _per_asset(rng, p["os_per_asset"]))]
        if rng.random() < float(p["app_asset_share"]):
            scopes.append(("application", _per_asset(rng, p["app_per_asset"])))
        for scope, count in scopes:
            mix = p["os_severity_mix"] if scope == "os" else p["app_severity_mix"]
            for _ in range(count):
                severity = int(rng.choices(list(mix), weights=[float(w) for w in mix.values()])[0])
                candidates = [q for q in _QIDS if q[3] == scope and q[4] in (family, "any")]
                exact = [q for q in candidates if q[2] == severity]
                qid, title, sev, _scope, _family = rng.choice(exact or candidates)
                first = end - timedelta(days=rng.randrange(3, DAYS), hours=rng.randrange(24))
                key = (qid, v.name.lower(), first)
                if key in seen:
                    continue
                seen.add(key)
                fixed = (first + timedelta(days=rng.randrange(1, 30))
                         if rng.random() < float(p["remediated_share"]["value"]) else None)
                if fixed is not None and fixed >= end:
                    fixed = None
                last = fixed or end
                out.vulnerabilities.append(VulnerabilityRow(
                    out.tenant_id, qid, v.name.lower(), v.private_ip, r.id, scope, title, sev,
                    v.os_version, "Apply the vendor update referenced in the advisory.",
                    "An attacker may exploit this to compromise the host.",
                    "Potential" if rng.random() < float(p["potential_share"]) else "Confirmed",
                    [QUALYS_TAG, f"RG:{v.resource_group}"], first, last, fixed,
                    Decimal((last - first).days)))


def _costs(sim: Simulation, out: PlatformRows, account: AccountRow,
           resources: Mapping[str, ResourceRow]) -> None:
    """Daily consumption in the billing currency, sized so a month's total is
    the workbook's revenue-to-consumption ratio applied to this SOW's revenue
    (in the contract currency: a ratio of magnitudes, not a conversion)."""
    c = params()["commercial"]
    rng = random.Random(f"{sim.seed}:cost")  # noqa: S311 - simulated data
    monthly = Decimal(c["monthly_manage_revenue"]) / Decimal(c["revenue_to_consumption"])
    # Running VMs bill compute and disk; deallocated ones only disk.
    weights = {v.name: Decimal(_vcpus(v.size) * (4 if v.running else 1) + v.disks)
               for v in sim.vms}
    total = sum(weights.values(), Decimal(0))
    jitter = float(c["consumption_day_jitter"])
    for i in range(DAYS):
        d = sim.start + timedelta(days=i)
        for v in sim.vms:
            if v.created.date() > d:
                continue
            share = monthly / 30 * weights[v.name] / total
            factor = Decimal(str(round(1 + rng.uniform(-jitter, jitter), 4)))
            compute = money(share * factor * Decimal("0.8")) if v.running else money(0)
            disk = money(share * factor * (Decimal("0.2") if v.running else Decimal(1)))
            r = resources[v.name]
            for service, canonical, amount in (("Virtual Machines", "compute", compute),
                                               ("Storage", "storage", disk)):
                if amount:
                    out.costs.append(CostRow(
                        out.tenant_id, account.id, r.id, d, f"{d:%Y-%m}", service, canonical,
                        "consumption", amount, amount, account.billing_currency,
                        {"resource_group": v.resource_group}))


def _billing_totals(sim: Simulation, out: PlatformRows, account: AccountRow) -> None:
    """The provider's month-to-date total per period, agreeing with the line
    items, and the invoice for each closed month: reconciliation verifies."""
    sums: dict[str, Decimal] = {}
    last: dict[str, date] = {}
    for c in out.costs:
        sums[c.invoice_period] = sums.get(c.invoice_period, Decimal(0)) + c.unblended_cost
        last[c.invoice_period] = max(last.get(c.invoice_period, c.usage_date), c.usage_date)
    current = f"{sim.anchor:%Y-%m}"
    for period, total in sorted(sums.items()):
        out.billing_totals.append(BillingTotalRow(
            out.tenant_id, account.id, "azure", "provider_total", period, last[period], total,
            account.billing_currency, None))
        issued = (date.fromisoformat(f"{period}-01") + timedelta(days=32)).replace(day=3)
        if period != current and issued <= sim.anchor:
            out.billing_totals.append(BillingTotalRow(
                out.tenant_id, account.id, "azure", "invoice", period, issued, total,
                account.billing_currency, f"INV-SIM-{period}"))
