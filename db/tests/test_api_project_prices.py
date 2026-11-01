"""Project prices from ServiceNow project tasks (ADR 0038) against the seeded
database: three tenants in EUR, GBP and USD, one closed task each, priced in the
tenant's currency and closed when the database was seeded."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from .conftest import Api, Principals, Seeded

pytestmark = pytest.mark.integration

PATH = "/v1/commercial/project-prices"
TODAY = datetime.now(UTC).date().isoformat()
# A month nothing else seeds, for the figures this module adds.
MARCH = {"from": "2025-03-01", "to": "2025-03-31"}


def _query(**params: str) -> str:
    return PATH + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def _customer(body: dict[str, Any], tenant: Any) -> dict[str, Any]:
    return next(c for c in body["customers"] if c["tenant"]["id"] == str(tenant.id))


def _amounts(bucket: dict[str, Any]) -> dict[str, Decimal]:
    return {a["currency"]: Decimal(a["amount"]["value"]) for a in bucket["amounts"]}


def _task(cx: psycopg.Connection[Any], tenant: Any, status: str, on: date,
          price: str | None, currency: str | None) -> None:
    moment = datetime(on.year, on.month, on.day, 12, tzinfo=UTC)
    cx.execute(
        "INSERT INTO cloudops.project_task (tenant_id, sys_id, number, company_raw, status,"
        " opened_at, closed_at, source_updated_at, price, price_currency, price_field)"
        " VALUES (%s, %s, %s, 'test', %s, %s, %s, %s, %s, %s, %s)",
        (tenant.id, uuid4().hex, f"PRJTASK-T{uuid4().hex[:6]}", status, moment,
         moment if status == "closed" else None, moment, price, currency,
         "cost" if price else None))


@pytest.fixture
def march(seeded: Seeded) -> Iterator[None]:
    """Tenant a in March 2025: delivered in EUR and GBP (the GBP rate changes
    mid-month), one closed without a price, one open in USD with no USD rate."""
    a = seeded.a
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        _task(cx, a, "closed", date(2025, 3, 10), "1000.000000", "EUR")
        _task(cx, a, "closed", date(2025, 3, 20), "500.000000", "GBP")
        _task(cx, a, "closed", date(2025, 3, 21), None, None)
        _task(cx, a, "open", date(2025, 3, 5), "200.000000", "USD")
        _task(cx, a, "open", date(2025, 4, 5), "999.000000", "EUR")  # outside the range
        cx.execute(
            "INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate, rate_type,"
            " effective_date, source) VALUES ('GBP', 'EUR', 1.10, 'average', '2025-03-01',"
            " 'test'), ('GBP', 'EUR', 1.20, 'average', '2025-03-15', 'test')")
    yield
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("DELETE FROM cloudops.project_task WHERE company_raw = 'test'")
        cx.execute("DELETE FROM cloudops.fx_rate WHERE source = 'test'")


async def test_every_customer_in_scope_with_its_delivered_price(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    r = await api.get(_query(**{"from": TODAY, "to": TODAY}), principals.finops_all)
    assert r.status_code == 200
    body = r.json()
    assert body["from"] == body["to"] == TODAY
    assert body["source"] == "never_synced"
    assert body["as_of"] is None  # never synced: no freshness to claim
    for t in seeded.tenants:
        c = _customer(body, t)
        assert c["delivered"]["tasks"]["value"] == 1
        assert _amounts(c["delivered"]) == {t.currency: Decimal("12500")}
        assert c["delivered"]["converted"] is None  # no currency asked for
        assert c["delivered"]["amounts"][0]["amount"]["source_system"] == "servicenow"
        assert c["proposed"]["tasks"]["value"] == 0
        assert c["proposed"]["amounts"] == []


@pytest.mark.usefixtures("march")
async def test_two_currencies_convert_at_the_rate_on_each_tasks_date(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    r = await api.get(_query(**MARCH, currency="EUR"), principals.finops_all)
    a = _customer(r.json(), seeded.a)
    delivered, proposed = a["delivered"], a["proposed"]
    assert delivered["tasks"]["value"] == 3
    assert delivered["unpriced_tasks"]["value"] == 1  # counted, not summed as zero
    assert _amounts(delivered) == {"EUR": Decimal("1000"), "GBP": Decimal("500")}
    # 500 GBP on 20 March at 1.20 (in force from the 15th), never 1.10 or today's rate.
    assert Decimal(delivered["converted"]["value"]) == Decimal("1600")
    assert delivered["converted"]["unit"] == "EUR"
    assert proposed["tasks"]["value"] == 1
    assert _amounts(proposed) == {"USD": Decimal("200")}
    assert proposed["converted"] is None  # no USD rate: absent, never partial
    assert _customer(r.json(), seeded.b)["delivered"]["tasks"]["value"] == 0


@pytest.mark.usefixtures("march")
async def test_one_customer_narrows_the_scope(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    r = await api.get(_query(**MARCH, tenant_id=str(seeded.a.id)), principals.finops_all)
    assert [c["tenant"]["id"] for c in r.json()["customers"]] == [str(seeded.a.id)]


@pytest.mark.isolation
async def test_engineers_and_customers_are_refused(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    for who in (principals.engineer_a, principals.customer_a):
        r = await api.get(_query(**{"from": TODAY, "to": TODAY}), who)
        assert r.status_code == 403
        assert "12500" not in r.text


@pytest.mark.isolation
async def test_a_tenant_outside_scope_is_not_found(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    """A tenant id that is not in scope is 404, like one that does not exist; an
    engineer is refused the same way for its own tenant and another's, so the
    answer says nothing about which tenant ids are real."""
    r = await api.get(_query(**{"from": TODAY, "to": TODAY}, tenant_id=str(uuid4())),
                      principals.finops_all)
    assert r.status_code == 404
    own, other = [await api.get(_query(**{"from": TODAY, "to": TODAY}, tenant_id=str(t)),
                                principals.engineer_a) for t in (seeded.a.id, seeded.b.id)]
    assert own.status_code == other.status_code == 403
    assert own.json()["error"]["code"] == other.json()["error"]["code"]


@pytest.mark.parametrize(("start", "end"), [("2026-02-01", "2026-01-01"),
                                            ("2020-01-01", "2026-01-01")])
async def test_a_backwards_or_huge_range_is_refused(
    api: Api, principals: Principals, start: str, end: str
) -> None:
    r = await api.get(_query(**{"from": start, "to": end}), principals.finops_all)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_range"


def _run(cx: psycopg.Connection[Any], status: str, finished: datetime) -> Any:
    run = uuid4()
    cx.execute(
        "INSERT INTO staging.ingest_run (id, source_system, entity, mode, started_at,"
        " finished_at, status) VALUES (%s, 'servicenow', 'pm_project_task', 'incremental',"
        " %s, %s, %s)", (run, finished, finished, status))
    return run


async def test_the_source_state_and_the_last_successful_sync(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    """``as_of`` is when a sync last succeeded. After PPM is switched off, the
    state says so ("no project data", not zeros), and as_of still says when the
    figures were last read."""
    synced = datetime(2026, 10, 30, 6, 0, 12, tzinfo=UTC)
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        runs = [_run(cx, "succeeded", synced)]
    try:
        r = await api.get(_query(**{"from": TODAY, "to": TODAY}), principals.finops_all)
        assert r.json()["source"] == "available"
        assert datetime.fromisoformat(r.json()["as_of"]) == synced
        delivered = _customer(r.json(), seeded.a)["delivered"]
        assert datetime.fromisoformat(delivered["tasks"]["as_of"]) == synced
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            runs.append(_run(cx, "source_absent", datetime.now(UTC)))
        r = await api.get(_query(**{"from": TODAY, "to": TODAY}), principals.finops_all)
        assert r.json()["source"] == "source_absent"
        assert datetime.fromisoformat(r.json()["as_of"]) == synced
    finally:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("DELETE FROM staging.ingest_run WHERE id = ANY(%s)", (runs,))
