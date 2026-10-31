"""The acceptance diff: a regenerated month against the manually assembled pack.

    result = compare(text_lines(render_lines(...)), load_reference(path))
    assert result.matches, "\\n".join(result.diff)

Line by line, including currency presentation. The only normalisation is
whitespace (runs of spaces collapse, ends are trimmed, blank lines dropped),
because the manual pack's text is extracted from a document and its spacing
is an artefact of that. Digits, signs, separators, currency codes and symbols
are compared exactly: "EUR 1,234.56" and "€1,234.56" are different lines.

Lines that differ and carry an amount are listed again in
``currency_mismatches``, so a presentation difference is never lost in a long
diff.

**The reference pack is not in the repository yet** (ADR 0020). It is a
manually assembled pack for a recent month, its text extracted one line per
printed line. Compare it with the regenerated month's text
(``GET /v1/reports/packs/{id}/text``, the PDF's own lines):

    python -m cloudops_domain.reporting.acceptance generated.txt manual.txt

It prints the diff and the lines whose amounts differ, and exits 1 on any
difference. ``services/domain/acceptance/README.md`` describes the procedure.
"""

from __future__ import annotations

import difflib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_SPACES: Final = re.compile(r"\s+")
_AMOUNT: Final = re.compile(r"(\b[A-Z]{3}\s?-?[\d.,]+\d|[€£$¥]\s?-?[\d.,]+\d|-?[\d.,]+\d\s?[€£$¥])")


def normalise(line: str) -> str:
    return _SPACES.sub(" ", line).strip()


def load_reference(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


@dataclass(frozen=True)
class Comparison:
    matches: bool
    diff: list[str]
    currency_mismatches: list[tuple[str, str]]  # (reference, generated)


def compare(generated: Sequence[str], reference: Sequence[str]) -> Comparison:
    gen = [n for n in (normalise(x) for x in generated) if n]
    ref = [n for n in (normalise(x) for x in reference) if n]
    diff = list(difflib.unified_diff(ref, gen, "manual", "generated", lineterm="", n=1))
    money: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ref, b=gen, autojunk=False
                                                       ).get_opcodes():
        if tag in ("replace", "delete", "insert"):
            left, right = ref[i1:i2], gen[j1:j2]
            for k in range(max(len(left), len(right))):
                a = left[k] if k < len(left) else ""
                b = right[k] if k < len(right) else ""
                if _AMOUNT.search(a) or _AMOUNT.search(b):
                    money.append((a, b))
    return Comparison(gen == ref, diff, money)


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m cloudops_domain.reporting.acceptance GENERATED MANUAL",
              file=sys.stderr)
        return 2
    result = compare(load_reference(Path(argv[0])), load_reference(Path(argv[1])))
    if result.matches:
        print("matches, line by line")
        return 0
    print("\n".join(result.diff))
    if result.currency_mismatches:
        print("\namounts presented differently (manual | generated):")
        for manual, generated in result.currency_mismatches:
            print(f"  {manual!r} | {generated!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
