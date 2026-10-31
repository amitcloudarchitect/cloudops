"""Recorded ECB euro reference rates (eurofxref-hist format).

    python -m cloudops_devdata.ecb_fixtures      # rewrites the fixture files

``eurofxref-hist.xml``: the ECB's history file as it is published, newest day
first, for ``DAYS`` TARGET business days ending ``LAST_DAY`` (a Friday):
weekends and TARGET holidays have no ``Cube``. Every day quotes the same
currencies as the real file does (a subset of them), EUR -> X, at the
precision the ECB publishes each one (five significant figures). The rates
are a seeded random walk, not real rates.

``eurofxref-hist-revised.xml``: the same file after the ECB revised one
published rate (``REVISED``): the connector must update it and record the
explicit restatement entry.

``expectations()`` is the ground truth: every directed rate the connector must
store for a currency set, computed here from the generated rates rather than
by the connector. The committed files must equal ``build()`` exactly (a test
checks).
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Final

FIXTURE_DIR: Final = (
    Path(__file__).resolve().parents[4] / "connectors/src/cloudops_connectors/ecb/fixtures"
)
HIST: Final = "eurofxref-hist.xml"
HIST_REVISED: Final = "eurofxref-hist-revised.xml"
LAST_DAY: Final = date(2026, 10, 30)
DAYS: Final = 120
SEED: Final = 16
RATE: Final = Decimal("0.00000001")  # numeric(18,8)
# currency -> (opening EUR rate, decimal places the ECB publishes it with)
OPENING: Final = {
    "USD": ("1.0850", 4), "JPY": ("162.45", 2), "CZK": ("24.950", 3), "DKK": ("7.4610", 4),
    "GBP": ("0.85500", 5), "HUF": ("395.10", 2), "PLN": ("4.2850", 4), "SEK": ("11.2450", 4),
    "CHF": ("0.94200", 5), "NOK": ("11.6850", 4), "AUD": ("1.6420", 4), "CAD": ("1.4810", 4),
    "INR": ("90.4560", 4),
}
# The rate the ECB later revised (day, currency), and by how much.
REVISED: Final = (date(2026, 10, 14), "GBP")
REVISION: Final = Decimal("0.00120")


def _target_holidays(year: int) -> set[date]:
    """TARGET closing days: New Year, Good Friday, Easter Monday, 1 May, 25 and 26 Dec."""
    # Anonymous Gregorian algorithm for Easter Sunday.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    easter = date(year, month, day + 1)
    return {date(year, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1),
            date(year, 5, 1), date(year, 12, 25), date(year, 12, 26)}


def business_days() -> list[date]:
    """The ``DAYS`` TARGET business days ending ``LAST_DAY``, oldest first."""
    out: list[date] = []
    d = LAST_DAY
    while len(out) < DAYS:
        if d.weekday() < 5 and d not in _target_holidays(d.year):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _quantum(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)


def published() -> dict[date, dict[str, Decimal]]:
    """day -> currency -> EUR rate, as the first file publishes them."""
    rng = random.Random(SEED)  # noqa: S311 - test data
    level = {c: Decimal(v) for c, (v, _) in OPENING.items()}
    out: dict[date, dict[str, Decimal]] = {}
    for d in business_days():
        day: dict[str, Decimal] = {}
        for c, (_, places) in OPENING.items():
            level[c] *= Decimal(1 + rng.gauss(0, 0.004))
            day[c] = level[c].quantize(_quantum(places), rounding=ROUND_HALF_EVEN)
        out[d] = day
    return out


def revised() -> dict[date, dict[str, Decimal]]:
    """The history after the ECB revised one rate."""
    rates = {d: dict(v) for d, v in published().items()}
    day, ccy = REVISED
    rates[day][ccy] = rates[day][ccy] + REVISION
    return rates


def revised_rate() -> tuple[date, str, Decimal, Decimal]:
    """(day, currency, as first published, as revised)."""
    day, ccy = REVISED
    return day, ccy, published()[day][ccy], revised()[day][ccy]


def _xml(rates: dict[date, dict[str, Decimal]]) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"'
        ' xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">',
        "\t<gesmes:subject>Reference rates</gesmes:subject>",
        "\t<gesmes:Sender>",
        "\t\t<gesmes:name>European Central Bank</gesmes:name>",
        "\t</gesmes:Sender>",
        "\t<Cube>",
    ]
    for d in sorted(rates, reverse=True):
        lines.append(f'\t\t<Cube time="{d.isoformat()}">')
        for c, (_, places) in OPENING.items():
            lines.append(f'\t\t\t<Cube currency="{c}" rate="{rates[d][c]:.{places}f}"/>')
        lines.append("\t\t</Cube>")
    lines += ["\t</Cube>", "</gesmes:Envelope>", ""]
    return "\n".join(lines).encode()


@dataclass(frozen=True, order=True)
class Expected:
    from_currency: str
    to_currency: str
    rate_type: str  # average | closing
    effective_date: date
    rate: Decimal


def expectations(
    rates: dict[date, dict[str, Decimal]] | None = None,
    currencies: tuple[str, ...] = ("EUR", "GBP", "USD"),
) -> list[Expected]:
    """Every directed pair among ``currencies``: each day's ``average``, and a
    ``closing`` on the last business day of every month before the newest."""
    rates = published() if rates is None else rates
    newest = max(rates)
    last_of_month: dict[tuple[int, int], date] = {}
    for d in rates:
        key = (d.year, d.month)
        last_of_month[key] = max(d, last_of_month.get(key, d))
    closing = {d for k, d in last_of_month.items() if k < (newest.year, newest.month)}
    out: list[Expected] = []
    for d, day in rates.items():
        per_eur = {"EUR": Decimal(1), **{c: day[c] for c in currencies if c != "EUR"}}
        for a in currencies:
            for b in currencies:
                if a == b:
                    continue
                rate = (per_eur[b] / per_eur[a]).quantize(RATE, rounding=ROUND_HALF_EVEN)
                out.append(Expected(a, b, "average", d, rate))
                if d in closing:
                    out.append(Expected(a, b, "closing", d, rate))
    return sorted(out)


def build() -> dict[str, bytes]:
    return {HIST: _xml(published()), HIST_REVISED: _xml(revised())}


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, body in build().items():
        (FIXTURE_DIR / name).write_bytes(body)
    print(f"wrote {FIXTURE_DIR} ({DAYS} business days to {LAST_DAY})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
