"""The gateway's rate card: which deployments fill each tier, and what they cost.

Application code names a tier; this module is the only place that knows
which deployment, on which provider, serves it (LLD: "Application code names
a tier, never a model"). Order matters: the first deployment is primary, the
rest are fallbacks, tried on throttling or unavailability.

Prices are per thousand tokens, ``Decimal`` in a stated currency (CLAUDE.md
rule 5). The local prices below are placeholders of a realistic order of
magnitude, so quotas and the spend breaker have something to count; they are
not anyone's real price list. Production values come with LLD open item L12
(the model per tier), recorded in an ADR.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from cloudops_gateway.providers import Tier

MONEY: Final = Decimal("0.000001")


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
