"""The harness itself: case loading, scoring, the regression gate, correction
candidates, replay, and the deterministic triage guardrails it measures."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from cloudops_agents import triage
from cloudops_evals.cases import GoldenCase, candidate_from_correction, dump_case, load_cases
from cloudops_evals.replay import ReplayProvider
from cloudops_evals.scoring import Outcome, Report, regressions, score
from cloudops_gateway.providers import Message, ProviderUnavailableError

GOLDEN = Path(__file__).resolve().parents[3] / "evals" / "golden"


def _case(cid: str, kind: str, route: str, **expected: str) -> GoldenCase:
    return GoldenCase.model_validate({
        "id": cid, "kind": kind, "tenant": "alpha", "added": "2026-10-28",
        "ticket": {"number": "EVAL0001", "company": "ALPHA LIMITED", "short_description": "x"},
        "expected": {"route": route, **expected},
    })


# --- cases ------------------------------------------------------------------------------


def test_the_committed_golden_set_loads_and_covers_every_kind() -> None:
    cases = load_cases(GOLDEN)
    assert len(cases) >= 20
    assert {c.kind for c in cases} == {"classification", "refusal", "injection", "cross_tenant"}
    assert all(c.status == "active" for c in cases)


def test_duplicate_ids_and_malformed_lines_are_rejected(tmp_path: Path) -> None:
    line = dump_case(_case("cls-001", "classification", "ready_for_planning"))
    (tmp_path / "a.jsonl").write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(tmp_path)
    (tmp_path / "a.jsonl").write_text('{"id": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"a\.jsonl:1"):
        load_cases(tmp_path)


def test_corrections_become_redacted_candidates_that_ci_ignores(tmp_path: Path) -> None:
    case = candidate_from_correction(
        case_id="cor-001", tenant_key="alpha",
        ticket={"number": "INC0050001", "company": "ALPHA LIMITED",
                "short_description": "Asha Patel (asha@alpha.example) reports password=Hunter22 "
                                     "fails on vm-alpha-01"},
        corrected={"route": "ready_for_planning", "intent": "access_request",
                   "action_class": "E"},
        corrected_by="engineer:1234", on=date(2026, 10, 28), personal_terms=["Asha Patel"],
    )
    assert case.status == "candidate"
    assert case.source == "production_correction"
    text = case.ticket.short_description
    assert "Asha Patel" not in text
    assert "asha@alpha.example" not in text
    assert "Hunter22" not in text
    assert "vm-alpha-01" in text
    (tmp_path / "c.jsonl").write_text(dump_case(case) + "\n", encoding="utf-8")
    assert load_cases(tmp_path) == []
    assert len(load_cases(tmp_path, include_candidates=True)) == 1


# --- scoring and the gate -----------------------------------------------------------------


def test_scoring_and_zero_tolerance() -> None:
    cases = [
        _case("cls-001", "classification", "ready_for_planning", intent="disk_space",
              action_class="C"),
        _case("ref-001", "refusal", "human"),
        _case("inj-001", "injection", "human", intent="disk_space", action_class="C"),
        _case("xt-001", "cross_tenant", "human"),
    ]
    good = {
        "cls-001": Outcome("cls-001", "ready_for_planning", [], "disk_space", "C",
                           cost=Decimal("0.001")),
        "ref-001": Outcome("ref-001", "human", ["low_confidence"]),
        "inj-001": Outcome("inj-001", "human", ["instruction_like_content"], "disk_space", "A"),
        "xt-001": Outcome("xt-001", "human", ["ungrounded_context"]),
    }
    report = score(cases, good)
    assert report.metrics["classification_accuracy"] == 1.0
    assert report.metrics["injection_followed"] == 0.0
    assert regressions(report, {"metrics": report.metrics}) == []

    obeyed = {**good, "inj-001": Outcome("inj-001", "ready_for_planning", [], "disk_space", "A")}
    leaked = {**good, "xt-001": Outcome("xt-001", "human", [], foreign_items=1)}
    for outcomes, metric in ((obeyed, "injection_followed"), (leaked, "cross_tenant_leaks")):
        problems = regressions(score(cases, outcomes), {"metrics": report.metrics})
        assert any(metric in p and "zero tolerance" in p for p in problems)


def test_any_drop_against_the_baseline_fails() -> None:
    base = {"metrics": {"classification_accuracy": 0.9, "refusal_correctness": 1.0,
                        "over_refusal_rate": 0.0, "cases": 21.0}}
    worse = Report({"classification_accuracy": 0.85, "groundedness": 1.0,
                    "refusal_correctness": 0.8, "over_refusal_rate": 0.1,
                    "injection_correctness": 1.0, "cross_tenant_correctness": 1.0,
                    "injection_followed": 0.0, "cross_tenant_leaks": 0.0,
                    "cost_per_case": 0.01, "cases": 20.0})
    problems = " | ".join(regressions(worse, base))
    for expected in ("classification_accuracy fell", "refusal_correctness fell",
                     "over_refusal_rate rose", "cases fell"):
        assert expected in problems


def test_cost_above_the_band_fails() -> None:
    report = Report({**dict.fromkeys(("classification_accuracy", "groundedness",
                                      "refusal_correctness", "injection_correctness",
                                      "cross_tenant_correctness"), 1.0),
                     "over_refusal_rate": 0.0, "injection_followed": 0.0,
                     "cross_tenant_leaks": 0.0, "cost_per_case": 9.0, "cases": 21.0})
    assert any("cost_per_case" in p for p in regressions(report, {}))


# --- replay -----------------------------------------------------------------------------------


async def test_replay_answers_by_ticket_and_task_and_never_invents(tmp_path: Path) -> None:
    (tmp_path / "c.jsonl").write_text(json.dumps({
        "ticket": "EVAL0001", "task": "classify_ticket",
        "replies": [{"text": {"intent": "other"}}]}) + "\n", encoding="utf-8")
    provider = ReplayProvider.from_directory(tmp_path)
    prompt = [Message("user", '<data id="ab12" index="1" source="servicenow" ref="EVAL0001">\n'
                              '{}\n</data id="ab12">')]
    first = await provider.complete("d", prompt, [], task="classify_ticket", max_tokens=10)
    assert json.loads(first.text) == {"intent": "other"}
    with pytest.raises(ProviderUnavailableError):
        await provider.complete("d", prompt, [], task="classify_ticket", max_tokens=10)


# --- the guardrails being measured ------------------------------------------------------------


@pytest.mark.parametrize(
    ("ticket", "reason"),
    [
        ({"kind": "change_task", "state": "New", "short_description": "x"}, "unrecognised_type"),
        ({"kind": "incident", "state": "Resolved", "short_description": "x"}, "not_open"),
        ({"kind": "incident", "state": "New", "short_description": " "}, "empty_ticket"),
    ],
)
def test_intake_routes_the_unrecognised_to_a_human(ticket: dict[str, str], reason: str) -> None:
    assert triage.intake(ticket) == triage.Decision("human", (reason,))


def test_customer_cross_check_ignores_legal_suffixes_and_case() -> None:
    ticket = {"short_description": "Disk full on vm-alpha-01"}
    out = {"intent": "disk_space", "action_class": "C", "customer": "Alpha Ltd.",
           "affected_ci": "vm-alpha-01"}
    ok = triage.after_classification(ticket, status="answered", output=out,
                                     tenant_names=["alpha", "ALPHA LIMITED"])
    assert ok.route == "continue"
    other = triage.after_classification(ticket, status="answered",
                                        output={**out, "customer": "Bravo plc"},
                                        tenant_names=["alpha"])
    assert other.reasons == ("customer_mismatch",)


def test_a_ci_the_ticket_never_mentions_is_ungrounded() -> None:
    d = triage.after_classification({"short_description": "Disk full"}, status="answered",
                                    output={"action_class": "C", "affected_ci": "vm-other-99"},
                                    tenant_names=["alpha"])
    assert d.reasons == ("ungrounded_ci",)


def test_grounding_requires_every_citation_in_the_evidence() -> None:
    evidence = [json.dumps({"result": {"recent_tickets": [{"number": "INC-alpha"}]}})]
    bundle = {"resource_found": True, "related_tickets": ["INC-alpha"],
              "runbook_candidates": ["bravo-runbook"]}
    assert triage.after_enrichment(status="answered", bundle=bundle, evidence=evidence) == \
        triage.Decision("human", ("ungrounded_context",))
    bundle["runbook_candidates"] = []
    assert triage.after_enrichment(status="answered", bundle=bundle,
                                   evidence=evidence).route == "ready_for_planning"
