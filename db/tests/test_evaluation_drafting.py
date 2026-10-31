"""The drafting agents' evaluation gate (step 17; LLD, Evaluation).

    uv run pytest -m evaluation                                     # the gate
    CLOUDOPS_EVAL_UPDATE_BASELINE=1 uv run pytest -m evaluation     # move the baseline

Every active case in ``evals/drafting/golden.jsonl`` runs through the drafting
activity production runs, with model replies from ``evals/drafting/cassettes.jsonl``.
Injection followed, cross-tenant leaks and results set without a person fail at
any value above zero; the rest fail on any regression against
``evals/drafting/baseline.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from cloudops_evals.drafting import (
    drafting_regressions,
    load_drafting_cases,
    run_drafting_cases,
    score_drafting,
)

from .conftest import Step17
from .support import REPO_ROOT

pytestmark = [pytest.mark.integration, pytest.mark.evaluation]

SUITE = REPO_ROOT / "evals" / "drafting"
BASELINE = SUITE / "baseline.json"


async def test_the_drafting_golden_set_does_not_regress(step17: Step17) -> None:
    cases = load_drafting_cases(SUITE / "golden.jsonl")
    assert len(cases) >= 9
    s = step17.seeded
    outcomes = await run_drafting_cases(
        cases, app_dsn=s.app_dsn, tenants={t.name: t.id for t in s.tenants},
        cassettes=SUITE / "cassettes.jsonl", library_actor=step17.admin)
    report = score_drafting(cases, outcomes)
    await asyncio.to_thread(Path(SUITE / "last-report.json").write_text, report.to_json(),
                            encoding="utf-8")
    wrong = [f"{c.id}: expected {c.expected.state}, got {outcomes[c.id].state}"
             f" ({outcomes[c.id].detail})" for c in cases
             if outcomes[c.id].state != c.expected.state]
    assert wrong == [], report.to_json()
    if os.environ.get("CLOUDOPS_EVAL_UPDATE_BASELINE") == "1":
        assert drafting_regressions(report, {}) == [], "never baseline a zero-tolerance failure"
        BASELINE.write_text(json.dumps({"metrics": report.metrics}, indent=2, sort_keys=True)
                            + "\n", encoding="utf-8")
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))["metrics"]
    problems = drafting_regressions(report, baseline)
    assert problems == [], "\n".join(problems) + "\n\n" + report.to_json()
