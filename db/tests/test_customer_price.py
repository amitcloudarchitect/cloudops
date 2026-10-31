"""The customer price for cloud consumption (ADR 0021, migrations 0014-0015).

``cost_fact`` is what the provider charges Getronics: commercial (ADR 0002,
D10). A customer scope is refused its cost columns by the database, and reads
its consumption at the price Getronics charges it, which is the amortized cost
times (1 + the uplift of the one SOW in force), through
``cloudops.customer_price`` alone. Staff cannot call that function, because
price divided by cost is the uplift.

The seeded tenants each have one cost row today: amortized 118.000000 in the
tenant's currency (alpha EUR, bravo GBP, charlie USD). Charlie has a second
subscription with 7.500000 USD, which a subscription-limited customer must not
see.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Api, Principals, Seeded
from .support import scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

UPLIFT_A = Decimal("0.08")  # alpha, EUR
UPLIFT_C = Decimal("0.10")  # charlie, USD; bravo (GBP) has none
SEK_PER_EUR = Decimal("11.43210000")
SEK_PER_USD = Decimal("10.51870000")
FX_SOURCE = "test:customer-price"


async def _rows(db: Database, s: TenantScope, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with db.transaction(s) as conn:
        return [tuple(r) for r in await conn.execute(text(sql), params)]


@pytest.fixture(scope="module")
def priced(seeded: Seeded) -> Iterator[None]:
    """Uplifts on alpha's and charlie's SOWs, and SEK rates in force since 2000."""
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for t, uplift in ((seeded.a, UPLIFT_A), (seeded.c, UPLIFT_C)):
            cx.execute("UPDATE cloudops.sow SET consumption_uplift = %s WHERE tenant_id = %s",
                       (uplift, t.id))
        for frm, rate in (("EUR", SEK_PER_EUR), ("USD", SEK_PER_USD)):
            cx.execute(
                "INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate, rate_type,"
                " effective_date, source) VALUES (%s, 'SEK', %s, 'average', '2000-01-01', %s)",
                (frm, rate, FX_SOURCE))
    try:
        yield
    finally:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("UPDATE cloudops.sow SET consumption_uplift = NULL"
                       " WHERE tenant_id = ANY(%s)", ([seeded.a.id, seeded.c.id],))
            cx.execute("DELETE FROM cloudops.fx_rate WHERE source = %s", (FX_SOURCE,))


_PRICE = ("SELECT tenant_id, currency, sum(price), bool_and(price IS NOT NULL)"
          " FROM cloudops.customer_price(current_date - 1, current_date) GROUP BY 1, 2")


# --- the cost columns are refused, not filtered --------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT unblended_cost FROM cloudops.cost_fact",
    "SELECT amortized_cost FROM cloudops.cost_fact",
    "SELECT sum(unblended_cost) FROM cloudops.cost_fact WHERE usage_date = current_date",
    "SELECT * FROM cloudops.cost_fact",
    "SELECT usage_date FROM cloudops.cost_fact ORDER BY unblended_cost",
    "SELECT consumption_uplift FROM cloudops.sow",
])
async def test_customer_scope_is_refused_getronics_cost(
    app_db: Database, seeded: Seeded, sql: str,
) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER), sql)


async def test_customer_scope_still_reads_its_consumption_lines(
    app_db: Database, seeded: Seeded,
) -> None:
    rows = await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER),
                       "SELECT tenant_id, service_native, currency FROM cloudops.cost_fact")
    assert {r[0] for r in rows} == {seeded.a.id}
    assert ("Virtual Machines", seeded.a.currency) in {(r[1], r[2]) for r in rows}


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.CUSTOMER])
async def test_no_staff_role_can_compute_the_price(
    app_db: Database, seeded: Seeded, role: Role,
) -> None:
    """Price over cost is the uplift: staff who read cost never get the price."""
    with pytest.raises(Exception, match="permission denied for function customer_price"):
        await _rows(app_db, scope(seeded.a, role=role), _PRICE)


# --- the price -------------------------------------------------------------------------------


async def test_price_is_cost_times_one_plus_the_uplift_in_two_currencies(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    a = await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER), _PRICE)
    c = await _rows(app_db, scope(seeded.c, role=Role.CUSTOMER), _PRICE)
    assert a == [(seeded.a.id, "EUR", Decimal("127.44000000000000"), True)]
    # Both of charlie's subscriptions: 118 + 7.5 USD, times 1.10.
    assert [(r[0], r[1], r[3]) for r in c] == [(seeded.c.id, "USD", True)]
    assert c[0][2] == (Decimal("118.000000") + Decimal("7.500000")) * (1 + UPLIFT_C)


async def test_a_sow_without_an_uplift_is_not_captured_never_the_cost(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    rows = await _rows(app_db, scope(seeded.b, role=Role.CUSTOMER), _PRICE)
    assert rows == [(seeded.b.id, "GBP", None, False)]


async def test_two_sows_in_force_are_not_captured(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    """Never guess between two contracts."""
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO cloudops.sow (tenant_id, reference, effective_from, currency,"
            " monthly_manage_revenue, consumption_uplift)"
            " VALUES (%s, 'SOW-overlap', '2026-06-01', 'EUR', 1.000000, 0.5)", (seeded.a.id,))
    try:
        rows = await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER), _PRICE)
    finally:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("DELETE FROM cloudops.sow WHERE reference = 'SOW-overlap'")
    assert rows == [(seeded.a.id, "EUR", None, False)]


async def test_the_function_returns_only_the_callers_tenant(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    """SECURITY DEFINER, yet row-level security still scopes it to the caller."""
    rows = await _rows(
        app_db, scope(seeded.a, role=Role.CUSTOMER),
        "SELECT DISTINCT tenant_id FROM cloudops.customer_price('2000-01-01', current_date)"
        " WHERE tenant_id = ANY(CAST(:ids AS uuid[]))",
        ids=[t.id for t in seeded.tenants])
    assert rows == [(seeded.a.id,)]


# --- the API ---------------------------------------------------------------------------------


def _line(body: dict[str, Any], currency: str) -> dict[str, Any]:
    found = [ln for ln in body["lines"] if ln["currency"] == currency]
    assert len(found) == 1, body
    line: dict[str, Any] = found[0]
    return line


async def test_a_customer_gets_the_price_and_never_the_cost(
    api: Api, principals: Principals, seeded: Seeded, priced: None,
) -> None:
    response = await api.get("/v1/cost/summary", principals.customer_a,
                             params={"days": "7", "currency": "SEK"})
    assert response.status_code == 200
    body = response.json()
    assert "amortized" not in response.text
    line = _line(body, "EUR")
    assert Decimal(line["price"]["value"]) == Decimal("118.000000") * (1 + UPLIFT_A)
    assert line["price"]["unit"] == "EUR"
    assert line["not_captured"] is None
    # Converted at the rate in force on the usage date; unrounded.
    assert Decimal(line["converted"]["value"]) == (
        Decimal("118.000000") * (1 + UPLIFT_A) * SEK_PER_EUR)
    assert line["converted"]["unit"] == "SEK"

    staff = (await api.get("/v1/cost/summary", principals.finops_all,
                           params={"days": "7"})).json()
    assert Decimal(_line(staff, "EUR")["amortized"]["value"]) == Decimal("118.000000")
    assert "price" not in _line(staff, "EUR")


async def test_a_subscription_limited_customer_is_priced_on_its_subscription_only(
    api: Api, principals: Principals, priced: None,
) -> None:
    body = (await api.get("/v1/cost/summary", principals.customer_c_limited,
                          params={"days": "7", "currency": "SEK"})).json()
    line = _line(body, "USD")
    assert Decimal(line["price"]["value"]) == Decimal("118.000000") * (1 + UPLIFT_C)
    assert Decimal(line["converted"]["value"]) == (
        Decimal("118.000000") * (1 + UPLIFT_C) * SEK_PER_USD)


async def test_no_rate_in_force_is_no_conversion_never_a_one(
    api: Api, principals: Principals, priced: None,
) -> None:
    body = (await api.get("/v1/cost/summary", principals.customer_a,
                          params={"days": "7", "currency": "JPY"})).json()
    line = _line(body, "EUR")
    assert line["price"] is not None
    assert line["converted"] is None


async def test_the_customer_overview_shows_the_price(
    api: Api, principals: Principals, seeded: Seeded, priced: None,
) -> None:
    [row] = (await api.get("/v1/customers", principals.customer_a)).json()
    assert row["cost_30d"] is None
    [price] = row["price_30d"]
    assert Decimal(price["price"]["value"]) == Decimal("118.000000") * (1 + UPLIFT_A)
    assert price["price"]["source_system"] == "customer_price"

    staff = (await api.get("/v1/customers", principals.finops_all)).json()
    assert all(r["price_30d"] is None for r in staff)
    assert any(r["cost_30d"] for r in staff)
