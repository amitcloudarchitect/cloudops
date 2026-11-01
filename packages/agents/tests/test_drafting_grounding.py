"""The report narrator's number check (``drafting.numbers_grounded``): dates, times
and ids in a section's data ground no figure in the narrative."""

from __future__ import annotations

from typing import Any

import pytest

from cloudops_agents.drafting import numbers_grounded

SECTION: dict[str, Any] = {
    "period": "October 2026",
    "figures": [{"key": "incidents_opened", "value": 42},
                {"key": "sla_attainment_pct", "value": "91.67"},
                {"key": "spend", "value": "1250.500000", "currency": "EUR"}],
    "tables": [{"number": "INC0012345", "opened_at": "2026-10-14T06:47:12Z",
                "updated": "2026-10-30 09:15:03.481+00:00", "due": "18:30",
                "id": "3f2a9c47-0b1d-4e8a-9c2f-12ab34cd5678"}],
}


@pytest.mark.parametrize("made_up", ["47", "12", "6", "14", "10", "30", "9", "15", "481",
                                     "18", "4", "5678", "12345"])
def test_dates_times_and_ids_do_not_ground_a_number(made_up: str) -> None:
    """Each of these occurs only inside a timestamp, a time or an id."""
    assert numbers_grounded(f"{made_up} incidents breached their SLA.", SECTION) == (
        False, [made_up])


def test_figures_beside_timestamps_are_still_grounded() -> None:
    assert numbers_grounded("42 incidents were opened in October 2026, and 91.67% "
                            "were resolved within SLA.", SECTION) == (True, [])
    assert numbers_grounded("Spend was 1250.5 EUR, about 1251 EUR.", SECTION) == (True, [])
    assert numbers_grounded("42 incidents, 8 fewer than last month.", SECTION) == (
        False, ["8"])
    assert numbers_grounded("INC0012345 was the longest open.", SECTION) == (True, [])


def test_a_date_or_time_in_the_narrative_must_be_in_the_data() -> None:
    assert numbers_grounded("INC0012345 opened on 2026-10-14 at 06:47.", SECTION) == (
        True, [])
    assert numbers_grounded("It was updated 2026-10-30 09:15, due 18:30.", SECTION) == (
        True, [])
    assert numbers_grounded("It opened on 2026-10-15.", SECTION) == (False, ["2026-10-15"])
    assert numbers_grounded("It opened at 06:48.", SECTION) == (False, ["06:48"])


def test_a_fraction_does_not_ground_a_percentage_here() -> None:
    """Unlike the assistant, the narrator is given percentages as percentages."""
    assert numbers_grounded("Attainment was 21.53%.", {"fraction": "0.2153"}) == (
        False, ["21.53"])
