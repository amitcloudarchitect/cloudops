"""The SAST gate (ADR 0013): high and critical fail, a reviewed file-and-rule
exception passes, and nothing broader than that does."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_sarif", REPO_ROOT / "tools/ci/check_sarif.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _sarif(rule: str, severity: str, uri: str) -> dict[str, Any]:
    return {"runs": [{
        "tool": {"driver": {"rules": [
            {"id": rule, "properties": {"security-severity": severity}}]}},
        "results": [{"ruleId": rule, "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": uri}, "region": {"startLine": 3}}}]}],
    }]}


@pytest.mark.parametrize(("severity", "fails"), [("9.8", True), ("7.0", True), ("6.9", False)])
def test_high_and_critical_fail(severity: str, fails: bool) -> None:
    found = gate.findings(_sarif("py/sql-injection", severity, "a.py"))
    assert bool(found) is fails


def test_the_excepted_file_and_rule_pass_and_nothing_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    excepted = ("packages/config/src/cloudops_config/__main__.py",
                "py/clear-text-logging-sensitive-data")
    (tmp_path / "a.sarif").write_text(json.dumps(_sarif(excepted[1], "7.5", excepted[0])))
    assert gate.main(["x", str(tmp_path)]) == 0
    (tmp_path / "b.sarif").write_text(json.dumps(_sarif(excepted[1], "7.5", "other.py")))
    assert gate.main(["x", str(tmp_path)]) == 1
    (tmp_path / "b.sarif").write_text(json.dumps(_sarif("py/sql-injection", "8.8", excepted[0])))
    assert gate.main(["x", str(tmp_path)]) == 1
    capsys.readouterr()


def test_an_exception_needs_a_reason(tmp_path: Path) -> None:
    listing = tmp_path / "e.txt"
    listing.write_text("a.py:py/x  # why\nb.py:py/y\n", encoding="utf-8")
    entries, errors = gate.exceptions(listing)
    assert entries == {("a.py", "py/x")}
    assert len(errors) == 1


def test_the_repository_exceptions_are_justified() -> None:
    _, errors = gate.exceptions()
    assert errors == []
