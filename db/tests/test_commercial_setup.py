"""Settings → Commercial setup (migration 0033, ADR 0035): the SOW register and
"recompute margin now".

A SOW version is never edited: an amendment is a new version and the previous
one only gains the day it ends. Only staff holding ``commercial.write`` record
one, for a customer they are assigned; customers and engineers read and write
nothing; the margin worker answers a recompute request with the entered figures.
Money is checked in two contract currencies."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_connectors.finops import jobs
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Api, Principals, Seeded
from .support import scope

pytestmark = pytest.mark.integration

TODAY = date.today()
START = TODAY.replace(day=1) - timedelta(days=60)


class Fresh:
    def __init__(self, tenant_id: UUID) -> None:
        self.id = tenant_id


@pytest.fixture(scope="module")
def fresh(seeded: Seeded) -> Iterator[tuple[Fresh, Fresh]]:
    """Two customers with no SOW: one contracted in EUR, one in GBP."""
    made = (Fresh(uuid4()), Fresh(uuid4()))
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for i, t in enumerate(made):
            cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code)"
                       " VALUES (%s, %s, %s)", (t.id, f"Setup test {i} {t.id}",
                                                f"ST{i}{str(t.id)[:6].upper()}"))
    yield made
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("SET session_replication_role = replica")  # the history trigger
        for t in made:
            cx.execute("DELETE FROM cloudops.margin_variance WHERE tenant_id = %s", (t.id,))
            cx.execute("DELETE FROM cloudops.margin_snapshot WHERE tenant_id = %s", (t.id,))
            cx.execute("DELETE FROM cloudops.tenant WHERE id = %s", (t.id,))


def _sow(**change: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "reference": "SOW-TEST-001",
        "effective_from": START.isoformat(),
        "term_start": START.isoformat(),
        "term_end": (START + timedelta(days=3 * 365)).isoformat(),
        "currency": "EUR",
        "service_scope": "Managed cloud operations, Azure, 24x7 Gold",
        "coverage": "24x7",
        "support": "Gold",
        "recurring_fee": "30000",
        "billing_frequency": "quarterly",
        "target_margin": "0.25",
        "consumption_uplift": "0.05",
        "document_uri": "SharePoint: Contracts/Test/SOW-TEST-001.pdf",
    }
    return {**body, **change}


def _path(tenant: UUID, tail: str = "sows") -> str:
    return f"/v1/commercial/customers/{tenant}/{tail}"


def _value(metric: dict[str, Any] | None) -> Decimal | None:
    return None if metric is None else Decimal(str(metric["value"]))


# --- versioning, in two currencies ------------------------------------------------------


async def test_an_amendment_is_a_new_version_and_never_rewrites_history(
    api: Api, principals: Principals, fresh: tuple[Fresh, Fresh]
) -> None:
    eur, _ = fresh
    first = await api.post(_path(eur.id), principals.admin, _sow())
    assert first.status_code == 201, first.text
    v1 = first.json()["versions"][0]
    assert _value(v1["version"]) == 1
    assert _value(v1["monthly_manage_revenue"]) == Decimal(10000)  # 30000 a quarter
    assert v1["monthly_manage_revenue"]["unit"] == "EUR"
    assert v1["effective_to"] == _sow()["term_end"]
    assert v1["entered_by"] == str(principals.admin)

    amended_from = START + timedelta(days=45)
    no_reason = await api.post(_path(eur.id), principals.admin,
                               _sow(effective_from=amended_from.isoformat()))
    assert no_reason.status_code == 422, "an amendment says why"
    backdated = await api.post(_path(eur.id), principals.admin, _sow(
        effective_from=START.isoformat(), amendment_reason="Backdated (test)"))
    assert backdated.status_code == 422
    assert backdated.json()["error"]["code"] == "effective_date_not_after_current"

    second = await api.post(_path(eur.id), principals.admin, _sow(
        effective_from=amended_from.isoformat(), recurring_fee="132000",
        billing_frequency="annual", amendment_reason="Scope extended to AWS (test)"))
    assert second.status_code == 201, second.text
    v2, old = second.json()["versions"]
    assert _value(v2["version"]) == 2
    assert _value(v2["monthly_manage_revenue"]) == Decimal(11000)
    assert v2["amendment_reason"] == "Scope extended to AWS (test)"
    # The first version is unchanged except for the day it now ends.
    assert old["effective_to"] == (amended_from - timedelta(days=1)).isoformat()
    assert {k: v for k, v in old.items() if k not in ("effective_to", "in_force")} == {
        k: v for k, v in v1.items() if k not in ("effective_to", "in_force")}


async def test_a_gbp_contract_keeps_its_currency(
    api: Api, principals: Principals, fresh: tuple[Fresh, Fresh]
) -> None:
    _, gbp = fresh
    r = await api.post(_path(gbp.id), principals.admin, _sow(
        currency="GBP", recurring_fee="100000", billing_frequency="annual",
        hosting_acv="24000.50", manage_acv="100000", professional_services="1500.25"))
    assert r.status_code == 201, r.text
    v = r.json()["versions"][0]
    # 100000 / 12, stored at six places; rounded once, by the console.
    assert _value(v["monthly_manage_revenue"]) == Decimal("8333.333333")
    for key in ("recurring_fee", "monthly_manage_revenue", "hosting_acv", "manage_acv",
                "professional_services"):
        assert v[key]["unit"] == "GBP", key
    assert _value(v["target_margin"]) == Decimal("0.25")
    assert v["target_margin"]["unit"] == "ratio"


@pytest.mark.parametrize("change", [
    {"recurring_fee": 30000},  # a JSON number: never accepted as money
    {"recurring_fee": "30000.1234567"},
    {"consumption_uplift": "0"},  # ADR 0021: above 0
    {"target_margin": "1.5"},
    {"term_end": (START - timedelta(days=1)).isoformat()},
    {"billing_frequency": "weekly"},
    {"document_uri": ""},
])
async def test_an_invalid_sow_is_refused(
    api: Api, principals: Principals, fresh: tuple[Fresh, Fresh], change: dict[str, Any]
) -> None:
    eur, _ = fresh
    r = await api.post(_path(eur.id), principals.admin, _sow(**change))
    assert r.status_code == 422


@pytest.mark.isolation
@pytest.mark.parametrize("statement", [
    "UPDATE cloudops.sow SET monthly_manage_revenue = 1",
    "UPDATE cloudops.sow SET reference = 'changed'",
    "UPDATE cloudops.sow SET effective_to = NULL",
    "UPDATE cloudops.sow SET effective_to = effective_to + 30",
])
def test_no_one_edits_a_version(seeded: Seeded, statement: str) -> None:
    """Even the superuser: the figures are history, and an end date only moves
    earlier, as an amendment moves it."""
    with psycopg.connect(seeded.admin_dsn) as cx:
        # Closing an open-ended version is what an amendment does: allowed.
        cx.execute("UPDATE cloudops.sow SET effective_to = '2099-12-31'"
                   " WHERE effective_to IS NULL")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="never edited"):
            cx.execute(statement + " WHERE effective_to IS NOT NULL")
        cx.rollback()


@pytest.mark.isolation
async def test_the_application_login_cannot_edit_a_version_either(
    app_db: Database, seeded: Seeded
) -> None:
    with pytest.raises(Exception, match="never edited"):
        async with app_db.transaction(scope(seeded.a, role=Role.CFO)) as conn:
            await conn.execute(text("UPDATE cloudops.sow SET monthly_manage_revenue = 1"))


# --- who may ----------------------------------------------------------------------------


@pytest.mark.isolation
async def test_reading_and_recording_are_refused_without_the_permission(
    api: Api, principals: Principals, seeded: Seeded
) -> None:
    for who in (principals.engineer_a, principals.customer_a):
        r = await api.get(_path(seeded.a.id), who)
        assert r.status_code == 403, who
    for who in (principals.finops_all, principals.engineer_a, principals.customer_a,
                principals.tenant_admin_a):
        for tail, body in (("sows", _sow()), ("margin-recompute", None)):
            r = await api.post(_path(seeded.a.id, tail), who, body)
            assert r.status_code == 403, (who, tail)
    # FinOps reads the register (commercial.read); it may not write it.
    assert (await api.get(_path(seeded.a.id), principals.finops_all)).status_code == 200


@pytest.mark.isolation
async def test_the_database_refuses_a_writer_without_the_permission(
    app_db: Database, seeded: Seeded, principals: Principals
) -> None:
    """The function checks the matrix itself, whatever the API did."""
    with pytest.raises(Exception, match="not permitted"):
        async with app_db.transaction(scope(seeded.a, role=Role.FINOPS)) as conn:
            await conn.execute(text(
                "SELECT access.record_sow(:a, :t, CAST(:s AS jsonb))"),
                {"a": principals.finops_all, "t": seeded.a.id, "s": "{}"})


@pytest.mark.isolation
async def test_a_writer_cannot_reach_a_customer_outside_its_scope(
    api: Api, app_db: Database, principals: Principals, seeded: Seeded
) -> None:
    """A Commercial principal assigned to tenant a: tenant b is not found, through
    the API and through the function under a scope that names b."""
    writer = uuid4()
    async with app_db.transaction(TenantScope.for_resolution("test:writer")) as conn:
        await conn.execute(text("SELECT access.set_principal(:a, :o, 'staff', 'w', "
                                "'Commercial', false)"), {"a": principals.admin, "o": writer})
        await conn.execute(text("SELECT access.grant_tenant(:a, :o, :t)"),
                           {"a": principals.admin, "o": writer, "t": seeded.a.id})
    r = await api.post(_path(seeded.b.id), writer, _sow())
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    with pytest.raises(Exception, match="tenant not found"):
        async with app_db.transaction(scope(seeded.b, role=Role.COMMERCIAL)) as conn:
            await conn.execute(text("SELECT access.record_sow(:a, :t, CAST(:s AS jsonb))"),
                               {"a": writer, "t": seeded.b.id, "s": "{}"})


@pytest.mark.isolation
@pytest.mark.parametrize("role", ["Customer viewer", "Engineer", "SecOps"])
async def test_the_matrix_cannot_give_commercial_write_to_a_non_commercial_role(
    app_db: Database, principals: Principals, role: str
) -> None:
    with pytest.raises(Exception, match="commercial permission"):
        async with app_db.transaction(TenantScope.for_resolution("test")) as conn:
            await conn.execute(text(
                "SELECT access.grant_permission(:a, :r, 'commercial.write')"),
                {"a": principals.admin, "r": role})


# --- recompute now -------------------------------------------------------------------------


async def test_recompute_now_is_answered_by_the_worker_with_the_entered_figures(
    api: Api, principals: Principals, fresh: tuple[Fresh, Fresh], seeded: Seeded,
    ingest_db: Database, app_db: Database
) -> None:
    _, gbp = fresh
    first = await api.post(_path(gbp.id, "margin-recompute"), principals.admin)
    again = await api.post(_path(gbp.id, "margin-recompute"), principals.admin)
    assert first.status_code == again.status_code == 202
    assert first.json()["status"] == "requested"
    assert again.json()["id"] == first.json()["id"], "one waiting request per customer"

    report = await jobs.recompute_pending(ingest_db, TODAY)
    assert report.succeeded >= 1
    assert report.failed == 0
    register = (await api.get(_path(gbp.id), principals.admin)).json()
    assert register["recompute"]["status"] == "succeeded"
    assert register["recompute"]["finished_at"] is not None

    # The month to date (the daily run's month) now has a figure: revenue from the entered fee, in
    # GBP. With no billing data yet it is not captured, never a zero margin.
    async with app_db.transaction(TenantScope([gbp.id], Role.CFO, "test")) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT contract_currency, revenue, not_captured FROM cloudops.margin_snapshot"
            " WHERE tenant_id = :t AND period = :p"),
            {"t": gbp.id, "p": jobs.month_start(TODAY - timedelta(days=1))})]
    assert rows == [("GBP", Decimal("8333.333333"), "no_cost_data")]


@pytest.mark.isolation
async def test_recompute_requests_are_commercial_and_per_customer(
    api: Api, principals: Principals, seeded: Seeded, app_db: Database
) -> None:
    r = await api.post(_path(seeded.a.id, "margin-recompute"), principals.admin)
    assert r.status_code == 202
    count = text("SELECT count(*) FROM cloudops.margin_recompute")
    async with app_db.transaction(scope(seeded.a, role=Role.ENGINEER)) as conn:
        assert await conn.scalar(count) == 0
    async with app_db.transaction(scope(seeded.b, role=Role.CFO)) as conn:
        assert await conn.scalar(text(
            "SELECT count(*) FROM cloudops.margin_recompute WHERE tenant_id = :t"),
            {"t": seeded.a.id}) == 0
    async with app_db.transaction(scope(seeded.a, role=Role.CFO)) as conn:
        assert await conn.scalar(count) >= 1
    limited = TenantScope([seeded.a.id], Role.CFO, "test", [seeded.a.account_id])
    async with app_db.transaction(limited) as conn:
        assert await conn.scalar(count) == 0, "per customer, never per subscription"
    with pytest.raises(Exception, match="permission denied"):
        async with app_db.transaction(scope(seeded.a, role=Role.CUSTOMER)) as conn:
            await conn.scalar(count)


@pytest.mark.isolation
def test_a_request_only_moves_forward(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx, pytest.raises(
            psycopg.errors.InsufficientPrivilege, match="only move forward"):
        cx.execute("UPDATE cloudops.margin_recompute SET status = 'requested',"
                   " started_at = NULL, finished_at = NULL WHERE status = 'succeeded'")
