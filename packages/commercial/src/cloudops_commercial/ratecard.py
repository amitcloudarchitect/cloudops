"""The rate card: versioned, effective-dated, and the one place a price lives.

LLD, CI/CD: "any monetary literal outside the rate card module" fails the build
(``tools/ci/check_money_literals.py``; this file is on its allowlist). HLD M11:
"versioned rate card". A version is immutable once published: a change of price
is a new ``RateCard`` with its own ``effective_from``, and the previous one gets
an ``effective_to``. A quote or a margin figure records the version it used, so
it can be reproduced after the card has moved on.

Version ``2026-v2.14`` is ``reference/pricing_engine.py``'s constants, which trace
to named ranges of ``Public_Cloud_Services_-_Pricing_Calculator_-_2026_-_V2_14.xlsx``.
They are copied digit for digit from the reference as ``Decimal`` strings, so
the port reproduces the workbook (``tests/test_pricing_reference.py``). Open
commercial items carried with the values, not resolved here (HLD, Decisions):

- D6: the 24x7 rows of ``service_multiplier`` carry an extra 1.1 uplift the
  16x5 and 8x5 rows do not. The workbook's values are authoritative until
  commercial confirms the published table or the computed values.
- D7: the Budapest people cost and its FX basis are awaiting Finance.
- The workbook has people cost for India and Budapest only. A ``Mixed``
  delivery location has none, so its people cost is *not captured*, never an
  invented blend.

Amounts are EUR (``currency``): the workbook's currency. Conversion to a
contract currency happens where the figure is used, at the rate in force.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal

Coverage = Literal["8x5", "16x5", "24x7"]
SupportModel = Literal["Bronze", "Silver", "Gold"]
Location = Literal["India", "Budapest"]
Complexity = int | Literal["Case"]


class RateCardError(LookupError):
    """No rate card version is in force on the date asked about."""


@dataclass(frozen=True, slots=True)
class Band:
    """A marginal consumption band: ``rate`` applies to spend inside it only."""

    lower: Decimal
    upper: Decimal | None  # None: unbounded
    rate: Decimal


@dataclass(frozen=True, slots=True)
class Slab:
    label: str
    lower: int
    upper: int
    transition_weeks: int


@dataclass(frozen=True, slots=True)
class HealthBand:
    """A margin below ``below`` (a fraction) falls in ``label``. Checked in order."""

    below: Decimal
    label: str


@dataclass(frozen=True, slots=True)
class RateCard:
    version: str
    effective_from: date
    effective_to: date | None  # exclusive; None: still in force
    source: str
    currency: str
    blended_day_rate: Decimal  # per 8-hour day
    minutes_per_day: Decimal
    complexity_minutes: Mapping[Complexity, Decimal]
    service_multiplier: Mapping[tuple[Coverage, SupportModel], Decimal]
    resource_complexity: Mapping[str, Complexity]
    # The platform's type_canonical -> the catalogue row that prices it. A type
    # with no row is an unpriced CI: counted and shown, never priced by guess.
    catalog_type_of: Mapping[str, str]
    base_pack_bands: tuple[Band, ...]
    base_pack_floor: Decimal
    base_pack_coverage_uplift: Mapping[Coverage, Decimal]
    ci_slabs: tuple[Slab, ...]
    tool_cost_per_ci: Mapping[str, Decimal]
    corestack_consumption_rate: Decimal
    people_cost: Mapping[Location, Decimal]  # per person per month, fully loaded
    working_days_per_month: Decimal
    hours_per_day: Decimal
    role_mix: Mapping[str, Decimal]
    default_tools: tuple[str, ...]
    # slab_headroom: repricing is due within max(minimum, share x slab width) CIs.
    repricing_headroom_minimum: int
    repricing_headroom_share: Decimal
    health_bands: tuple[HealthBand, ...]
    healthy_label: str

    def in_force_on(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None
                                               or day < self.effective_to)

    def health(self, margin: Decimal) -> str:
        """The band a gross margin fraction falls in (reference engine, HLD M12)."""
        for band in self.health_bands:
            if margin < band.below:
                return band.label
        return self.healthy_label


V2_14: Final = RateCard(
    version="2026-v2.14",
    effective_from=date(2026, 1, 1),
    effective_to=None,
    source="Public_Cloud_Services_-_Pricing_Calculator_-_2026_-_V2_14.xlsx",
    currency="EUR",
    # Pricing Catalog!B1
    blended_day_rate=Decimal("266.9932626632895"),
    minutes_per_day=Decimal("480"),
    # Pricing Catalog, cols N:O
    complexity_minutes={
        1: Decimal(3), 2: Decimal(5), 3: Decimal(6), 4: Decimal(10), 5: Decimal(25),
        6: Decimal(30), 7: Decimal(45), 8: Decimal(60), 9: Decimal(75), 10: Decimal(90),
        11: Decimal(120), "Case": Decimal(150),
    },
    # Pricing Catalog, cols Q:R (D6: the 24x7 rows' extra 1.1 uplift is the workbook's)
    service_multiplier={
        ("24x7", "Gold"): Decimal("5.544"), ("16x5", "Gold"): Decimal("3.96"),
        ("8x5", "Gold"): Decimal("3.6"),
        ("24x7", "Silver"): Decimal("3.542"), ("16x5", "Silver"): Decimal("2.53"),
        ("8x5", "Silver"): Decimal("2.3"),
        ("24x7", "Bronze"): Decimal("3.08"), ("16x5", "Bronze"): Decimal("2.2"),
        ("8x5", "Bronze"): Decimal("2.0"),
    },
    # Pricing Catalog col C. Azure/AWS equivalents share a row.
    resource_complexity={
        "AD Domain Services / AWS Managed Microsoft AD": "Case",
        "Microsoft Active Directory IaaS / AD on AWS EC2": "Case",
        "Microsoft Entra ID / AWS IAM Identity Center": 6,
        "Azure Virtual Machines / Amazon EC2": 3,
        "App Service / AWS Elastic Beanstalk": 3,
        "Azure Kubernetes Service (AKS) / Amazon EKS": 5,
        "File Sync / AWS DataSync": 3,
        "MS SQL DB / Azure SQL / Amazon RDS": 4,
        "Event Hubs / Amazon Kinesis": 3,
        "Azure Functions / AWS Lambda": 3,
        "Fileshare / Amazon FSx": 11,
        "Web Application Firewall / AWS WAF": 9,
        "Load Balancer / Elastic Load Balancing": 6,
        "ExpressRoute / AWS Direct Connect": 6,
        "Firewall / AWS Network Firewall": 3,
        "Azure CDN / Amazon CloudFront": 2,
        "Virtual WANs / AWS Transit Gateway": 4,
        "VPN Gateway / AWS Site-to-Site VPN": 6,
        "Application Gateway / AWS ALB": "Case",
        "Azure DevOps / AWS Developer Tools": "Case",
    },
    # Platform types with a catalogue row (step 16; for commercial to confirm).
    # storage.account and storage.disk have none in the workbook.
    catalog_type_of={
        "compute.vm": "Azure Virtual Machines / Amazon EC2",
        "web.app": "App Service / AWS Elastic Beanstalk",
        "compute.kubernetes": "Azure Kubernetes Service (AKS) / Amazon EKS",
        "database.sql": "MS SQL DB / Azure SQL / Amazon RDS",
        "compute.function": "Azure Functions / AWS Lambda",
        "network.load_balancer": "Load Balancer / Elastic Load Balancing",
        "network.firewall": "Firewall / AWS Network Firewall",
        "network.vpn_gateway": "VPN Gateway / AWS Site-to-Site VPN",
        "network.application_gateway": "Application Gateway / AWS ALB",
        "messaging.event_stream": "Event Hubs / Amazon Kinesis",
        "storage.file_share": "Fileshare / Amazon FSx",
    },
    # Base pack Calculation!A3:E11, applied marginally
    base_pack_bands=(
        Band(Decimal(0), Decimal(1_000), Decimal("0.0")),
        Band(Decimal(1_000), Decimal(5_000), Decimal("0.07")),
        Band(Decimal(5_000), Decimal(10_000), Decimal("0.055")),
        Band(Decimal(10_000), Decimal(25_000), Decimal("0.045")),
        Band(Decimal(25_000), Decimal(50_000), Decimal("0.03")),
        Band(Decimal(50_000), Decimal(75_000), Decimal("0.02")),
        Band(Decimal(75_000), Decimal(100_000), Decimal("0.015")),
        Band(Decimal(100_000), None, Decimal("0.0085")),
    ),
    base_pack_floor=Decimal("250.0"),  # Base pack Calculation!B36:B38
    base_pack_coverage_uplift={"8x5": Decimal("1.0"), "16x5": Decimal("1.1"),
                               "24x7": Decimal("1.4")},
    # Slabs!A3:A8
    ci_slabs=(
        Slab("0 - 50 CIs", 0, 50, 6),
        Slab("51 - 150 CIs", 51, 150, 8),
        Slab("151 - 300 CIs", 151, 300, 10),
        Slab("301 - 500 CIs", 301, 500, 12),
        Slab("501 - 1000 CIs", 501, 1000, 14),
        Slab("> 1001 CIs", 1001, 10**9, 18),
    ),
    # Platforms Cost!J5:M8 and the CoreStack consumption slab (K3)
    tool_cost_per_ci={"OpsRamp": Decimal("4.343"), "Qualys": Decimal("3.225"),
                      "CloudBolt": Decimal("7.00")},
    corestack_consumption_rate=Decimal("0.021"),
    # People Cost!B25:B27 / E25:E27 (D7: Budapest awaiting Finance)
    people_cost={"India": Decimal("1786.9079747305266"),
                 "Budapest": Decimal("7878.248133680555")},
    working_days_per_month=Decimal("18.1"),  # People Cost!B28
    hours_per_day=Decimal(8),
    # Base pack Calculation!G3:H8
    role_mix={
        "Full Stack Cloud Engineer": Decimal("0.45"),
        "Cloud Specialist": Decimal("0.35"),
        "Cloud Architect": Decimal("0.10"),
        "CloudOps Manager": Decimal("0.05"),
        "Cloud Delivery Manager": Decimal("0.05"),
    },
    default_tools=("OpsRamp", "Qualys", "CloudBolt"),
    repricing_headroom_minimum=10,
    repricing_headroom_share=Decimal("0.05"),
    # The reference engine's banding (HLD M12)
    health_bands=(
        HealthBand(Decimal("0.10"), "critical"),
        HealthBand(Decimal("0.20"), "at risk"),
        HealthBand(Decimal("0.30"), "watch"),
    ),
    healthy_label="healthy",
)

# Every published version, oldest first. Effective periods never overlap.
RATE_CARDS: Final[tuple[RateCard, ...]] = (V2_14,)


def rate_card_on(day: date, cards: tuple[RateCard, ...] = RATE_CARDS) -> RateCard:
    """The version in force on ``day``. Raises rather than guessing."""
    found = [c for c in cards if c.in_force_on(day)]
    if len(found) != 1:
        raise RateCardError(f"{len(found)} rate card versions in force on {day.isoformat()}")
    return found[0]


def rate_card(version: str, cards: tuple[RateCard, ...] = RATE_CARDS) -> RateCard:
    for c in cards:
        if c.version == version:
            return c
    raise RateCardError(f"no rate card version {version!r}")
