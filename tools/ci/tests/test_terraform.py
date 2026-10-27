"""Tests for the Terraform gates: the plan tag check, and stack consistency."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STACKS = sorted(p for p in (REPO_ROOT / "infra/terraform/stacks").iterdir() if p.is_dir())


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / f"tools/ci/{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tags_check = _load("check_terraform_tags")

GOOD = {
    "owner": "o@getronics.com",
    "environment": "dev",
    "costcentre": "CC-1",
    "application": "cloudops",
    "dataclass": "c2",
    "managedby": "terraform",
}


def _plan(*changes: tuple[str, list[str], dict[str, Any] | None]) -> dict[str, Any]:
    return {
        "errored": False,
        "resource_changes": [
            {"address": addr, "change": {"actions": actions, "after": after}}
            for addr, actions, after in changes
        ],
    }


def test_fully_tagged_plan_passes() -> None:
    plan = _plan(("azurerm_resource_group.net", ["create"], {"name": "rg", "tags": GOOD}))
    assert tags_check.violations(plan) == []


@pytest.mark.parametrize("drop", list(GOOD))
def test_each_missing_tag_fails(drop: str) -> None:
    tags = {k: v for k, v in GOOD.items() if k != drop}
    plan = _plan(("azurerm_virtual_network.this", ["create"], {"tags": tags}))
    assert tags_check.violations(plan)


@pytest.mark.parametrize(
    "tags", [None, {}, {**GOOD, "owner": "  "}, {**GOOD, "managedby": "portal"}]
)
def test_null_empty_or_wrong_tags_fail(tags: dict[str, str] | None) -> None:
    plan = _plan(("azurerm_key_vault.this", ["update"], {"tags": tags}))
    assert tags_check.violations(plan)


def test_untaggable_deleted_and_unchanged_resources_are_ignored() -> None:
    plan = _plan(
        ("azurerm_subnet_nat_gateway_association.this", ["create"], {"subnet_id": "x"}),
        ("azurerm_resource_group.old", ["delete"], None),
        ("azurerm_resource_group.same", ["no-op"], {"tags": {}}),
    )
    assert tags_check.violations(plan) == []


@pytest.mark.parametrize(
    "plan",
    [
        {"format_version": "1.2", "errored": True},
        {"format_version": "1.2", "errored": False},
        {"format_version": "1.2", "resource_changes": []},
    ],
)
def test_errored_or_incomplete_plans_fail(plan: dict[str, Any]) -> None:
    assert tags_check.violations(plan)


@pytest.mark.parametrize(
    ("rule", "bad"),
    [
        ({"name": "r", "destination_port_ranges": ["*"]}, True),
        ({"name": "r", "source_port_ranges": ["443", "*"]}, True),
        ({"name": "r", "destination_port_range": "*", "destination_port_ranges": None}, False),
        ({"name": "r", "destination_port_ranges": ["443", "5671"]}, False),
    ],
)
def test_wildcard_inside_port_list_fails(rule: dict[str, Any], bad: bool) -> None:
    plan = _plan(
        ("azurerm_network_security_group.x", ["create"], {"tags": GOOD, "security_rule": [rule]})
    )
    assert bool(tags_check.violations(plan)) is bad


@pytest.mark.parametrize("content", ["", "not json"])
def test_unreadable_plan_fails_cleanly(tmp_path: Path, content: str) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(content, encoding="utf-8")
    assert tags_check.main(["check", str(plan)]) == 1


def test_common_variables_are_identical_in_every_stack() -> None:
    reference = (STACKS[0] / "variables_common.tf").read_text(encoding="utf-8")
    for stack in STACKS:
        assert (stack / "variables_common.tf").read_text(encoding="utf-8") == reference, stack.name


@pytest.mark.parametrize("stack", STACKS, ids=lambda p: p.name)
def test_backend_uses_entra_auth_and_holds_no_key(stack: Path) -> None:
    versions = (stack / "versions.tf").read_text(encoding="utf-8")
    assert re.search(rf'key\s*=\s*"{re.escape(stack.name)}\.tfstate"', versions)
    backend = (REPO_ROOT / "infra/terraform/env/dev/backend.hcl").read_text(encoding="utf-8")
    assert re.search(r"use_azuread_auth\s*=\s*true", backend)
    assert not re.search(r"access_key|sas_token|client_secret", backend + versions)


@pytest.mark.parametrize("stack", STACKS, ids=lambda p: p.name)
def test_no_hardcoded_subscription(stack: Path) -> None:
    guid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    for tf in stack.glob("*.tf"):
        text = re.sub(r'regex\("[^"]*"', "", tf.read_text(encoding="utf-8"))
        assert not guid.search(text), tf


def test_role_assignments_live_only_in_the_rbac_stack() -> None:
    for tf in (REPO_ROOT / "infra/terraform").rglob("*.tf"):
        if "azurerm_role_assignment" in tf.read_text(encoding="utf-8"):
            assert tf.parent.name == "04b-rbac", tf
