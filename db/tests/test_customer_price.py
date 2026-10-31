"""The customer price for cloud consumption (ADR 0021, migrations 0014-0017).

``cost_fact`` is what the provider charges Getronics: commercial (ADR 0002,
D10). A customer scope is refused its cost columns by the database, and reads
its consumption at the price Getronics charges it: the amortized cost times
(1 + the uplift of the one SOW in force).

It reads it through ``cloudops.customer_monthly_price`` alone, by closed calendar
month, provider and currency, and nothing finer (decision 8). A per-line price
divided by a known list price would be the uplift. A CoreStack finding's saving
is priced the same way and read only as a total, through
``cloudops.customer_saving_total``. Staff can call none of these, because price
divided by cost is the uplift.

The seeded tenants each have one cost row today (the current month, never
shown). The fixture adds two lines last month, in two charge classes on two
days, for each tenant in its currency (alpha EUR, bravo GBP, charlie USD), and
one more on charlie's second subscription, which a subscription-limited
customer must not see. Other suites may add rows to the same month, so expected
figures are read from the table as superuser, never assumed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Api, Principals, Seeded, Tenant
from .support import scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

UPLIFT_A = Decimal("0.08")  # alpha, EUR
UPLIFT_C = Decimal("0.10")  # charlie, USD; bravo (GBP) has none
SEK_PER_EUR = Decimal("11.43210000")
SEK_PER_USD = Decimal("10.51870000")
SOURCE = "test:customer-price"

TODAY = datetime.now(UTC).date()
THIS_MONTH = TODAY.replace(day=1)
LAST_MONTH = (THIS_MONTH - timedelta(days=1)).replace(day=1)  # its partitions exist
D1, D2 = LAST_MONTH + timedelta(days=3), LAST_MONTH + timedelta(days=17)


async def _rows(db: Database, s: TenantScope, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with db.transaction(s) as conn:
        return [tuple(r) for r in await conn.execute(text(sql), params)]


def _cost(cx: psycopg.Connection[Any], t: Tenant, account: Any, day: date, cls: str,
          amount: str) -> None:
    cx.execute(
        "INSERT INTO cloudops.cost_fact (tenant_id, account_id, usage_date, invoice_period,"
        " service_native, charge_class, unblended_cost, amortized_cost, currency)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (t.id, account, day, f"{day:%Y-%m}", SOURCE, cls, amount, amount, t.currency))


@pytest.fixture(scope="module")
def priced(seeded: Seeded) -> Iterator[None]:
    """Uplifts on alpha's and charlie's SOWs, last month's lines, and SEK rates."""
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for t, uplift in ((seeded.a, UPLIFT_A), (seeded.c, UPLIFT_C)):
            cx.execute("UPDATE cloudops.sow SET consumption_uplift = %s WHERE tenant_id = %s",
                       (uplift, t.id))
        for t in seeded.tenants:
            _cost(cx, t, t.account_id, D1, "consumption", "40.000000")
            _cost(cx, t, t.account_id, D2, "support", "60.000000")
        _cost(cx, seeded.c, seeded.c_account2, D1, "marketplace", "7.500000")
        for frm, rate in (("EUR", SEK_PER_EUR), ("USD", SEK_PER_USD)):
            cx.execute(
                "INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate, rate_type,"
                " effective_date, source) VALUES (%s, 'SEK', %s, 'average', '2000-01-01', %s)",
                (frm, rate, SOURCE))
    try:
        yield
    finally:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("UPDATE cloudops.sow SET consumption_uplift = NULL"
                       " WHERE tenant_id = ANY(%s)", ([seeded.a.id, seeded.c.id],))
            cx.execute("DELETE FROM cloudops.cost_fact WHERE service_native = %s", (SOURCE,))
            cx.execute("DELETE FROM cloudops.fx_rate WHERE source = %s", (SOURCE,))


def _last_month_cost(seeded: Seeded, t: Tenant, account: Any = None) -> Decimal:
    """What the table holds, as superuser: the figure the price must be built on."""
    with psycopg.connect(seeded.admin_dsn) as cx:
        row = cx.execute(
            "SELECT sum(amortized_cost) FROM cloudops.cost_fact WHERE tenant_id = %s"
            " AND usage_date >= %s AND usage_date < %s AND (%s::uuid IS NULL OR account_id = %s)",
            (t.id, LAST_MONTH, THIS_MONTH, account, account)).fetchone()
    assert row is not None
    assert row[0] is not None
    return Decimal(row[0])


_MONTHLY = ("SELECT tenant_id, period, provider, currency, price, converted"
            " FROM cloudops.customer_monthly_price(:start, :end, CAST(:rep AS char(3)))")


async def _monthly(db: Database, t: Tenant, rep: str | None = None,
                   start: date = LAST_MONTH, end: date = TODAY) -> list[tuple[Any, ...]]:
    return await _rows(db, scope(t, role=Role.CUSTOMER), _MONTHLY, start=start, end=end,
                       rep=rep)


