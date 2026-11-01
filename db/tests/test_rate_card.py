"""The rate card in the database (migration 0031, ADR 0033).

Version 1 is the workbook's card, unchanged; history is immutable; only holders
of ``ratecard.publish`` publish, and only into the future; commercial roles read
it and nobody else does, the customer role least of all."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text

from cloudops_commercial import pricing
from cloudops_commercial.ratecard import chain, from_document, to_document
from cloudops_tenancy import Role, TenantScope

from .conftest import Api, Principals, Seeded
from .support import raw_scope, scope

pytestmark = pytest.mark.integration

SEED = Path(__file__).resolve().parents[1] / "migrations/data/rate_card_2026-v2.14.json"
TABLES = (
    "cloudops.rate_card", "cloudops.rate_card_complexity", "cloudops.rate_card_multiplier",
    "cloudops.rate_card_catalogue", "cloudops.rate_card_type_map",
    "cloudops.rate_card_base_band", "cloudops.rate_card_coverage_uplift",
    "cloudops.rate_card_slab", "cloudops.rate_card_tool", "cloudops.rate_card_people_cost",
    "cloudops.rate_card_role", "cloudops.rate_card_health_band",
)
COMMERCIAL = {"ceo", "cfo", "finops", "delivery_lead", "commercial", "auditor",
              "platform_admin"}
DOCUMENTS = text("SELECT document FROM cloudops.rate_card_documents()")


def _seed() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(SEED.read_text(encoding="utf-8"))
    return loaded


async def _documents(app_db: Any, seeded: Seeded, role: Role = Role.CFO) -> list[Any]:
    async with app_db.transaction(scope(seeded.a, role=role)) as conn:
        return [r[0] for r in await conn.execute(DOCUMENTS)]


# --- version 1 is the workbook, unchanged ------------------------------------------------


async def test_version_1_is_the_seed_digit_for_digit(app_db: Any, seeded: Seeded) -> None:
    """The pricing-regression job proves the seed file reproduces the reference
    engine; this proves the database returns exactly that file, so pricing is the
    same before and after the move."""
    docs = await _documents(app_db, seeded)
    first = next(d for d in docs if d["version"] == "2026-v2.14")
    assert from_document(first) == from_document(_seed())
    # Not merely equal as numbers: the same digits, so nothing was truncated.
    assert first["blended_day_rate"] == "266.9932626632895"
    assert first["people_cost"][0]["monthly_cost"] == "1786.9079747305266"


async def test_the_reference_quote_from_the_database(app_db: Any, seeded: Seeded) -> None:
    card = from_document(next(d for d in await _documents(app_db, seeded)
                              if d["version"] == "2026-v2.14"))
    q = pricing.quote(pricing.Engagement(
        customer="", inventory={"Azure Virtual Machines / Amazon EC2": 15,
                                "App Service / AWS Elastic Beanstalk": 260,
                                "Azure Kubernetes Service (AKS) / Amazon EKS": 46},
        monthly_consumption_eur=Decimal("43856")), card)
    seed = pricing.quote(pricing.Engagement(
        customer="", inventory={"Azure Virtual Machines / Amazon EC2": 15,
                                "App Service / AWS Elastic Beanstalk": 260,
                                "Azure Kubernetes Service (AKS) / Amazon EKS": 46},
        monthly_consumption_eur=Decimal("43856")), from_document(_seed()))
    assert q == seed


# --- isolation ---------------------------------------------------------------------------


@pytest.mark.isolation
@pytest.mark.parametrize("role", [r for r in Role if r is not Role.CUSTOMER])
async def test_only_commercial_roles_read_the_rate_card(
    app_db: Any, seeded: Seeded, role: Role
) -> None:
    async with app_db.transaction(scope(seeded.a, role=role)) as conn:
        for table in TABLES:
            n = await conn.scalar(text(f"SELECT count(*) FROM {table}"))
            assert (n > 0) == (role.value in COMMERCIAL), (table, role)


@pytest.mark.isolation
@pytest.mark.parametrize("table", TABLES)
async def test_a_customer_scope_is_refused_the_rate_card(
    app_db: Any, seeded: Seeded, table: str
) -> None:
    """First lock: cloudops_customer holds no grant on any rate card table."""
    with pytest.raises(Exception, match="permission denied"):
        async with app_db.transaction(scope(seeded.a, role=Role.CUSTOMER)) as conn:
            await conn.scalar(text(f"SELECT count(*) FROM {table}"))


@pytest.mark.isolation
@pytest.mark.parametrize("role", ["", "customer", "customer_admin", "CFO", "engineer"])
def test_unknown_and_customer_role_strings_see_no_rate_card(seeded: Seeded, role: str) -> None:
    """Second lock: whatever app.role says, outside the commercial list no row."""
    with raw_scope(seeded.app_dsn, str(seeded.a.id), role) as cx:
        for table in TABLES:
            assert cx.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
        assert cx.execute("SELECT count(*) FROM cloudops.rate_card_documents()"
                          ).fetchone() == (0,)


@pytest.mark.isolation
def test_the_rate_card_policies_are_exactly_these(seeded: Seeded) -> None:
    """No tenant_isolation to widen (no tenant), so the permissive policies are a
    read for anyone the grants let in, an insert for the publish function's owner
    only, and the restrictive commercial list over both."""
    with psycopg.connect(seeded.admin_dsn) as cx:
        found = cx.execute(
            "SELECT c.relname, p.polname, p.polpermissive, p.polcmd::text,"
            " array(SELECT rolname FROM pg_roles WHERE oid = ANY(p.polroles))"
            " FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid"
            " WHERE c.relname LIKE 'rate\\_card%' ORDER BY 1, 2").fetchall()
        forced = cx.execute(
            "SELECT count(*) FROM pg_class WHERE relname LIKE 'rate\\_card%'"
            " AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity").fetchone()
    assert forced == (len(TABLES),)
    expected = []
    for table in sorted(t.split(".")[1] for t in TABLES):
        expected += [(table, "commercial_visibility", False, "*", []),
                     (table, "rate_card_publish", True, "a", ["cloudops_access_admin"]),
                     (table, "rate_card_read", True, "r", [])]
    assert found == expected


@pytest.mark.isolation
async def test_the_customer_role_cannot_read_the_documents(app_db: Any, seeded: Seeded) -> None:
    with pytest.raises(Exception, match="permission denied"):
        async with app_db.transaction(scope(seeded.a, role=Role.CUSTOMER)) as conn:
            await conn.execute(DOCUMENTS)


@pytest.mark.isolation
@pytest.mark.parametrize("statement", [
    "UPDATE cloudops.rate_card SET blended_day_rate = 1",
    "DELETE FROM cloudops.rate_card_role",
    "UPDATE cloudops.rate_card_people_cost SET monthly_cost = 1",
    "TRUNCATE cloudops.rate_card_health_band",
])
def test_history_is_immutable_even_for_a_superuser(seeded: Seeded, statement: str) -> None:
    """Row-level security already leaves the owner no row to change (no UPDATE or
    DELETE policy); the trigger holds even where RLS does not apply."""
    with psycopg.connect(seeded.admin_dsn) as cx, pytest.raises(
            psycopg.errors.InsufficientPrivilege, match="immutable"):
        cx.execute(statement)


def test_the_owner_finds_no_row_to_change(seeded: Seeded) -> None:
    with psycopg.connect(seeded.migrator_dsn) as cx:
        cx.execute("SELECT set_config('app.role', 'platform_admin', true)")
        assert cx.execute("UPDATE cloudops.rate_card SET blended_day_rate = 1").rowcount == 0
        assert cx.execute("DELETE FROM cloudops.rate_card_role").rowcount == 0
        cx.rollback()


@pytest.mark.isolation
async def test_the_application_cannot_write_the_tables_directly(
    app_db: Any, seeded: Seeded
) -> None:
    with pytest.raises(Exception, match="permission denied"):
        async with app_db.transaction(scope(seeded.a, role=Role.PLATFORM_ADMIN)) as conn:
            await conn.execute(text(
                "INSERT INTO cloudops.rate_card_role (rate_card_id, position, role, share)"
                " SELECT id, 99, 'x', 0 FROM cloudops.rate_card LIMIT 1"))


@pytest.mark.isolation
@pytest.mark.parametrize("role", ["Customer viewer", "Engineer", "SecOps"])
async def test_the_matrix_cannot_give_publish_to_a_non_commercial_role(
    app_db: Any, principals: Principals, role: str
) -> None:
    """D10: ratecard.publish is commercial; 0004's trigger keeps it off every
    class the commercial policy does not admit, customer roles above all."""
    with pytest.raises(Exception, match="commercial permission"):
        async with app_db.transaction(TenantScope.for_resolution("test")) as conn:
            await conn.execute(text(
                "SELECT access.grant_permission(:a, :r, 'ratecard.publish')"),
                {"a": principals.admin, "r": role})


# --- publishing --------------------------------------------------------------------------


def _next(version: str, effective: str, **change: Any) -> dict[str, Any]:
    return {"document": {**_seed(), "version": version, "effective_from": effective, **change},
            "reason": "Annual review (test)"}


@pytest.mark.isolation
async def test_publishing_is_refused_without_the_permission(
    api: Api, principals: Principals
) -> None:
    body = _next("refused-1", "2090-01-01")
    for who in (principals.finops_all, principals.engineer_a, principals.customer_a):
        r = await api.post("/v1/commercial/rate-cards", who, body)
        assert r.status_code == 403, who
    listed = await api.get("/v1/commercial/rate-cards", principals.admin)
    assert "refused-1" not in {v["version"] for v in listed.json()}


@pytest.mark.isolation
async def test_the_database_refuses_a_publisher_without_the_permission(
    app_db: Any, seeded: Seeded, principals: Principals
) -> None:
    """The function checks the matrix itself, whatever the API did."""
    with pytest.raises(Exception, match="not permitted"):
        async with app_db.transaction(scope(seeded.a, role=Role.FINOPS)) as conn:
            await conn.execute(text(
                "SELECT access.publish_rate_card(:a, CAST(:d AS jsonb), 'x-test')"),
                {"a": principals.finops_all,
                 "d": json.dumps({**_seed(), "version": "sql-1",
                                  "effective_from": "2091-01-01"})})


async def test_a_new_version_takes_effect_without_touching_the_old(
    api: Api, principals: Principals, app_db: Any, seeded: Seeded
) -> None:
    before = await _documents(app_db, seeded)
    body = _next("2099-test", "2099-01-01", blended_day_rate="300",
                 people_cost=[{"location": "India", "monthly_cost": "2000"},
                              {"location": "Budapest", "monthly_cost": "8000"}])
    r = await api.post("/v1/commercial/rate-cards", principals.admin, body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["version"] == "2099-test"
    assert created["effective_to"] is None
    assert created["in_force"] is False
    assert created["document"]["blended_day_rate"] == "300"

    after = await _documents(app_db, seeded)
    assert after[: len(before)] == before, "a published version never changes"
    cards = chain(after)
    previous = next(c for c in cards if c.version == before[-1]["version"])
    assert previous.effective_to == date(2099, 1, 1)

    # Quotes before the date price on the old card, from it on the new one.
    quote = {"inventory": {"Azure Virtual Machines / Amazon EC2": 10},
             "monthly_consumption": "1000", "currency": "EUR"}
    old = await api.post("/v1/commercial/quotes", principals.admin,
                         {**quote, "on": "2098-12-31"})
    new = await api.post("/v1/commercial/quotes", principals.admin, {**quote, "on": "2099-01-01"})
    assert old.json()["rate_card_version"] == previous.version
    assert new.json()["rate_card_version"] == "2099-test"
    assert new.json()["service_cost"]["value"] != old.json()["service_cost"]["value"]

    # The past, and the same version again, are refused.
    past = await api.post("/v1/commercial/rate-cards", principals.admin,
                          _next("2098-test", "2098-06-01"))
    assert past.status_code == 422
    assert past.json()["error"]["code"] == "effective_date_not_future"
    again = await api.post("/v1/commercial/rate-cards", principals.admin,
                           _next("2099-test", "2100-01-01"))
    assert again.status_code == 409


@pytest.mark.parametrize("change", [
    {"roles": [{"role": "Engineer", "share": "0.5"}]},
    {"blended_day_rate": 266.99},  # a number, not a decimal string
    {"people_cost": [{"location": "Mixed", "monthly_cost": "1"}]},
    {"service_multipliers": []},
])
async def test_a_card_that_cannot_price_is_refused(
    api: Api, principals: Principals, change: dict[str, Any]
) -> None:
    r = await api.post("/v1/commercial/rate-cards", principals.admin,
                       _next("bad-1", "2120-01-01", **change))
    assert r.status_code == 422


async def test_the_listing_is_newest_first_and_round_trips(
    api: Api, principals: Principals
) -> None:
    r = await api.get("/v1/commercial/rate-cards", principals.finops_all)
    assert r.status_code == 200
    versions = r.json()
    assert [v["effective_from"] for v in versions] == sorted(
        (v["effective_from"] for v in versions), reverse=True)
    seed = next(v for v in versions if v["version"] == "2026-v2.14")
    assert seed["published_by"] == "migration:0031"
    assert from_document(seed["document"]) == from_document(_seed())
    assert to_document(from_document(seed["document"]))["roles"] == _seed()["roles"]
