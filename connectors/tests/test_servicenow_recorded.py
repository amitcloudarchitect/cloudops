"""Contract tests against records from a real instance (ADR 0009), recorded and
scrubbed by tools/servicenow_record.py. The synthetic fixtures prove the logic;
these prove the logic holds for what a real instance actually sends."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cloudops_connectors.servicenow.mapping import (
    DEFAULT_PRICE_FIELD,
    ENTITIES,
    PROJECT_ENTITY,
    normalise,
    normalise_project,
)

RECORDED = Path(__file__).parent / "recorded"
INSTANCES = sorted(p.name for p in RECORDED.iterdir() if p.is_dir())
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PERSON = {"assigned_to", "caller_id", "closed_by", "opened_by", "reopened_by", "resolved_by",
          "sys_created_by", "sys_updated_by", "watch_list", "work_notes_list"}


def _records(instance: str, entity: str) -> list[dict[str, Any]]:
    data = json.loads((RECORDED / instance / f"{entity}.json").read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = data["result"]
    return records


def test_there_is_at_least_one_recorded_instance() -> None:
    assert INSTANCES


@pytest.mark.parametrize("instance", INSTANCES)
@pytest.mark.parametrize("entity", sorted(ENTITIES))
def test_every_real_record_normalises(instance: str, entity: str) -> None:
    records = _records(instance, entity)
    assert records
    for record in records:
        normalise(entity, record)  # raises MalformedRecordError on any it cannot read


@pytest.mark.parametrize("instance", INSTANCES)
@pytest.mark.parametrize("entity", sorted(ENTITIES))
def test_records_have_the_shape_the_client_asks_for(instance: str, entity: str) -> None:
    """display_value=all: every field a {value, display_value} pair."""
    for record in _records(instance, entity):
        for name in ("number", "sys_id", "sys_updated_on", "company"):
            assert set(record[name]) >= {"value", "display_value"}, name


@pytest.mark.parametrize("instance", INSTANCES)
@pytest.mark.parametrize("entity", sorted(ENTITIES))
def test_recordings_carry_no_personal_data(instance: str, entity: str) -> None:
    text = (RECORDED / instance / f"{entity}.json").read_text(encoding="utf-8")
    assert not _EMAIL.search(text)
    for record in _records(instance, entity):
        for name in PERSON & record.keys():
            shown = record[name]["display_value"]
            assert shown == "" or re.fullmatch(r"Person \d+(, Person \d+)*", shown), name
        for name in ("description", "comments", "work_notes"):
            if name in record:
                assert record[name]["display_value"] in ("", "[redacted]"), name


# --- project tasks (ADR 0038): recorded from dev407774 once PPM was active ---------

PROJECTS = [i for i in INSTANCES if (RECORDED / i / f"{PROJECT_ENTITY}.json").exists()]


@pytest.mark.parametrize("instance", PROJECTS)
def test_every_real_project_task_normalises_with_its_entered_price(instance: str) -> None:
    """The price is the amount as entered, with its code (``currency_display_value``),
    never ``value``, which the instance converts to its reference currency."""
    tasks = [normalise_project(r, DEFAULT_PRICE_FIELD) for r in _records(instance, PROJECT_ENTITY)]
    assert tasks
    for task, record in zip(tasks, _records(instance, PROJECT_ENTITY), strict=True):
        entered = record["cost"]["currency_display_value"]
        assert (task.price_currency, f"{task.price:.2f}") == tuple(entered.split(";"))
        assert task.price != Decimal(record["cost"]["value"]) or task.price_currency == "USD"
    assert {t.status for t in tasks} == {"open", "closed"}


@pytest.mark.parametrize("instance", PROJECTS)
def test_project_task_text_that_names_an_estate_is_redacted_everywhere(instance: str) -> None:
    """A project task's short description names the customer's resource groups and
    networks, and ServiceNow echoes it in ``project`` and ``task``."""
    for record in _records(instance, PROJECT_ENTITY):
        assert record["short_description"]["display_value"] in ("", "[redacted]")
        for name in ("project", "task"):
            shown = record[name]["display_value"]
            assert shown == "" or shown.startswith("[redacted]"), name
