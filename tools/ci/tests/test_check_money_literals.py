"""The rate-card gate (LLD, CI/CD): it must catch prices in product code and stay
quiet on units, precision constants and justified exceptions."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_money_literals", REPO_ROOT / "tools/ci/check_money_literals.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _findings(tmp_path: Path, code: str) -> list[Any]:
    path = tmp_path / "m.py"
    path.write_text(code, encoding="utf-8")
    return list(gate.scan(path, "m.py"))


@pytest.mark.parametrize("code", [
    'PRICE = Decimal("12.50")',
    "FEE = Decimal(25)",
    'RATE = decimal.Decimal("0.0825")',
    'REFUND = Decimal("-3.10")',
    'x = Decimal(\n    "99.99",\n)',
])
def test_a_price_is_caught(tmp_path: Path, code: str) -> None:
    assert len(_findings(tmp_path, code)) == 1


@pytest.mark.parametrize("code", [
    "ZERO = Decimal(0)",
    'ONE = Decimal("1")',
    'MONEY = Decimal("0.000001")',
    "PER_THOUSAND = Decimal(1000)",
    "x = Decimal(amount)",
    'x = Decimal(str(value))',
    'RATIO = Decimal(5)  # not money: a ratio of spend rates',
])
def test_units_variables_and_waived_lines_pass(tmp_path: Path, code: str) -> None:
    assert _findings(tmp_path, code) == []


def test_an_allowlist_entry_needs_a_reason(tmp_path: Path) -> None:
    listing = tmp_path / "allow.txt"
    listing.write_text("a/rate_card.py  # the rate card\nb/other.py\n", encoding="utf-8")
    entries, errors = gate.allowlist(listing)
    assert entries == {"a/rate_card.py"}
    assert errors == ["allow.txt:2: 'b/other.py' has no justification"]


def test_a_directory_entry_covers_its_files() -> None:
    entries = frozenset({"packages/devdata/"})
    assert gate._allowed("packages/devdata/src/x.py", entries)
    assert not gate._allowed("packages/devdata2/src/x.py", entries)


def test_the_repository_passes() -> None:
    assert gate.check(REPO_ROOT) == []
