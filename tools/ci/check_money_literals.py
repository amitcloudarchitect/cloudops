"""CI gate: no monetary literal outside the rate card (LLD, CI/CD; CLAUDE.md rule 5).

A price, rate, cost or amount written into product code is a number nobody can
trace or change in one place. Fails (exit 1) when product source (``src/`` under
``packages/``, ``services/`` and ``connectors/``) contains a ``Decimal`` literal
that could be money:

- ``Decimal("12.50")``, ``Decimal(5)``, ``decimal.Decimal(...)`` with a numeric
  string or number;
- except 0, 1, and powers of ten (``Decimal("0.000001")``, ``Decimal(1000)``):
  those are precision and unit constants, never prices.

Money in Python is ``Decimal`` (rule 5), so a float price is already a defect the
type checker and review catch; this gate covers what they cannot see.

Two ways out, both reviewed like any change to this gate:
- a file in ``money_literal_allowlist.txt`` with a justification: a rate card;
- a line marked ``# not money: <reason>`` (a ratio, a duration, a count).

Usage: python tools/ci/check_money_literals.py [repo_root]
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOTS = ("packages", "services", "connectors")
WAIVER = "# not money:"
ALLOWLIST = Path(__file__).with_name("money_literal_allowlist.txt")


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    literal: str

    def __str__(self) -> str:
        return (f"{self.path}:{self.line}: monetary literal Decimal({self.literal}) outside the "
                "rate card; move it there, or mark the line '# not money: <reason>'")


def _is_decimal_call(node: ast.Call) -> bool:
    f = node.func
    return (isinstance(f, ast.Name) and f.id == "Decimal") or (
        isinstance(f, ast.Attribute) and f.attr == "Decimal")


def _is_unit(value: Decimal) -> bool:
    """0, 1 and powers of ten are precision or unit constants."""
    if value == 0:
        return True
    magnitude = abs(value)
    return magnitude.normalize().as_tuple().digits == (1,)


def _literal(node: ast.Call) -> str | None:
    """The literal's text if this call builds a constant number, else None."""
    if len(node.args) != 1 or node.keywords:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
        arg = arg.operand
    if not isinstance(arg, ast.Constant) or isinstance(arg.value, bool):
        return None
    raw = arg.value
    if not isinstance(raw, (str, int, float)):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not value.is_finite() or _is_unit(value):
        return None
    return repr(raw)


def scan(path: Path, rel: str) -> Iterator[Finding]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source, filename=rel)):
        if isinstance(node, ast.Call) and _is_decimal_call(node):
            literal = _literal(node)
            if literal is None:
                continue
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            if any(WAIVER in lines[i - 1] for i in range(node.lineno, end + 1)):
                continue
            yield Finding(rel, node.lineno, literal)


def allowlist(path: Path = ALLOWLIST) -> tuple[frozenset[str], list[str]]:
    """Entries and errors (an entry without a justification is an error)."""
    entries: set[str] = set()
    errors: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        entry, _, why = text.partition("#")
        if not why.strip():
            errors.append(f"{path.name}:{n}: '{entry.strip()}' has no justification")
            continue
        entries.add(entry.strip())
    return frozenset(entries), errors


def _allowed(rel: str, entries: frozenset[str]) -> bool:
    return any(rel == e or (e.endswith("/") and rel.startswith(e)) for e in entries)


def check(root: Path) -> list[str]:
    entries, problems = allowlist()
    for top in ROOTS:
        for path in sorted((root / top).glob("**/src/**/*.py")):
            rel = path.relative_to(root).as_posix()
            if any(part in {".venv", "__pycache__"} for part in path.parts):
                continue
            if _allowed(rel, entries):
                continue
            problems.extend(str(f) for f in scan(path, rel))
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    problems = check(root)
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(f"money literals: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("money literals: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
