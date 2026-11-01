"""The connector health band (cloudops_api.connectors.assess), on its own: the
LLD's three-interval outage, lateness, never run, and attention raising the band."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from cloudops_api.connectors import FAILING_RATE, Figures, assess, rate

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
EVERY = 300  # five minutes

QUIET = Figures(every_seconds=EVERY, last_success_at=NOW - timedelta(seconds=60),
                last_run_at=NOW - timedelta(seconds=60), last_status="succeeded",
                runs_24h=288, failed_runs_24h=0, records_24h=1000, rejected_24h=0,
                dead_letters_open=0, drift_open=0)


def ago(seconds: int) -> Figures:
    at = NOW - timedelta(seconds=seconds)
    return replace(QUIET, last_success_at=at, last_run_at=at)


def test_a_recent_success_is_healthy() -> None:
    a = assess(QUIET, NOW)
    assert (a.band, a.timeliness, a.reasons) == ("healthy", "on_time", ())
    assert a.age_seconds == 60
    assert a.intervals == Decimal("0.20")
    assert a.failed_run_rate == Decimal("0.0000")


@pytest.mark.parametrize(("seconds", "band", "timeliness"), [
    (EVERY, "healthy", "on_time"),  # exactly one interval: not yet late
    (EVERY + 1, "at-risk", "late"),
    (3 * EVERY, "at-risk", "late"),  # exactly three: late, not yet an outage
    (3 * EVERY + 1, "critical", "outage"),  # more than three intervals: the LLD's P2 page
    (30 * EVERY, "critical", "outage"),
])
def test_lateness_is_measured_in_intervals(seconds: int, band: str, timeliness: str) -> None:
    a = assess(ago(seconds), NOW)
    assert (a.band, a.timeliness) == (band, timeliness)
    assert a.age_seconds == seconds


def test_never_run_is_unknown_not_healthy_and_not_an_outage() -> None:
    a = assess(replace(QUIET, last_success_at=None, last_run_at=None, last_status=None,
                       runs_24h=0, records_24h=0), NOW)
    assert (a.band, a.timeliness, a.reasons) == ("unknown", "never_run", ("never_run",))
    assert a.age_seconds is None
    assert a.intervals is None
    # Nothing to divide by: absent, never zero.
    assert a.failed_run_rate is None
    assert a.rejected_rate is None


def test_a_first_run_still_running_is_never_run() -> None:
    a = assess(replace(QUIET, last_success_at=None, last_status="running", runs_24h=0,
                       records_24h=0), NOW)
    assert (a.band, a.timeliness) == ("unknown", "never_run")


def test_runs_that_never_succeeded_are_an_outage() -> None:
    a = assess(replace(QUIET, last_success_at=None, last_status="failed", runs_24h=3,
                       failed_runs_24h=3), NOW)
    assert (a.band, a.timeliness) == ("critical", "outage")
    assert "no_success" in a.reasons
    assert "failing" in a.reasons


@pytest.mark.parametrize(("change", "reason"), [
    ({"failed_runs_24h": 1}, "failed_runs"),
    ({"rejected_24h": 4}, "rejected_records"),
    ({"dead_letters_open": 1}, "dead_letters"),
    ({"drift_open": 2}, "schema_drift"),
    ({"last_status": "dead_lettered"}, "last_run_failed"),
])
def test_attention_raises_a_healthy_connector_to_watch(change: dict[str, object],
                                                       reason: str) -> None:
    a = assess(replace(QUIET, **change), NOW)  # type: ignore[arg-type]
    assert a.band == "watch"
    assert reason in a.reasons


def test_attention_never_lowers_a_worse_band() -> None:
    a = assess(replace(ago(4 * EVERY), drift_open=1), NOW)
    assert a.band == "critical"
    assert a.reasons == ("outage", "schema_drift")


def test_a_failing_run_rate_raises_to_at_risk() -> None:
    a = assess(replace(QUIET, runs_24h=4, failed_runs_24h=2), NOW)
    assert a.failed_run_rate == FAILING_RATE
    assert a.band == "at-risk"
    assert "failing" in a.reasons
    below = assess(replace(QUIET, runs_24h=4, failed_runs_24h=1), NOW)
    assert below.band == "watch"


def test_attention_on_a_never_run_connector_is_shown() -> None:
    a = assess(replace(QUIET, last_success_at=None, last_run_at=None, last_status=None,
                       drift_open=1), NOW)
    assert a.band == "watch"
    assert a.timeliness == "never_run"


def test_rates_are_decimals_to_four_places_half_up() -> None:
    assert rate(1, 3) == Decimal("0.3333")
    assert rate(2, 3) == Decimal("0.6667")
    assert rate(1, 8) == Decimal("0.1250")
    assert rate(0, 0) is None
    assert isinstance(rate(1, 2), Decimal)


def test_a_source_without_the_table_is_its_own_state_not_an_outage() -> None:
    """ADR 0038: ServiceNow project tasks while Project Portfolio Management is
    inactive. Runs, never a success, and still not critical: nothing is broken."""
    a = assess(replace(QUIET, last_success_at=None, last_status="source_absent",
                       runs_24h=4, failed_runs_24h=0), NOW)
    assert (a.band, a.timeliness, a.reasons) == ("unknown", "source_absent", ("source_absent",))
    assert a.age_seconds is None
