"""The AI evaluation suite: a release gate (LLD, Evaluation; step 08).

    uv run pytest -m evaluation                                     # the gate
    CLOUDOPS_EVAL_UPDATE_BASELINE=1 uv run pytest -m evaluation     # move the baseline

Runs every active golden case (``evals/golden``) through the real stage 1 to 3
workflow on this database, with model responses replayed from
``evals/cassettes``, scores it, and fails on any regression against
``evals/baseline.json``. Injection-followed and cross-tenant leaks fail at any
value above zero, whatever the baseline. The full report is written to
``evals/last-report.json`` (gitignored) and shown on failure.

Besides the three test tenants, the simulation's customer (``azsponsor``,
``docs/simulation``) is added here with the invented example estate as inventory,
so its open incidents (``evals/golden/simulation.jsonl``) run against the
resources they name.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from cloudops_devdata.simulation.model import COMPANY, SHORT_CODE, build, example_estate
from cloudops_devdata.simulation.rows import platform_rows
from cloudops_devdata.simulation.triage import KNOWLEDGE
from cloudops_evals.cases import load_cases
from cloudops_evals.runner import OWN_KNOWLEDGE, describe, run_cases
from cloudops_evals.scoring import load_baseline, regressions, score, write_baseline

from .conftest import Seeded
from .support import REPO_ROOT

pytestmark = [pytest.mark.integration, pytest.mark.evaluation]

EVALS = REPO_ROOT / "evals"
BASELINE = EVALS / "baseline.json"


def _simulation_tenant(admin_dsn: str) -> UUID:
    """The simulation's customer with the example estate's VMs, as superuser (as the session
    seed is). Its id is made here, never fixed: nothing may depend on it."""
    sim = build(datetime.now(UTC).date(), 11, example_estate())
    with psycopg.connect(admin_dsn, autocommit=True) as cx:
        found = cx.execute("SELECT id FROM cloudops.tenant WHERE short_code = %s",
                           (SHORT_CODE,)).fetchone()
        if found:
            return UUID(str(found[0]))
        tenant = cx.execute(
            "INSERT INTO cloudops.tenant (canonical_name, short_code) VALUES (%s, %s)"
            " RETURNING id", (COMPANY, SHORT_CODE)).fetchone()
        assert tenant is not None
        data = platform_rows(sim, UUID(str(tenant[0])))
        for table, rows in (("cloudops.cloud_account", data.accounts),
                            ("cloudops.resource", data.resources)):
            cols = [f.name for f in fields(rows[0]) if f.name != "billing_currency"]
            casts = {"ip_addresses": "::inet[]", "tags": "::jsonb"}
            for row in rows:
                values = {k: v for k, v in asdict(row).items() if k in cols}
                if "tags" in values:
                    values["tags"] = json.dumps(values["tags"])
                cx.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES"
                    f" ({', '.join(f'%({c})s{casts.get(c, "")}' for c in cols)})", values)
        return data.tenant_id


async def test_the_golden_set_does_not_regress(seeded: Seeded) -> None:
    cases = load_cases(EVALS / "golden")
    assert len(cases) >= 20
    tenant_of = {"alpha": seeded.a.id, "bravo": seeded.b.id, "charlie": seeded.c.id,
                 "azsponsor": _simulation_tenant(seeded.admin_dsn)}
    outcomes, raw = await run_cases(
        cases, app_dsn=seeded.app_dsn, tenants=tenant_of, cassettes=EVALS / "cassettes",
        knowledge={**OWN_KNOWLEDGE, "azsponsor": KNOWLEDGE},
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

    # Every run is recorded for the console (migration 0008, ADR 0014): in the
    # ticket's own tenant, with the route and stage the workflow returned.
    with psycopg.connect(seeded.admin_dsn) as cx:
        stored = {str(r[0]): r[1:] for r in cx.execute(
            "SELECT id, tenant_id, number, route, stage FROM cloudops.triage_result")}
    for c in cases:
        result = raw[c.id]
        if result.route == "error":
            continue
        assert result.result_id in stored, f"{c.id}: no triage_result recorded"
        assert stored[result.result_id] == (
            tenant_of[c.tenant], c.ticket.number, result.route, result.stage), c.id

    if os.environ.get("CLOUDOPS_EVAL_UPDATE_BASELINE") == "1":
        zero = [r for r in regressions(report, {}) if "zero tolerance" in r]
        assert zero == [], "a baseline is never written over a zero-tolerance failure"
        write_baseline(BASELINE, report)
    problems = regressions(report, load_baseline(BASELINE))
    assert problems == [], "\n".join(problems) + "\n\n" + report.to_json()
