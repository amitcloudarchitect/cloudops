"""ECB euro foreign exchange reference rates -> ``cloudops.fx_rate``.

The ECB publishes, every TARGET business day around 16:00 CET, the euro's
reference rate against some thirty currencies: ``EUR -> X``. The history file
(``eurofxref-hist.xml``, or its 90-day sibling) holds every day, newest first:

    <Cube><Cube time="2026-10-30"><Cube currency="USD" rate="1.0850"/>...</Cube>...

Rates are parsed with ``Decimal`` only, never ``float``. Stored for the
configured currency set (EUR always included), every directed pair, because
``cloudops.fx_rate_on`` has no inverse fallback:

- ``EUR -> X`` as published; ``X -> EUR`` = 1 / rate; ``X -> Y`` = rate_Y /
  rate_X; all quantised to numeric(18,8) with ROUND_HALF_EVEN, once.
- ``average`` effective each ECB day: flows on that day convert at it (as the
  synthetic seed stores daily rates).
- ``closing`` effective on the last ECB business day of every *completed*
  month: one before the month of the file's newest day. The current month's
  closing appears on the first run after it ends.

A day's ``time`` that is not a date, a currency that is not three capitals,
and a rate that is not a positive decimal are rejected and counted; the rest
of the day still counts. The file is data (CLAUDE.md rule 4): it supplies
numbers and codes, nothing else is read from it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final

from cloudops_connectors.core.xmlrec import parse

SOURCE: Final = "ecb"
ENTITY: Final = "fx"
ENTITIES: Final = frozenset({ENTITY})
BASE: Final = "EUR"
DEFAULT_CURRENCIES: Final = ("EUR", "GBP", "USD")
RATE: Final = Decimal("0.00000001")  # numeric(18,8)
CURRENCY: Final = re.compile(r"[A-Z]{3}")
AVERAGE: Final = "average"
CLOSING: Final = "closing"


@dataclass(frozen=True)
class History:
    days: dict[date, dict[str, Decimal]]  # day -> currency -> EUR rate
    rejected: int

    @property
    def newest(self) -> date | None:
        return max(self.days) if self.days else None


@dataclass(frozen=True, order=True)
class Rate:
    from_currency: str
    to_currency: str
    rate_type: str
    effective_date: date
    rate: Decimal


def currency_set(currencies: Iterable[str]) -> tuple[str, ...]:
    """The configured set, EUR always in it, validated."""
    out = {BASE}
    for c in currencies:
        if not CURRENCY.fullmatch(c):
            raise ValueError(f"not a currency code: {c!r}")
        out.add(c)
    return tuple(sorted(out))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rate(value: str | None) -> Decimal | None:
    try:
        rate = Decimal((value or "").strip())
    except InvalidOperation:
        return None
    return rate if rate.is_finite() and rate > 0 else None


def history(body: bytes) -> History:
    """The file's days. Raises on a payload that is not XML (the page
    dead-letters, its bronze copy kept)."""
    root = parse(body)
    days: dict[date, dict[str, Decimal]] = {}
    rejected = 0
    for el in root.iter():
        if _local(el.tag) != "Cube" or "time" not in el.attrib:
            continue
        try:
            day = date.fromisoformat(el.attrib["time"].strip())
        except ValueError:
            rejected += 1
            continue
        if day in days:
            rejected += 1  # a second copy of a day: the first stands
            continue
        quotes: dict[str, Decimal] = {}
        for c in el:
            if _local(c.tag) != "Cube":
                continue
            ccy = (c.attrib.get("currency") or "").strip()
            rate = _rate(c.attrib.get("rate"))
            if not CURRENCY.fullmatch(ccy) or rate is None or ccy == BASE or ccy in quotes:
                rejected += 1
                continue
            quotes[ccy] = rate
        days[day] = quotes
    return History(days, rejected)


def _closing_days(days: Iterable[date], newest: date) -> set[date]:
    last: dict[tuple[int, int], date] = {}
    for d in days:
        key = (d.year, d.month)
        last[key] = max(d, last.get(key, d))
    return {d for key, d in last.items() if key < (newest.year, newest.month)}


def directed(rate_a: Decimal, rate_b: Decimal) -> Decimal:
    """A -> B from the EUR rates of A and B (EUR's own is 1)."""
    return (rate_b / rate_a).quantize(RATE, rounding=ROUND_HALF_EVEN)


def rates(h: History, currencies: Iterable[str], start: date | None = None) -> list[Rate]:
    """Every directed rate for days on or after ``start``. A currency the ECB
    did not quote on a day has no rate that day."""
    newest = h.newest
    if newest is None:
        return []
    wanted = currency_set(currencies)
    closing = _closing_days(h.days, newest)
    out: list[Rate] = []
    for d, quotes in h.days.items():
        if start is not None and d < start:
            continue
        per_eur: Mapping[str, Decimal] = {
            BASE: Decimal(1), **{c: quotes[c] for c in wanted if c in quotes}}
        for a, ra in per_eur.items():
            for b, rb in per_eur.items():
                if a == b:
                    continue
                value = directed(ra, rb)
                out.append(Rate(a, b, AVERAGE, d, value))
                if d in closing:
                    out.append(Rate(a, b, CLOSING, d, value))
    return sorted(out)
