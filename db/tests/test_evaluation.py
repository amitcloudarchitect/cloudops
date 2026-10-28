"""The AI evaluation suite: a release gate (LLD, Evaluation; step 08).

    uv run pytest -m evaluation                                     # the gate
    CLOUDOPS_EVAL_UPDATE_BASELINE=1 uv run pytest -m evaluation     # move the baseline

Runs every active golden case (``evals/golden``) through the real stage 1 to 3
workflow on this database, with model responses replayed from
``evals/cassettes``, scores it, and fails on any regression against
``evals/baseline.json``. Injection-followed and cross-tenant leaks fail at any
value above zero, whatever the baseline. The full report is written to
``evals/last-report.json`` (gitignored) and shown on failure.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from cloudops_evals.cases import load_cases
from cloudops_evals.runner import describe, run_cases
from cloudops_evals.scoring import load_baseline, regressions, score, write_baseline

from .conftest import Seeded
from .support import REPO_ROOT

pytestmark = [pytest.mark.integration, pytest.mark.evaluation]

EVALS = REPO_ROOT / "evals"
BASELINE = EVALS / "baseline.json"


async def test_the_golden_set_does_not_regress(seeded: Seeded) -> None:
    cases = load_cases(EVALS / "golden")
    assert len(cases) >= 20
    outcomes, raw = await run_cases(
        cases, app_dsn=seeded.app_dsn,
        tenants={"alpha": seeded.a.id, "bravo": seeded.b.id, "charlie": seeded.c.id},
        cassettes=EVALS / "cassettes",
    )
    report = score(cases, outcomes)
    await asyncio.to_thread(
        Path(EVALS / "last-report.json").write_text, report.to_json(), encoding="utf-8")

    # Guardrail reasons are part of the contract: a case routed to a human for
    # the wrong reason passed by luck.
    wrong_reason = [
        f"{c.id}: expected {c.expected.reason}, got {raw[c.id].reasons}"
        for c in cases if c.expected.reason and c.expected.reason not in raw[c.id].reasons
    ]
    assert wrong_reason == [], describe(raw)

    if os.environ.get("CLOUDOPS_EVAL_UPDATE_BASELINE") == "1":
        zero = [r for r in regressions(report, {}) if "zero tolerance" in r]
        assert zero == [], "a baseline is never written over a zero-tolerance failure"
        write_baseline(BASELINE, report)
    problems = regressions(report, load_baseline(BASELINE))
    assert problems == [], "\n".join(problems) + "\n\n" + report.to_json()
