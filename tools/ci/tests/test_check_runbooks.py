"""The runbook gate (LLD, Runbook manifest): the LLD's own example passes, and each
rule the LLD states fails a manifest that breaks it."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_runbooks", REPO_ROOT / "tools/ci/check_runbooks.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

# The LLD's example, as authored (the pipeline adds the signature).
EXAMPLE: dict[str, Any] = {
    "apiVersion": "cloudops/v1",
    "kind": "Runbook",
    "metadata": {"id": "restart-windows-service", "version": 3, "actionClass": "B",
                 "platforms": ["azure", "aws", "resolve"], "resourceTypes": ["compute.vm"]},
    "spec": {
        "parameters": [{"name": "serviceName", "type": "string",
                        "pattern": "^[A-Za-z0-9_.-]{1,64}$", "required": True}],
        "blastRadius": {"maxTargets": 1},
        "preConditions": ["service-exists", "not-in-change-freeze",
                          "resource-tagged-environment"],
        "steps": ["capture-state", "restart"],
        "rollback": ["restore-state"],
        "postConditions": [{"id": "service-running", "timeoutSeconds": 120}],
    },
}


def _broken(path: str, value: Any) -> dict[str, Any]:
    """The example with one field replaced (or deleted, when value is the gate)."""
    doc = copy.deepcopy(EXAMPLE)
    target: Any = doc
    keys: list[Any] = [int(k) if k.isdigit() else k for k in path.split(".")]
    for key in keys[:-1]:
        target = target[key]
    if value is gate:
        del target[keys[-1]]
    else:
        target[keys[-1]] = value
    return doc


def test_the_llds_example_is_valid() -> None:
    assert gate.validate(EXAMPLE) == []


@pytest.mark.parametrize(("path", "value", "message"), [
    ("signature", {"keyId": "key-runbook-signing", "algorithm": "RS256"}, "only the pipeline"),
    ("spec.parameters.0.pattern", "[A-Za-z]+", "anchored pattern"),
    ("spec.parameters.0.type", "shell", "type must be"),
    ("spec.steps", ["restart ${serviceName}"], "step id"),
    ("spec.parameters.0.description", "runs `net stop $(name)`", "interpolation"),
    ("spec.rollback", gate, None),  # class B: rollback optional
    ("spec.postConditions", [], "prove its own success"),
    ("spec.blastRadius", {"maxTargets": 500}, "maxTargets"),
    ("metadata.actionClass", "F", "actionClass"),
    ("metadata.platforms", ["gcp"], "platforms"),
    ("spec.shell", "rm -rf /", "unknown key"),
    ("metadata.version", 0, "version"),
])
def test_each_rule_is_enforced(path: str, value: Any, message: str | None) -> None:
    errors = gate.validate(_broken(path, value))
    if message is None:
        assert errors == []
    else:
        assert any(message in e for e in errors), errors


@pytest.mark.parametrize("action_class", ["C", "D", "E"])
def test_class_c_and_above_need_a_rollback(action_class: str) -> None:
    doc = _broken("metadata.actionClass", action_class)
    del doc["spec"]["rollback"]
    assert any("spec.rollback" in e for e in gate.validate(doc))


def test_an_integer_needs_both_bounds() -> None:
    doc = _broken("spec.parameters.0", {"name": "count", "type": "integer", "minimum": 1,
                                        "required": True})
    assert any("minimum <= maximum" in e for e in gate.validate(doc))


def test_the_repository_passes() -> None:
    assert gate.check(REPO_ROOT) == []
