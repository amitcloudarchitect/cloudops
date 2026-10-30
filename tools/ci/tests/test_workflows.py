"""The pipelines themselves (ADR 0013): pinned, least-privilege, and shaped as the
LLD says, so a careless edit to a workflow fails like any other change."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = {p.stem: p for p in sorted((REPO_ROOT / ".github/workflows").glob("*.yml"))}


def _load(name: str) -> dict[str, Any]:
    raw: dict[Any, Any] = yaml.safe_load(WORKFLOWS[name].read_text(encoding="utf-8"))
    # PyYAML reads the key `on` as True.
    raw["on"] = raw.pop(True, raw.get("on"))
    return {str(k): v for k, v in raw.items()}


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for job in doc["jobs"].values() for s in job.get("steps", [])]


@pytest.mark.parametrize("name", list(WORKFLOWS))
def test_every_action_is_pinned_to_a_commit(name: str) -> None:
    for step in _steps(_load(name)):
        uses = step.get("uses")
        if uses and not uses.startswith("./"):
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses), (name, uses)


@pytest.mark.parametrize("name", list(WORKFLOWS))
def test_the_default_token_is_read_only(name: str) -> None:
    doc = _load(name)
    assert doc["permissions"] == {"contents": "read"}, name
    assert "pull_request_target" not in doc["on"], name  # runs fork code with secrets


def test_ci_has_every_lld_stage() -> None:
    jobs = set(_load("ci")["jobs"])
    assert jobs >= {"boundaries", "secret-scan", "lint-and-types", "tests", "evaluation",
                    "pricing-regression", "sast", "sca", "images", "terraform-static",
                    "terraform-plan"}


def test_ci_never_writes_terraform_state() -> None:
    text = WORKFLOWS["ci"].read_text(encoding="utf-8")
    assert "-lock=false" in text
    assert "terraform apply" not in text


def test_main_runs_the_gates_before_releasing() -> None:
    jobs = _load("main")["jobs"]
    assert jobs["gates"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["release"]["needs"] == "gates"
    assert jobs["bump-dev"]["needs"] == "release"
    ignored = _load("main")["on"]["push"]["paths-ignore"]
    assert all(p.startswith("infra/gitops/overlays/dev/") for p in ignored)


def test_promotion_is_gated_and_production_needs_a_change_record() -> None:
    doc = _load("promote")
    assert doc["jobs"]["promote"]["environment"] == "${{ inputs.environment }}"
    assert doc["jobs"]["promote"]["needs"] == "verify"
    checks = doc["jobs"]["verify"]["steps"][0]["run"]
    assert "CHG[0-9]{7,}" in checks
    assert "refs/heads/main" in checks


def test_runbooks_are_signed_only_on_main() -> None:
    job = _load("runbooks")["jobs"]["sign-and-publish"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "github.event_name == 'push'" in job["if"]


def test_codeowners_cover_the_dangerous_paths() -> None:
    owners = (REPO_ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
    for path in ("/packages/tenancy/", "/execution/", "/infra/", "/runbooks/", "/.github/"):
        assert re.search(rf"^{re.escape(path)}\s+@\S+", owners, re.MULTILINE), path
