"""cloudops.to_currency(): the rate in force on the transaction date, rate_type
required, missing rate raises, exact decimals. At least two currency pairs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import exc, text

from cloudops_tenancy import Database

from .conftest import Seeded
from .support import scope

pytestmark = pytest.mark.integration

RATES = [
    # from, to, rate, type, effective
    ("EUR", "GBP", "0.85000000", "average", date(2019, 1, 31)),
    ("EUR", "GBP", "0.86000000", "average", date(2019, 2, 28)),
    ("EUR", "GBP", "0.84000000", "closing", date(2019, 1, 31)),
    ("USD", "EUR", "0.92123456", "average", date(2019, 1, 31)),
]


@pytest.fixture(scope="module")
def rates(seeded: Seeded) -> Iterator[None]:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for row in RATES:
            cx.execute(
                "INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate, rate_type,"
                " effective_date, source) VALUES (%s, %s, %s, %s, %s, 'test')",
                row,
            )
        yield
        cx.execute("DELETE FROM cloudops.fx_rate WHERE source = 'test'")


async def convert(db: Database, seeded: Seeded, *args: Any) -> Any:
    async with db.transaction(scope(seeded.a)) as conn:
        return await conn.scalar(
            text("SELECT cloudops.to_currency(:a, :f, :t, :d, :r)"),
            dict(zip("aftdr", args, strict=True)),
        )


def sqlstate(error: exc.DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


@pytest.mark.usefixtures("rates")
@pytest.mark.parametrize(
    ("amount", "pair", "on", "rate_type", "expected"),
    [
        ("100.000000", ("EUR", "GBP"), date(2019, 1, 31), "average", "85.00000000000000"),
        ("100.000000", ("EUR", "GBP"), date(2019, 2, 15), "average", "85.00000000000000"),
        ("100.000000", ("EUR", "GBP"), date(2019, 3, 1), "average", "86.00000000000000"),
        ("100.000000", ("EUR", "GBP"), date(2019, 2, 15), "closing", "84.00000000000000"),
        ("1234.567891", ("USD", "EUR"), date(2019, 6, 30), "average", "1137.32660785551296"),
    ],
)
async def test_rate_in_force_on_the_transaction_date(
    app_db: Database,
    seeded: Seeded,
    amount: str,
    pair: tuple[str, str],
    on: date,
    rate_type: str,
    expected: str,
) -> None:
    result = await convert(app_db, seeded, Decimal(amount), *pair, on, rate_type)
    assert isinstance(result, Decimal)
    assert result == Decimal(expected)


@pytest.mark.usefixtures("rates")
async def test_history_is_not_converted_at_todays_rate(app_db: Database, seeded: Seeded) -> None:
    january = await convert(
        app_db, seeded, Decimal(100), "EUR", "GBP", date(2019, 1, 31), "average"
    )
    today = await convert(app_db, seeded, Decimal(100), "EUR", "GBP", date.today(), "average")
    assert january != today


async def test_same_currency_needs_no_rate(app_db: Database, seeded: Seeded) -> None:
    amount = Decimal("41250.500000")
    assert (
        await convert(app_db, seeded, amount, "GBP", "GBP", date(2001, 1, 1), "closing") == amount
    )


@pytest.mark.usefixtures("rates")
@pytest.mark.parametrize(
    ("pair", "on"),
    [
        (("EUR", "GBP"), date(2019, 1, 30)),  # before the first rate
        (("GBP", "EUR"), date(2019, 3, 1)),  # no inverse fallback
        (("EUR", "JPY"), date(2019, 3, 1)),  # unknown pair
    ],
)
async def test_missing_rate_raises_rather_than_returning_zero(
    app_db: Database, seeded: Seeded, pair: tuple[str, str], on: date
) -> None:
    with pytest.raises(exc.DBAPIError) as error:
        await convert(app_db, seeded, Decimal(100), *pair, on, "average")
    assert sqlstate(error.value) == "P0002"  # no_data_found


@pytest.mark.parametrize("rate_type", ["spot", "", None, "Average"])
async def test_rate_type_must_be_closing_or_average(
    app_db: Database, seeded: Seeded, rate_type: str | None
) -> None:
    with pytest.raises(exc.DBAPIError) as error:
        await convert(app_db, seeded, Decimal(1), "EUR", "GBP", date(2019, 3, 1), rate_type)
    assert sqlstate(error.value) == "22023"  # invalid_parameter_value


async def test_rate_type_has_no_default(app_db: Database, seeded: Seeded) -> None:
    with pytest.raises(exc.DBAPIError) as error:
        async with app_db.transaction(scope(seeded.a)) as conn:
            await conn.scalar(
                text("SELECT cloudops.to_currency(1::numeric, 'EUR', 'GBP', '2019-03-01'::date)")
            )
    assert sqlstate(error.value) == "42883"  # undefined_function


@pytest.mark.usefixtures("rates")
async def test_rate_and_rate_date_are_available_for_views(app_db: Database, seeded: Seeded) -> None:
    async with app_db.transaction(scope(seeded.a)) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT rate, effective_date"
                    " FROM cloudops.fx_rate_on('EUR', 'GBP', :d, 'average')"
                ),
                {"d": date(2019, 2, 15)},
            )
        ).one()
    assert tuple(row) == (Decimal("0.85000000"), date(2019, 1, 31))
