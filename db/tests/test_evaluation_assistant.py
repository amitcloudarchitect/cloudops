"""The assistant's evaluation gate (ADR 0029; LLD, Evaluation).

    uv run pytest -m evaluation                                     # the gate
    CLOUDOPS_EVAL_UPDATE_BASELINE=1 uv run pytest -m evaluation     # move the baseline

Every active case in ``evals/assistant/golden.jsonl`` runs through the assistant as
the API runs it, with the case's own model replies. Uncited claims, cross-tenant
leaks and followed injections fail at any value above zero; the rest fail on any
regression against ``evals/assistant/baseline.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from cloudops_evals.assistant import (
    Planter,
    Ticket,
    assistant_regressions,
    load_assistant_cases,
    run_assistant_cases,
    score_assistant,
)

from .conftest import Seeded
from .support import REPO_ROOT

pytestmark = [pytest.mark.integration, pytest.mark.evaluation]

SUITE = REPO_ROOT / "evals" / "assistant"
BASELINE = SUITE / "baseline.json"


def _planter(admin_dsn: str) -> Planter:
    def plant(tickets: Sequence[Ticket], tenants: Mapping[str, UUID], remove: bool) -> None:
        with psycopg.connect(admin_dsn, autocommit=True) as cx:
            for t in tickets:
                if remove:
                    cx.execute("DELETE FROM cloudops.work_item WHERE number = %s", (t.number,))
                else:
                    cx.execute(
                        "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw,"
                        " opened_at, short_description, close_notes, state, priority)"
                        " VALUES (%s, 'incident', %s, 'EVAL', now(), %s, %s, 'In Progress',"
                        " '3')", (tenants[t.tenant], t.number, t.summary, t.close_notes))
    return plant


async def test_the_assistant_golden_set_does_not_regress(seeded: Seeded) -> None:
    cases = load_assistant_cases(SUITE / "golden.jsonl")
    assert len(cases) >= 19
    internal = uuid4()
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code, internal,"
                   " hosts_platform) VALUES (%s, 'Getronics platform (eval)', 'GTXEVAL',"
                   " true, true)", (internal,))
    try:
        outcomes = await run_assistant_cases(
            cases, app_dsn=seeded.app_dsn, plant=_planter(seeded.admin_dsn),
            tenants={t.name: t.id for t in seeded.tenants}, internal=internal)
    finally:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("DELETE FROM cloudops.tenant WHERE id = %s", (internal,))
    report = score_assistant(cases, outcomes)
    await asyncio.to_thread(Path(SUITE / "last-report.json").write_text, report.to_json(),
                            encoding="utf-8")
    wrong = [f"{c.id}: expected {c.expected.status}/{c.expected.reason},"
             f" got {outcomes[c.id].status}/{outcomes[c.id].reason}" for c in cases
             if outcomes[c.id].status != c.expected.status
             or (c.expected.reason and outcomes[c.id].reason != c.expected.reason)]
    assert wrong == [], report.to_json()
    if os.environ.get("CLOUDOPS_EVAL_UPDATE_BASELINE") == "1":
        assert assistant_regressions(report, {}) == [], "never baseline a zero-tolerance failure"
        BASELINE.write_text(json.dumps({"metrics": report.metrics}, indent=2, sort_keys=True)
                            + "\n", encoding="utf-8")
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))["metrics"]
    problems = assistant_regressions(report, baseline)
    assert problems == [], "\n".join(problems) + "\n\n" + report.to_json()
