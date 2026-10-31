"""The pricing, effort and margin engine: a port of ``reference/pricing_engine.py``.

PORTED, not rewritten. The reference reproduces the published calculator
(``Public_Cloud_Services_-_Pricing_Calculator_-_2026_-_V2_14.xlsx``) exactly, so
every function keeps its name, its arithmetic and its order of operations. What
changed, and only this:

- ``float`` became ``Decimal`` (CLAUDE.md rule 5). The constants moved, digit for
  digit, into the versioned rate card (``ratecard.py``), and every function
  takes the ``RateCard`` it prices with, so a quote names the version it used.
- The reference's in-file regression run became ``tests/test_pricing_reference.py``,
  which checks the reference case to six decimal places as a CI release gate.
- ``margin`` raises ``SowNotCapturedError`` (a ``ValueError``) as before, and its
  two cost formulas are ``modelled_cost`` and ``actual_cost`` so the
  contract-currency margin (``margin.py``) uses the same arithmetic.

Design rules (the reference's):
  - No rates baked into business logic; all rates live in the rate card.
  - Every public function is pure. State lives in the caller.
  - Monetary values are rounded only at the presentation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from cloudops_commercial.ratecard import Coverage, Location, RateCard, SupportModel

TEAM_PLACES: Final = Decimal("0.001")  # the reference's round(fte * share, 3)
ZERO: Final = Decimal(0)


class SowNotCapturedError(ValueError):
    """The engagement has no SOW revenue: margin is not captured, never zero."""


# ---------------------------------------------------------------------------
# INPUTS / OUTPUTS
# ---------------------------------------------------------------------------

@dataclass
class Engagement:
    """Everything the engine needs about one customer engagement."""
    customer: str
    inventory: dict[str, int]          # resource type -> CI count
    monthly_consumption_eur: Decimal
    coverage: Coverage = "24x7"
    support_model: SupportModel = "Gold"
    location: Location = "India"
    tools: tuple[str, ...] = ("OpsRamp", "Qualys", "CloudBolt")
    corestack: bool = True
    # From the SOW. None means not yet captured.
    sow_monthly_revenue_eur: Decimal | None = None


@dataclass
class Quote:
    customer: str
    rate_card_version: str
    critical_cis: int
    slab: str
    service_cost_eur: Decimal          # effort-driven catalogue cost
    base_pack_eur: Decimal             # consumption-driven managed-service floor
    cloud_services_cost_eur: Decimal   # service + base pack
    platform_cost_eur: Decimal         # tooling
    total_monthly_eur: Decimal
    price_per_ci_eur: Decimal
    fte_count: Decimal
    team: dict[str, Decimal] = field(default_factory=dict)
    transition_weeks: int = 0
    transition_effort_days: int = 0


# ---------------------------------------------------------------------------
# CORE CALCULATIONS
# ---------------------------------------------------------------------------

def effort_minutes_for(resource_type: str, card: RateCard) -> Decimal:
    """Monthly effort minutes per CI, from the complexity score."""
    complexity = card.resource_complexity[resource_type]
    return card.complexity_minutes[complexity]


def unit_price(resource_type: str, coverage: Coverage, support: SupportModel,
               card: RateCard) -> Decimal:
    """Monthly catalogue price for one CI of this type."""
    minutes = effort_minutes_for(resource_type, card)
    base = minutes * (card.blended_day_rate / card.minutes_per_day)
    return base * card.service_multiplier[(coverage, support)]


def service_cost(inventory: dict[str, int], coverage: Coverage, support: SupportModel,
                 card: RateCard) -> Decimal:
    return sum((unit_price(rt, coverage, support, card) * n
                for rt, n in inventory.items() if n), ZERO)


def base_pack(consumption_eur: Decimal, coverage: Coverage, card: RateCard) -> Decimal:
    """Marginal-band percentage of consumption, plus the floor, uplifted for coverage."""
    total = ZERO
    for band in card.base_pack_bands:
        if consumption_eur <= band.lower:
            break
        top = consumption_eur if band.upper is None else min(consumption_eur, band.upper)
        total += (top - band.lower) * band.rate
    return (total + card.base_pack_floor) * card.base_pack_coverage_uplift[coverage]


def slab_for(ci_count: int, card: RateCard) -> tuple[str, int]:
    """Returns (slab label, transition weeks)."""
    for slab in card.ci_slabs:
        if slab.lower <= ci_count <= slab.upper:
            return slab.label, slab.transition_weeks
    return card.ci_slabs[-1].label, card.ci_slabs[-1].transition_weeks


def platform_cost(ci_count: int, consumption_eur: Decimal,
                  tools: tuple[str, ...], corestack: bool, card: RateCard) -> Decimal:
    licence = sum((card.tool_cost_per_ci[t] * ci_count for t in tools), ZERO)
    governance = consumption_eur * card.corestack_consumption_rate if corestack else ZERO
    return licence + governance


def fte_count(cloud_services_cost_eur: Decimal, location: Location, card: RateCard) -> Decimal:
    """Implied team size: service cost divided by fully loaded cost per head."""
    return cloud_services_cost_eur / card.people_cost[location]


def build_team(fte: Decimal, card: RateCard) -> dict[str, Decimal]:
    return {role: (fte * share).quantize(TEAM_PLACES, rounding=ROUND_HALF_EVEN)
            for role, share in card.role_mix.items()}


def quote(e: Engagement, card: RateCard) -> Quote:
    cis = sum(e.inventory.values())
    slab_label, weeks = slab_for(cis, card)
    svc = service_cost(e.inventory, e.coverage, e.support_model, card)
    bp = base_pack(e.monthly_consumption_eur, e.coverage, card)
    cloud_services = svc + bp
    platforms = platform_cost(cis, e.monthly_consumption_eur, e.tools, e.corestack, card)
    total = cloud_services + platforms
    fte = fte_count(cloud_services, e.location, card)
    return Quote(
        customer=e.customer,
        rate_card_version=card.version,
        critical_cis=cis,
        slab=slab_label,
        service_cost_eur=svc,
        base_pack_eur=bp,
        cloud_services_cost_eur=cloud_services,
        platform_cost_eur=platforms,
        total_monthly_eur=total,
        price_per_ci_eur=total / cis if cis else ZERO,
        fte_count=fte,
        team=build_team(fte, card),
        transition_weeks=weeks,
        transition_effort_days=weeks * 5,
    )


# ---------------------------------------------------------------------------
# MARGIN  (the part the workbook cannot do: modelled cost vs SOW revenue)
# ---------------------------------------------------------------------------

@dataclass
class MarginResult:
    customer: str
    revenue_eur: Decimal
    modelled_cost_eur: Decimal
    actual_cost_eur: Decimal | None
    gross_margin_pct: Decimal
    variance_vs_model_eur: Decimal | None
    health: str


def modelled_cost(fte: Decimal, platform_cost_eur: Decimal, location: Location,
                  card: RateCard) -> Decimal:
    """Implied FTE x loaded people cost + tooling."""
    return fte * card.people_cost[location] + platform_cost_eur


def day_rate(location: Location, card: RateCard) -> Decimal:
    """Loaded cost of one person-day."""
    return card.people_cost[location] / card.working_days_per_month


def actual_cost(actual_effort_hours: Decimal, platform_cost_eur: Decimal,
                location: Location, card: RateCard) -> Decimal:
    """Observed effort hours x loaded day rate / 8 + tooling."""
    return (actual_effort_hours / card.hours_per_day) * day_rate(location, card) \
        + platform_cost_eur


def margin(e: Engagement, q: Quote, card: RateCard,
           actual_effort_hours: Decimal | None = None) -> MarginResult:
    """
    Modelled cost  = implied FTE x loaded people cost + tooling.
    Actual cost    = observed effort hours x loaded day rate / 8 + tooling,
                     where effort hours come from ServiceNow (incident duration,
                     change tasks, project tasks, service requests).
    The gap between the two is where margin leaks.
    """
    if e.sow_monthly_revenue_eur is None:
        raise SowNotCapturedError("SOW revenue not captured")

    modelled = modelled_cost(q.fte_count, q.platform_cost_eur, e.location, card)

    actual = None
    variance = None
    if actual_effort_hours is not None:
        actual = actual_cost(actual_effort_hours, q.platform_cost_eur, e.location, card)
        variance = actual - modelled

    cost_basis = actual if actual is not None else modelled
    gm = (e.sow_monthly_revenue_eur - cost_basis) / e.sow_monthly_revenue_eur

    return MarginResult(
        customer=e.customer,
        revenue_eur=e.sow_monthly_revenue_eur,
        modelled_cost_eur=modelled,
        actual_cost_eur=actual,
        gross_margin_pct=gm,
        variance_vs_model_eur=variance,
        health=card.health(gm),
    )


@dataclass(frozen=True)
class SlabHeadroom:
    current_slab: str
    next_slab: str | None
    cis_until_next_slab: int | None
    repricing_due: bool


def slab_headroom(ci_count: int, card: RateCard) -> SlabHeadroom:
    """How close is this customer to a repricing boundary?"""
    label, _ = slab_for(ci_count, card)
    slabs = card.ci_slabs
    for i, slab in enumerate(slabs):
        if slab.label == label:
            nxt = slabs[i + 1].label if i + 1 < len(slabs) else None
            remaining = slab.upper - ci_count
            window = max(Decimal(card.repricing_headroom_minimum),
                         card.repricing_headroom_share * (slab.upper - slab.lower))
            return SlabHeadroom(
                current_slab=label,
                next_slab=nxt,
                cis_until_next_slab=remaining if nxt else None,
                repricing_due=bool(nxt) and remaining <= window,
            )
    raise ValueError("slab_for returned a label the rate card does not have")
