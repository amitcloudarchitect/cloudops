"""A pack as lines: what the PDF prints and what the acceptance diff compares.

One rendering, two consumers. ``render_lines`` turns the stored snapshot and
the people's narrative into an ordered list of ``Line``s; ``pdf.render_pdf``
lays them out, and ``acceptance.compare`` diffs their text against the
manually assembled pack. So whatever the PDF says, line for line, is exactly
what was compared, currency presentation included.

Presentation is the only place values are rounded: money by
``money.present`` in its currency, percentages to two places, days to one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from cloudops_domain.reporting.money import present
from cloudops_domain.reporting.sections import Figure, Kind, PackContent

LineKind = Literal["title", "subtitle", "banner", "heading", "subheading", "text", "row"]
DRAFT_BANNER: Final = "DRAFT - not reviewed, not for distribution"
_MONTHS: Final = ("January", "February", "March", "April", "May", "June", "July", "August",
                  "September", "October", "November", "December")


@dataclass(frozen=True)
class Line:
    kind: LineKind
    text: str
    cells: tuple[str, ...] = ()


def value_text(value: str, kind: Kind, unit: str) -> str:
    if kind == "money":
        return present(Decimal(value), unit)
    if kind == "percent":
        return f"{Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
    if kind == "days":
        return f"{Decimal(value).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)} days"
    if kind == "count":
        return f"{int(value):,}"
    return value


def figure_text(f: Figure) -> str:
    shown = value_text(f.value, f.kind, f.unit) if f.value else "not available"
    note = f" ({f.note})" if f.note else ""
    return f"{f.label}: {shown}{note}"


def render_lines(content: PackContent, narratives: Mapping[str, str], *, released: bool,
                 version: int) -> list[Line]:
    """``narratives`` holds a person's text per section; drafts never print."""
    month = f"{_MONTHS[content.period.month - 1]} {content.period.year}"
    lines = [
        Line("title", f"{content.tenant_name} - Monthly service report"),
        Line("subtitle", f"{month} (version {version}). Currency: {content.contract_currency}."
                         f" Data as of {content.data_as_of:%Y-%m-%d %H:%M} UTC."),
    ]
    if not released:
        lines.append(Line("banner", DRAFT_BANNER))
    for section in content.sections:
        lines.append(Line("heading", section.title))
        if not section.captured:
            lines.append(Line("text", f"Not captured: {section.gap}."))
            continue
        story = narratives.get(section.key)
        if story:
            lines.extend(Line("text", p.strip()) for p in story.split("\n\n") if p.strip())
        lines.extend(Line("text", figure_text(f)) for f in section.figures)
        for table in section.tables:
            lines.append(Line("subheading", table.title))
            lines.append(Line("row", " | ".join(table.columns), table.columns))
            for row in table.rows:
                cells = tuple(value_text(c, k, "") if c else "-"
                              for c, k in zip(row, table.kinds, strict=True))
                lines.append(Line("row", " | ".join(cells), cells))
    return lines


def text_lines(lines: Sequence[Line]) -> list[str]:
    return [line.text for line in lines]
