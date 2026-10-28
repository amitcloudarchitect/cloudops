"""Static checks on migration files: expand-then-contract and the LLD's
migration rules. No database needed."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from .support import REPO_ROOT

MIGRATIONS = REPO_ROOT / "db/migrations"
VERSIONS = sorted((MIGRATIONS / "versions").glob("*.py"))

# Forbidden in an expand migration: anything that removes, renames or
# tightens structure the running release may still depend on.
CONTRACT_ONLY = [
    r"\bDROP\s+(TABLE|COLUMN|SCHEMA|INDEX|VIEW|FUNCTION|TYPE|POLICY|TRIGGER)\b",
    r"\bRENAME\b",
    r"\bALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE\b",
    r"\bSET\s+NOT\s+NULL\b",
]
ADD_NOT_NULL_WITHOUT_DEFAULT = re.compile(
    r"\bADD\s+COLUMN\b(?![^,;]*\bDEFAULT\b)[^,;]*\bNOT\s+NULL\b", re.IGNORECASE
)
INDEX = re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b(?!\s+CONCURRENTLY)", re.IGNORECASE)


def _module_constant(path: Path, name: str) -> object:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    return None


EXPANDS = [p for p in VERSIONS if _module_constant(p, "CHANGE_KIND") == "expand"]


def test_there_are_migrations() -> None:
    assert VERSIONS


@pytest.mark.parametrize("path", VERSIONS, ids=lambda p: p.name)
def test_declares_its_change_kind(path: Path) -> None:
    assert _module_constant(path, "CHANGE_KIND") in {"expand", "contract"}


def test_the_first_migration_is_an_expand() -> None:
    assert VERSIONS[0] in EXPANDS


@pytest.mark.parametrize("path", EXPANDS, ids=lambda p: p.name)
def test_expand_migrations_only_add(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for pattern in CONTRACT_ONLY:
        assert not re.search(pattern, source, re.IGNORECASE), pattern
    assert not ADD_NOT_NULL_WITHOUT_DEFAULT.search(source)


@pytest.mark.parametrize("path", VERSIONS, ids=lambda p: p.name)
def test_indexes_are_built_concurrently(path: Path) -> None:
    """Only partitioned parents are exempt (PostgreSQL cannot build those
    concurrently), and each such line must say so."""
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if INDEX.search(line):
            assert "-- partitioned parent" in line, f"{path.name}:{lineno} not CONCURRENTLY"


@pytest.mark.parametrize("path", VERSIONS, ids=lambda p: p.name)
def test_downgrade_is_not_offered(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    downgrade = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
    )
    assert isinstance(downgrade.body[-1], ast.Raise)


def test_migrations_run_with_a_three_second_lock_timeout() -> None:
    assert _module_constant(MIGRATIONS / "env.py", "LOCK_TIMEOUT") == "3s"
