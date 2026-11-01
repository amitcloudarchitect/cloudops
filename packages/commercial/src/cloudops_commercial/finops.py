"""FinOps calculations (HLD M10): charge classes, invoice reconciliation, anomalies
and allocation. Pure; ``cloudops_connectors.finops`` runs them over the database.

**Charge classes.** Every cost row carries one. ONLY ``consumption`` feeds margin
(step 16; HLD D8: licence spend is excluded from consumption and reported
separately). Reservation and savings-plan purchases, marketplace, support, tax
and credits are shown, never folded into the consumption the base pack and
CoreStack governance price on.

**Reconciliation** (HLD principle 1: "a figure that cannot be reconciled is not
shown"). The ledger (our line items) is compared with an independent reference
total: daily, the provider's own month-to-date total; at close, the issued
invoice. Drift above 0.5% marks the figure *unverified* and raises an alert; it
is never adjusted to match. Different currencies, or no reference yet, are not
verified either.

**Anomalies** (step 16 decision). Day over day: a day's cost against the mean of
the seven days before it. Month over month: the month-to-date run rate
(projected to the whole month) against last month's total. Thresholds are data,
per tenant with a platform default; both a ratio and an absolute floor in the
account's currency must be crossed. Drops are recorded too; spikes alert.

**Allocation.** Cost to resource (the connector links it), resource to tag, tag to
application and cost centre. Tag keys arrive in every spelling (``CostCenter``,
``cost-centre``, ``Cost Centre``): keys are compared normalised, values never
rewritten. A row with no resource, or whose resource lacks the tag, is
*unallocated*, a first-class figure rather than a remainder.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal

ZERO: Final = Decimal(0)

CHARGE_CLASSES: Final = ("consumption", "licence", "marketplace", "support", "reservation",
                         "savings_plan", "tax", "credit", "other")
FEEDS_MARGIN: Final = frozenset({"consumption"})

# --- reconciliation ------------------------------------------------------------------------

DRIFT_TOLERANCE: Final = Decimal("0.005")  # not money: a ratio, 0.5% (HLD, LLD SLO)

Verification = Literal["verified", "unverified"]


@dataclass(frozen=True, slots=True)
class Reconciled:
    ledger: Decimal
    reference: Decimal
    drift: Decimal | None  # |ledger - reference| / |reference|; None: not comparable
    status: Verification
    reason: str | None  # why unverified


def reconcile(ledger: Decimal, ledger_currency: str, reference: Decimal,
              reference_currency: str) -> Reconciled:
    if ledger_currency != reference_currency:
        return Reconciled(ledger, reference, None, "unverified", "currency_mismatch")
    if reference == 0:
        if ledger == 0:
            return Reconciled(ledger, reference, ZERO, "verified", None)
        return Reconciled(ledger, reference, None, "unverified", "reference_zero")
    drift = abs(ledger - reference) / abs(reference)
    if drift > DRIFT_TOLERANCE:
        return Reconciled(ledger, reference, drift, "unverified", "drift")
    return Reconciled(ledger, reference, drift, "verified", None)


# --- anomalies -----------------------------------------------------------------------------

Window = Literal["day", "month"]
# The platform's own resources (ADR 0031): a resource in a platform resource group,
# by the LLD's naming, the platform's groups and the node group AKS makes for its
# cluster. A PostgreSQL regular expression on the lowercased ARM id.
PLATFORM_GROUP: Final = r"/resourcegroups/(mc_)?rg-cloudops-[^/]+(/|$)"

TRAILING_DAYS: Final = 7
MIN_BASELINE_DAYS: Final = 3


@dataclass(frozen=True, slots=True)
class Threshold:
    min_ratio: Decimal  # relative change, e.g. 0.30
    min_amount: Decimal  # absolute change, in the account's currency


@dataclass(frozen=True, slots=True)
class Anomaly:
    window: Window
    observed_on: date
    observed: Decimal
    baseline: Decimal
    change: Decimal  # observed - baseline
    ratio: Decimal | None  # change / baseline; None when the baseline is zero
    direction: Literal["spike", "drop"]


def _crosses(observed: Decimal, baseline: Decimal, t: Threshold) -> Decimal | None:
    """The ratio if both thresholds are crossed (either direction), else None.
    A zero baseline counts as crossed when the absolute floor is."""
    change = observed - baseline
    if abs(change) < t.min_amount:
        return None
    if baseline == 0:
        return ZERO if change != 0 else None
    ratio = change / abs(baseline)
    return ratio if abs(ratio) >= t.min_ratio else None


def day_over_day(daily: Mapping[date, Decimal], day: date, t: Threshold) -> Anomaly | None:
    """``day`` against the mean of the up-to-seven days before it present in
    ``daily`` (cost days, so a gap is a day without cost, read as zero)."""
    if day not in daily:
        return None
    before = sorted(d for d in daily if d < day)[-TRAILING_DAYS:]
    if len(before) < MIN_BASELINE_DAYS:
        return None
    baseline = sum((daily[d] for d in before), ZERO) / len(before)
    observed = daily[day]
    if _crosses(observed, baseline, t) is None:
        return None
    change = observed - baseline
    return Anomaly("day", day, observed, baseline, change,
                   change / baseline if baseline else None,
                   "spike" if change > 0 else "drop")


def month_over_month(month_to_date: Decimal, through: date, days_elapsed: int,
                     days_in_month: int, last_month_total: Decimal | None,
                     t: Threshold) -> Anomaly | None:
    """The month-to-date run rate projected to the whole month, against last
    month's total. None without a last month to compare with."""
    if last_month_total is None or days_elapsed <= 0:
        return None
    projected = month_to_date / days_elapsed * days_in_month
    if _crosses(projected, last_month_total, t) is None:
        return None
    change = projected - last_month_total
    return Anomaly("month", through, projected, last_month_total, change,
                   change / last_month_total if last_month_total else None,
                   "spike" if change > 0 else "drop")