# --- the cost columns are refused, not filtered --------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT unblended_cost FROM cloudops.cost_fact",
    "SELECT amortized_cost FROM cloudops.cost_fact",
    "SELECT sum(unblended_cost) FROM cloudops.cost_fact WHERE usage_date = current_date",
    "SELECT * FROM cloudops.cost_fact",
    "SELECT usage_date FROM cloudops.cost_fact ORDER BY unblended_cost",
    "SELECT consumption_uplift FROM cloudops.sow",
    "SELECT estimated_saving FROM cloudops.cost_finding",
    "SELECT * FROM cloudops.cost_finding",
    # The per-line building blocks: price divided by a known list price is the uplift.
    "SELECT * FROM cloudops.customer_price(current_date - 1, current_date)",
    "SELECT * FROM cloudops.customer_saving()",
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


async def test_customer_scope_still_reads_its_findings(
    app_db: Database, seeded: Seeded,
) -> None:
    rows = await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER),
                       "SELECT tenant_id, title, saving_currency FROM cloudops.cost_finding")
    assert rows == [(seeded.a.id, "Idle VM", seeded.a.currency)]


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.CUSTOMER])
@pytest.mark.parametrize(("sql", "function"), [
    ("SELECT * FROM cloudops.customer_monthly_price(current_date - 60, current_date, NULL)",
     "customer_monthly_price"),
    ("SELECT * FROM cloudops.customer_saving_total()", "customer_saving_total"),
    ("SELECT * FROM cloudops.customer_price(current_date - 1, current_date)", "customer_price"),
    ("SELECT * FROM cloudops.customer_saving()", "customer_saving"),
])
async def test_no_staff_role_can_compute_the_price(
    app_db: Database, seeded: Seeded, role: Role, sql: str, function: str,
) -> None:
    """Price over cost is the uplift: staff who read cost never get the price."""
    with pytest.raises(Exception, match=f"permission denied for function {function}"):
        await _rows(app_db, scope(seeded.a, role=role), sql)


# --- the uplift ------------------------------------------------------------------------------


@pytest.mark.parametrize("uplift", ["0", "0.000000", "-0.01"])
def test_an_uplift_of_zero_or_less_is_refused(seeded: Seeded, uplift: str) -> None:
    """0 would show the customer Getronics' exact cost."""
    with psycopg.connect(seeded.admin_dsn) as cx, pytest.raises(psycopg.errors.CheckViolation):
        cx.execute("UPDATE cloudops.sow SET consumption_uplift = %s WHERE tenant_id = %s",
                   (uplift, seeded.a.id))


# --- the price -------------------------------------------------------------------------------


