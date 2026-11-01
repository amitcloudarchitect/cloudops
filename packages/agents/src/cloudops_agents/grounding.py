"""The deterministic number check behind every drafted or answered text.

A model's text may only quote figures it was given. Every number in the text must
appear in the evidence, as given or rounded to 0-2 places; a computed number (a
sum, a difference) is ungrounded. The assistant (``assistant.ungrounded_numbers``)
also accepts a fraction quoted as a percentage; the report narrator
(``drafting.numbers_grounded``) does not.

Dates, times and identifiers are not figures: the minutes of a timestamp must not
ground a made-up "47". They are taken out of the evidence and the text before
numbers are read, and one quoted in the text must itself appear in the evidence,
as written or shortened (a date, or a time to the minute, of a timestamp).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

_NUMBER: Final = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
_ZONE: Final = r"(?:Z|[+-]\d{2}(?::?\d{2})?)?"
_TIME: Final = rf"\d{{1,2}}:\d{{2}}(?::\d{{2}}(?:\.\d+)?)?{_ZONE}"
_STAMP: Final = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    rf"|\b(?P<date>\d{{4}}-\d{{2}}-\d{{2}})(?:[T ](?P<time>{_TIME}))?(?!\w)"
    rf"|(?<![\w:.])(?P<clock>{_TIME})(?![\w:])",
    re.IGNORECASE)
_IDENTIFIER: Final = re.compile(r"\b[A-Z]{2,8}\d{3,12}\b")


def _stamps(value: str) -> tuple[str, set[str]]:
    """The text without its dates, times and identifiers, and those stamps (each
    timestamp also as its date and its time) as written."""
    found: set[str] = set()
    for m in _STAMP.finditer(value):
        found.add(m.group().lower().replace(" ", "t"))
        for part in ("date", "time"):
            if m.group(part):
                found.add(m.group(part).lower())
    return _IDENTIFIER.sub(" ", _STAMP.sub(" ", value)), found


def _numbers(value: str) -> list[Decimal]:
    out: list[Decimal] = []
    for match in _NUMBER.findall(value):
        try:
            out.append(Decimal(match.replace(",", "")))
        except InvalidOperation:
            continue
    return out


def ungrounded_numbers(text: str, evidence: Iterable[str], *,
                       percentages: bool = False) -> list[str]:
    """Numbers, dates and times in ``text`` that appear nowhere in the evidence.
    With ``percentages``, a fraction in the evidence also grounds its percentage."""
    known: set[Decimal] = set()
    stamps: set[str] = set()
    for item in evidence:
        item, found = _stamps(item)
        stamps |= found
        for n in _numbers(item):
            for v in (n, n * 100) if percentages else (n,):
                known.add(v.normalize())
                for places in (0, 1, 2):
                    known.add(v.quantize(Decimal(1).scaleb(-places),
                                         rounding=ROUND_HALF_UP).normalize())
    text, quoted = _stamps(text)
    return sorted({str(n) for n in _numbers(text) if n.normalize() not in known}
                  | {q for q in quoted if not any(s.startswith(q) for s in stamps)})
