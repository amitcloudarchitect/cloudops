"""Presenting money: the one place an amount is rounded (CLAUDE.md rule 5).

Amounts travel as unrounded ``Decimal`` in their currency. Here, at the
presentation boundary, they are rounded once, half-up, to the currency's minor
units (ISO 4217), and written as ``EUR 1,234.56``. Negative amounts are
``EUR -1,234.56``.

This format is what the acceptance diff compares against the manually
assembled pack, line by line; if that pack writes currency differently, this
is the function to change, in one place.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

# ISO 4217 exponents that differ from 2. Everything else has two minor units.
MINOR_UNITS: Final = {
    "JPY": 0, "KRW": 0, "ISK": 0, "CLP": 0, "VND": 0,
    "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3, "IQD": 3, "LYD": 3,
}
_CODE: Final = re.compile(r"^[A-Z]{3}$")


def check_currency(code: str) -> str:
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ValueError(f"not an ISO 4217 currency code: {code!r}")
    return code


def round_money(amount: Decimal, currency: str) -> Decimal:
    if not isinstance(amount, Decimal):
        raise TypeError("money is Decimal, never float")
    places = MINOR_UNITS.get(check_currency(currency), 2)
    return amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def present(amount: Decimal, currency: str) -> str:
    rounded = round_money(amount, currency)
    places = MINOR_UNITS.get(currency, 2)
    sign = "-" if rounded < 0 else ""
    return f"{currency} {sign}{abs(rounded):,.{places}f}"
