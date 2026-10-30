"""The gateway's rate card: which deployments fill each tier, and what they cost.

Application code names a tier; this module is the only place that knows
which deployment, on which provider, serves it (LLD: "Application code names
a tier, never a model"). Order matters: the first deployment is primary, the
rest are fallbacks, tried on throttling or unavailability.

Prices are per thousand tokens, ``Decimal`` in a stated currency (CLAUDE.md
rule 5). The local prices below are placeholders of a realistic order of
magnitude, so quotas and the spend breaker have something to count; they are
not anyone's real price list.

Azure (ADR 0012): list prices from Microsoft's public Retail Prices API
(prices.azure.com), West Europe, Data Zone Standard, read 2026-10-30. In USD
because the API rounds EUR prices to too few decimals (the embedding price
reads 0.00 in EUR). List price, not Getronics' rate: contract pricing (CSP or
another channel) replaces these later. gpt-5.5 has a dearer long-context band
(input 0.011, output 0.0495 per 1K) that applies above its context threshold;
the gateway's prompts stay far below it, so the short-context price is used.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from cloudops_gateway.providers import Tier

MONEY: Final = Decimal("0.000001")

# The spend breaker's floor (ADR 0005): recent spend below this, in USD like the
# prices below, never trips it, so a quiet tenant's first calls cannot.
SPEND_BREAKER_FLOOR: Final = Decimal("5.000000")


@dataclass(frozen=True, slots=True)
class Deployment:
    provider: str
    alias: str
    price_in_per_1k: Decimal
    price_out_per_1k: Decimal
    currency: str = "USD"

    def cost(self, tokens_in: int, tokens_out: int) -> Decimal:
        raw = (Decimal(tokens_in) * self.price_in_per_1k
               + Decimal(tokens_out) * self.price_out_per_1k) / Decimal(1000)
        return raw.quantize(MONEY, rounding=ROUND_HALF_EVEN)


# Local: two mock providers per tier, so fallback is exercised exactly as in
# production (primary Azure OpenAI, secondary per the HLD).
LOCAL_DEPLOYMENTS: Final[Mapping[Tier, Sequence[Deployment]]] = {
    Tier.SMALL: (
        Deployment("mock", "cloudops-small", Decimal("0.000150"), Decimal("0.000600")),
        Deployment("mock-secondary", "cloudops-small", Decimal("0.000200"), Decimal("0.000800")),
    ),
    Tier.LARGE: (
        Deployment("mock", "cloudops-large", Decimal("0.002500"), Decimal("0.010000")),
        Deployment("mock-secondary", "cloudops-large", Decimal("0.003000"), Decimal("0.015000")),
    ),
    Tier.EMBED: (
        Deployment("mock", "cloudops-embed", Decimal("0.000020"), Decimal("0")),
        Deployment("mock-secondary", "cloudops-embed", Decimal("0.000020"), Decimal("0")),
    ),
}


# Azure: one deployment per tier, no secondary provider yet (ADR 0012); the
# gateway still falls back from large to small. Meter IDs from the Retail Prices
# API, so a price can be traced: small e378a12b / 69008474, large db528725 /
# f7f9d475, embed 08d886b7.
AZURE_DEPLOYMENTS: Final[Mapping[Tier, Sequence[Deployment]]] = {
    Tier.SMALL: (
        Deployment("azure-openai", "cloudops-small", Decimal("0.000825"), Decimal("0.004950")),
    ),
    Tier.LARGE: (
        Deployment("azure-openai", "cloudops-large", Decimal("0.005500"), Decimal("0.033000")),
    ),
    Tier.EMBED: (
        Deployment("azure-openai", "cloudops-embed", Decimal("0.000022"), Decimal("0")),
    ),
}
