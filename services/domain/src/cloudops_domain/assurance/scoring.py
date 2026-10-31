"""Assessment scoring (HLD M14).

Compliant scores 100, partially compliant 50, non-compliant 0. Manual and
not-applicable responses are excluded from the denominator. Only *approved*
responses score at all: a response without a person's approval is pending,
counted and shown, never scored (HLD design principle 7: "No evidence shows
'manual', never compliant").

The score is a percentage held as a ``Decimal`` and rounded once, here at the
boundary, to two places. With nothing scorable it is ``None``, never zero.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

POINTS: Final = {"compliant": 100, "partially_compliant": 50, "non_compliant": 0}
EXCLUDED: Final = frozenset({"manual", "not_applicable"})
RESULTS: Final = (*POINTS, *sorted(EXCLUDED))


@dataclass(frozen=True)
class ScoredResponse:
    control_code: str
    result: str | None  # the approved result; None until approved
    approved: bool


@dataclass(frozen=True)
class Score:
    percent: Decimal | None
    points: int
    scorable: int  # the denominator, in controls
    counts: dict[str, int]  # by approved result
    pending: int  # not yet approved: not scored
    excluded: int  # manual and not applicable


def score(responses: Iterable[ScoredResponse]) -> Score:
    counts: Counter[str] = Counter()
    pending = 0
    for r in responses:
        if not r.approved or r.result is None:
            pending += 1
            continue
        if r.result not in RESULTS:
            raise ValueError(f"{r.control_code}: unknown result {r.result!r}")
        counts[r.result] += 1
    scorable = sum(counts[k] for k in POINTS)
    points = sum(POINTS[k] * counts[k] for k in POINTS)
    percent = None
    if scorable:
        percent = (Decimal(points) / Decimal(scorable)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Score(percent, points, scorable, {k: counts[k] for k in RESULTS}, pending,
                 sum(counts[k] for k in EXCLUDED))
