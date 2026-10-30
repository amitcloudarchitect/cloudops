"""CI gate: fail on high or critical SAST findings (LLD, CI/CD; ADR 0013).

Reads CodeQL's SARIF files and fails (exit 1) on any result whose rule has a
security severity of 7.0 or more (GitHub's high and critical), unless the file and
rule are listed in ``sast_exceptions.txt`` with a justification. Exceptions name a
file and a rule, never a line, so they survive edits and cover nothing else; the
finding still shows in the repository's code scanning view.

Usage: python tools/ci/check_sarif.py SARIF_DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HIGH = 7.0
EXCEPTIONS = Path(__file__).with_name("sast_exceptions.txt")


def exceptions(path: Path = EXCEPTIONS) -> tuple[frozenset[tuple[str, str]], list[str]]:
    """(file, rule) pairs, and errors for entries without a justification."""
    entries: set[tuple[str, str]] = set()
    errors: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        entry, _, why = text.partition("#")
        file, _, rule = entry.strip().rpartition(":")
        if not why.strip() or not file or not rule:
            errors.append(f"{path.name}:{n}: needs 'file:rule  # reason'")
            continue
        entries.add((file, rule))
    return frozenset(entries), errors


def findings(sarif: dict[str, Any]) -> list[tuple[str, int, str, float]]:
    out: list[tuple[str, int, str, float]] = []
    for run in sarif.get("runs", []):
        tool = run["tool"]
        rules = {r["id"]: r for r in tool["driver"].get("rules", [])}
        for ext in tool.get("extensions", []):
            rules.update({r["id"]: r for r in ext.get("rules", [])})
        for res in run.get("results", []):
            props = rules.get(res.get("ruleId"), {}).get("properties", {})
            severity = float(props.get("security-severity", 0) or 0)
            if severity < HIGH:
                continue
            loc = res["locations"][0]["physicalLocation"]
            out.append((loc["artifactLocation"]["uri"], loc.get("region", {}).get("startLine", 0),
                        res["ruleId"], severity))
    return out


def main(argv: list[str]) -> int:
    allowed, problems = exceptions()
    for path in sorted(Path(argv[1] if len(argv) > 1 else "sarif").glob("*.sarif")):
        for file, line, rule, severity in findings(json.loads(path.read_text(encoding="utf-8"))):
            if (file, rule) in allowed:
                print(f"excepted: {file}:{line}: {rule} ({severity})")
                continue
            problems.append(f"{file}:{line}: {rule} ({severity})")
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(f"SAST: {len(problems)} high or critical finding(s)", file=sys.stderr)
        return 1
    print("SAST: no high or critical findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