async def test_the_price_has_no_line_level_columns(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    async with app_db.transaction(scope(seeded.a, role=Role.CUSTOMER)) as conn:
        result = await conn.execute(text(
            "SELECT * FROM cloudops.customer_monthly_price(:s, :e, NULL)"),
            {"s": LAST_MONTH, "e": TODAY})
        columns = set(result.keys())
    assert columns == {"tenant_id", "period", "provider", "currency", "price", "converted"}


async def test_one_figure_per_month_provider_and_currency_in_two_currencies(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    """Two charge classes on two days (and, for charlie, two subscriptions) come
    back as one figure: no single line's price is visible."""
    a = await _monthly(app_db, seeded.a)
    c = sorted(await _monthly(app_db, seeded.c), key=lambda r: r[2])
    assert a == [(seeded.a.id, LAST_MONTH, "azure", "EUR",
                  _last_month_cost(seeded, seeded.a) * (1 + UPLIFT_A), None)]
    # Charlie's second subscription is on AWS: a figure of its own per provider.
    assert c == [
        (seeded.c.id, LAST_MONTH, "aws", "USD",
         _last_month_cost(seeded, seeded.c, seeded.c_account2) * (1 + UPLIFT_C), None),
        (seeded.c.id, LAST_MONTH, "azure", "USD",
         _last_month_cost(seeded, seeded.c, seeded.c.account_id) * (1 + UPLIFT_C), None),
    ]


async def test_the_current_month_is_never_shown(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    """A month to date that grows day by day would reveal each day's lines."""
    assert await _monthly(app_db, seeded.a, start=THIS_MONTH, end=TODAY) == []
    assert {r[1] for r in await _monthly(app_db, seeded.a)} == {LAST_MONTH}


async def test_the_dates_cannot_narrow_a_month(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    """Asking for one day returns the whole month it falls in."""
    whole = await _monthly(app_db, seeded.a)
    one_day = await _monthly(app_db, seeded.a, start=D2, end=D2)
    assert one_day == whole


async def test_converted_at_the_rate_on_each_usage_date(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    [a] = await _monthly(app_db, seeded.a, rep="SEK")
    assert a[5] == _last_month_cost(seeded, seeded.a) * (1 + UPLIFT_A) * SEK_PER_EUR
    [none] = await _monthly(app_db, seeded.a, rep="JPY")
    assert none[4] is not None
    assert none[5] is None


async def test_a_sow_without_an_uplift_is_not_captured_never_the_cost(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    rows = await _monthly(app_db, seeded.b)
    assert rows == [(seeded.b.id, LAST_MONTH, "azure", "GBP", None, None)]


async def test_two_sows_in_force_are_not_captured(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    """Never guess between two contracts."""
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO cloudops.sow (tenant_id, reference, effective_from, currency,"
            " monthly_manage_revenue, consumption_uplift)"
            " VALUES (%s, 'SOW-overlap', %s, 'EUR', 1.000000, 0.5)", (seeded.a.id, D2))
    try:
        rows = await _monthly(app_db, seeded.a)
    finally:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("DELETE FROM cloudops.sow WHERE reference = 'SOW-overlap'")
    assert [r[4] for r in rows] == [None]


async def test_the_function_returns_only_the_callers_tenant(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    """SECURITY DEFINER, yet row-level security still scopes it to the caller."""
    rows = await _rows(
        app_db, scope(seeded.a, role=Role.CUSTOMER),
        "SELECT DISTINCT tenant_id FROM cloudops.customer_monthly_price('2000-01-01', :e, NULL)",
        e=TODAY)
    assert rows == [(seeded.a.id,)]


# --- findings' saving ----------------------------------------------------------------------


_SAVING = "SELECT tenant_id, currency, findings, saving FROM cloudops.customer_saving_total()"


async def test_the_open_findings_saving_is_priced_as_a_total_in_two_currencies(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    """CoreStack's saving is in provider-cost terms (12.500000 seeded): uplifted too."""
    a = await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER), _SAVING)
    c = await _rows(app_db, scope(seeded.c, role=Role.CUSTOMER), _SAVING)
    assert a == [(seeded.a.id, "EUR", 1, Decimal("12.500000") * (1 + UPLIFT_A))]
    assert c == [(seeded.c.id, "USD", 1, Decimal("12.500000") * (1 + UPLIFT_C))]


async def test_a_saving_without_an_uplift_is_not_captured(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    rows = await _rows(app_db, scope(seeded.b, role=Role.CUSTOMER), _SAVING)
    assert rows == [(seeded.b.id, "GBP", 1, None)]


async def test_the_saving_total_returns_only_the_callers_tenant(
    app_db: Database, seeded: Seeded, priced: None,
) -> None:
    rows = await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER),
                       "SELECT DISTINCT tenant_id FROM cloudops.customer_saving_total()")
    assert rows == [(seeded.a.id,)]


# --- the API ---------------------------------------------------------------------------------


async def test_a_customer_gets_monthly_prices_and_never_the_cost(
    api: Api, principals: Principals, seeded: Seeded, priced: None,
) -> None:
    response = await api.get("/v1/cost/summary", principals.customer_a,
                             params={"months": "1", "currency": "SEK"})
    assert response.status_code == 200
    body = response.json()
    assert body["period_start"] == LAST_MONTH.isoformat()
    assert "amortized" not in response.text
    [line] = body["lines"]
    assert set(line) == {"period", "provider", "currency", "price", "not_captured", "converted"}
    expected = _last_month_cost(seeded, seeded.a) * (1 + UPLIFT_A)
    assert (line["period"], line["currency"]) == (LAST_MONTH.isoformat(), "EUR")
    assert Decimal(line["price"]["value"]) == expected
    assert Decimal(line["converted"]["value"]) == expected * SEK_PER_EUR
    assert line["converted"]["unit"] == "SEK"

    staff = (await api.get("/v1/cost/summary", principals.finops_all,
                           params={"days": "7"})).json()
    assert all("amortized" in ln and "price" not in ln for ln in staff["lines"])


async def test_a_subscription_limited_customer_is_priced_on_its_subscription_only(
    api: Api, principals: Principals, seeded: Seeded, priced: None,
) -> None:
    body = (await api.get("/v1/cost/summary", principals.customer_c_limited,
                          params={"months": "1"})).json()
    [line] = body["lines"]
    assert Decimal(line["price"]["value"]) == (
        _last_month_cost(seeded, seeded.c, seeded.c.account_id) * (1 + UPLIFT_C))


async def test_the_customer_overview_shows_last_months_price(
    api: Api, principals: Principals, seeded: Seeded, priced: None,
) -> None:
    [row] = (await api.get("/v1/customers", principals.customer_a)).json()
    assert row["cost_30d"] is None
    [price] = row["price_last_month"]
    assert price["period"] == LAST_MONTH.isoformat()
    assert Decimal(price["price"]["value"]) == (
        _last_month_cost(seeded, seeded.a) * (1 + UPLIFT_A))
    assert price["price"]["source_system"] == "customer_price"

    staff = (await api.get("/v1/customers", principals.finops_all)).json()
    assert all(r["price_last_month"] is None for r in staff)
    assert any(r["cost_30d"] for r in staff)
