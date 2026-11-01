"""Contract tests for ServiceNow project tasks (ADR 0038) against the provisional
fixtures. No database, no Temporal server: mapping, the price-field switch, the
"table absent" answer, bronze first, the schedule, and the fixtures themselves.

The fields, state values and currency-field shape were read from dev407774 on
2026-11-01; companies and prices are made up. A recording of real records waits
for the read account's access to them.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.servicenow.__main__ import SCHEDULE_EVERY, every
from cloudops_connectors.servicenow.client import ServiceNowClient, SourceTableAbsentError
from cloudops_connectors.servicenow.fake import FIXTURE_HOST, FakeServiceNow, load_fixture
from cloudops_connectors.servicenow.mapping import (
    DEFAULT_PRICE_FIELD,
    PRICE_FIELD_VAR,
    PROJECT_ENTITY,
    MalformedRecordError,
    money,
    normalise_project,
    price_field,
)
from cloudops_connectors.servicenow.workflows import FetchRequest, ServiceNowActivities
from cloudops_devdata.servicenow_fixtures import project_expectations

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _f(v: str, d: str | None = None) -> dict[str, str]:
    return {"value": v, "display_value": v if d is None else d}


def _money(amount: str, entered: str) -> dict[str, str]:
    """A currency field as the instance returns it: converted halves, and the
    amount as entered with its code."""
    return {"value": amount, "display_value": f"${amount}", "currency_display_value": entered}


def _task(**overrides: Any) -> dict[str, Any]:
    base = {
        "sys_id": _f("0" * 32), "number": _f("PRJTASK0000001"),
        "company": _f("c1", "Contoso Retail Ltd"), "state": _f("3", "Closed Complete"),
        "opened_at": _f("2026-09-01 08:00:00"), "closed_at": _f("2026-09-20 17:00:00"),
        "sys_updated_on": _f("2026-09-20 17:05:00"), "top_task": _f("p1", "Onboarding"),
        "cost": _money("13966.8087", "EUR;12500.50"), "work_cost": _money("12.47", "GBP;10.00"),
        "u_unknown_field": _f("keep me"),
    }
    base.update(overrides)
    return base


# --- mapping ----------------------------------------------------------------------


def test_a_closed_task_maps_with_its_price_and_currency() -> None:
    t = normalise_project(_task(), "cost")
    assert (t.status, t.state) == ("closed", "Closed Complete")
    assert (t.price, t.price_currency, t.price_field) == (Decimal("12500.50"), "EUR", "cost")
    assert isinstance(t.price, Decimal)
    assert t.company_raw == "Contoso Retail Ltd"
    assert t.project == "Onboarding"
    assert t.closed_at == datetime(2026, 9, 20, 17, tzinfo=UTC)
    # Unmapped fields kept, the price field among them (it is configurable).
    assert {"u_unknown_field", "cost", "work_cost"} <= set(t.raw_extra)


@pytest.mark.parametrize(("value", "status"), [
    ("3", "closed"), ("4", "closed"), ("7", "closed"),
    ("-5", "open"), ("1", "open"), ("2", "open"),
])
def test_status_comes_from_the_state_value_not_its_label(value: str, status: str) -> None:
    t = normalise_project(_task(state=_f(value, "Anything a person renamed it to")), "cost")
    assert t.status == status


@pytest.mark.parametrize(("field", "expected"), [
    # The shape dev407774 returns (2026-11-01): the entered amount, never the converted one.
    (_money("1379.1834", "EUR;1234.50"), (Decimal("1234.50"), "EUR")),
    (_money("1250.00", "USD;1250.00"), (Decimal("1250.00"), "USD")),
    (_money("-44.69", "EUR;-40.00"), (Decimal("-40.00"), "EUR")),
    (_f("GBP;777.00", "£777.00"), (Decimal("777.00"), "GBP")),  # a stored currency string
    (_money("1379.1834", ""), None),  # converted only: the currency entered is unknown
    (_f("1250.00", "EUR 1,250.00"), None),  # a plain amount is never paired with a code
    (_f("1250.00", "$1,250.00"), None),
    (_f("", ""), None),
    (_money("1", "lots"), None),
])
def test_a_price_is_the_amount_as_entered_with_its_currency(
    field: dict[str, str], expected: tuple[Decimal, str] | None
) -> None:
    assert money({"cost": field}, "cost") == expected


def test_an_unpriced_task_is_kept_without_a_price() -> None:
    t = normalise_project(_task(cost=_money("1250.00", "")), "cost")
    assert (t.price, t.price_currency, t.price_field) == (None, None, None)


@pytest.mark.parametrize("missing", ["sys_id", "number", "state", "sys_updated_on"])
def test_a_task_without_its_keys_is_malformed(missing: str) -> None:
    with pytest.raises(MalformedRecordError):
        normalise_project(_task(**{missing: _f("")}), "cost")


# --- the price-field switch (provisional, ADR 0038) -------------------------------------


def test_the_price_field_defaults_to_cost_and_can_be_switched() -> None:
    assert price_field({}) == DEFAULT_PRICE_FIELD == "cost"
    assert price_field({PRICE_FIELD_VAR: " work_cost "}) == "work_cost"
    switched = normalise_project(_task(), price_field({PRICE_FIELD_VAR: "work_cost"}))
    assert (switched.price, switched.price_currency, switched.price_field) == (
        Decimal("10.00"), "GBP", "work_cost")


@pytest.mark.parametrize("bad", ["cost^ORDERBYsys_id", "Cost", "1cost", "a" * 81, "cost.x"])
def test_the_price_field_must_be_a_field_name(bad: str) -> None:
    with pytest.raises(ValueError, match=PRICE_FIELD_VAR):
        price_field({PRICE_FIELD_VAR: bad})


# --- the source -----------------------------------------------------------------------


async def test_an_instance_without_the_table_says_so_and_is_not_retried() -> None:
    """The answer dev407774 gave on 2026-11-01, with PPM inactive."""
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={
            "error": {"message": "Invalid table pm_project_task", "detail": None},
            "status": "failure"})

    client = ServiceNowClient(FIXTURE_HOST, transport=httpx.MockTransport(handle))
    with pytest.raises(SourceTableAbsentError, match=PROJECT_ENTITY):
        await client.fetch(PROJECT_ENTITY, T0, 0)
    assert calls == 1


async def test_any_other_400_is_still_an_error() -> None:
    client = ServiceNowClient(FIXTURE_HOST, transport=httpx.MockTransport(
        lambda _: httpx.Response(400, json={"error": {"message": "Invalid query"}})))
    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch(PROJECT_ENTITY, T0, 0)


async def test_the_fake_answers_like_an_instance_without_the_table() -> None:
    fake = FakeServiceNow({"incident": load_fixture("incident")})
    client = ServiceNowClient(FIXTURE_HOST, transport=fake.transport())
    with pytest.raises(SourceTableAbsentError):
        await client.fetch(PROJECT_ENTITY, T0, 0)


async def test_a_page_is_in_bronze_before_anything_parses_it() -> None:
    raw = MemoryRawStore()
    client = ServiceNowClient(FIXTURE_HOST, transport=FakeServiceNow.from_fixtures().transport())
    acts = ServiceNowActivities(None, raw, client)  # type: ignore[arg-type]
    result = await ActivityEnvironment().run(
        acts.fetch_page, FetchRequest("run-1", PROJECT_ENTITY, T0.isoformat(), 0, 0))
    assert result.raw_ref.startswith(f"servicenow/{PROJECT_ENTITY}/")
    assert raw.events == [("put", result.raw_ref)]
    assert json.loads(raw.pages[result.raw_ref])["result"]


def test_project_tasks_sync_every_six_hours_tickets_every_five_minutes() -> None:
    """ADR 0037: business data every six hours; migration 0036 tells connector
    health the same interval."""
    assert every(PROJECT_ENTITY) == timedelta(hours=6)
    assert every("incident") == every("change_task") == SCHEDULE_EVERY


# --- the fixtures ------------------------------------------------------------------


def test_the_fixtures_reproduce_the_ground_truth() -> None:
    truth = {e.number: e for e in project_expectations()}
    records = load_fixture(PROJECT_ENTITY)
    assert {r["number"]["value"] for r in records} == set(truth)
    for record in records:
        e = truth[record["number"]["value"]]
        if e.status is None:
            with pytest.raises(MalformedRecordError):
                normalise_project(record, "cost")
            continue
        t = normalise_project(record, "cost")
        assert t.status == e.status
        assert (None if t.price is None else f"{t.price:.2f}", t.price_currency) == (
            e.price, e.currency)


def test_the_fixtures_cover_both_price_forms_both_states_and_an_unpriced_task() -> None:
    records = load_fixture(PROJECT_ENTITY)
    entered = [r["cost"].get("currency_display_value") for r in records]
    assert any(entered)
    assert all(";" not in r["cost"]["value"] for r in records), "value is the converted half"
    assert {e.status for e in project_expectations()} == {"open", "closed", None}
    assert any(e.price is None and e.status for e in project_expectations())


def test_the_fixtures_carry_no_real_data() -> None:
    text = json.dumps(load_fixture(PROJECT_ENTITY))
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    assert "Synthetic project task" in text