# --- allocation ----------------------------------------------------------------------------

def norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


# Normalised tag keys per dimension (LLD, Tagging: ``application``, ``costcentre``).
APPLICATION_KEYS: Final = frozenset({"application", "app", "applicationname", "appname"})
COST_CENTRE_KEYS: Final = frozenset({"costcentre", "costcenter", "cc", "costcode"})


@dataclass(frozen=True, slots=True)
class Allocation:
    application: str | None
    cost_centre: str | None


def _first(tags: Mapping[str, object], keys: frozenset[str]) -> str | None:
    for key in sorted(tags):
        value = tags[key]
        if norm_key(key) in keys and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def allocate(*tag_sets: Mapping[str, object] | None) -> Allocation:
    """The first tag set that names a dimension wins, per dimension: pass the
    cost row's snapshot first, then the resource's current tags."""
    app = cc = None
    for tags in tag_sets:
        if not tags:
            continue
        app = app or _first(tags, APPLICATION_KEYS)
        cc = cc or _first(tags, COST_CENTRE_KEYS)
    return Allocation(app, cc)


@dataclass(frozen=True, slots=True)
class Totals:
    allocated: Decimal
    unallocated: Decimal

    @property
    def unallocated_share(self) -> Decimal | None:
        whole = self.allocated + self.unallocated
        return None if whole == 0 else self.unallocated / whole


def unallocated(rows: Sequence[tuple[Allocation | None, Decimal]],
                dimension: Literal["application", "cost_centre"]) -> Totals:
    """Rows are (allocation, amount) in one currency; ``None``: no resource."""
    alloc = unalloc = ZERO
    for a, amount in rows:
        if a is not None and getattr(a, dimension) is not None:
            alloc += amount
        else:
            unalloc += amount
    return Totals(alloc, unalloc)
