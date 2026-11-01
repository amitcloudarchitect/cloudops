"""The assistant's platform help: user-facing pages, loaded as Getronics-wide knowledge.

    pages = load_pages()
    changed = sync(connection)  # as the schema owner, after migrations (db/migrations/env.py)

**What it is.** ``help/*.md`` in this package, written for end users: how each
screen works, what each metric and health band means, roles, onboarding. Never
ADRs, designs, infrastructure, runbook internals or security material: the
repository is public and these pages are shown to customers (ADR 0029).

**Format.** ``# Title``, then ``audience: all`` or ``audience: staff``, then
``## Section`` blocks. Each section is one chunk, retrieved on its own.

**Where it goes.** ``cloudops.global_knowledge_chunk``, kind ``platform_help``
(every scope, customers included) or ``platform_help_staff`` (staff only: the
table's ``audience`` policy keeps it from a customer scope). Only the schema owner
writes that table (ADR 0001), so the migrate job syncs it: a changed section's
embedding is cleared, a removed section is deleted. The AI worker's indexer then
embeds what is empty, through ``cloudops.fill_global_embedding``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import Final

from sqlalchemy import Connection, text

KINDS: Final = {"all": "platform_help", "staff": "platform_help_staff"}
HELP_KINDS: Final = tuple(KINDS.values())
MAX_SECTION_CHARS: Final = 4000


class HelpFormatError(ValueError):
    pass


@dataclass(frozen=True)
class HelpSection:
    page: str  # file stem, e.g. "work-queue-and-tickets"
    kind: str
    ref: str  # "<page>#<section-slug>", stable across edits of the text
    title: str  # "<page title>: <section title>"
    body: str


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def parse_page(stem: str, source: str) -> list[HelpSection]:
    lines = source.strip().splitlines()
    if len(lines) < 2 or not lines[0].startswith("# "):
        raise HelpFormatError(f"{stem}: first line must be '# Title'")
    audience = re.fullmatch(r"audience:\s*(all|staff)", lines[1].strip())
    if audience is None:
        raise HelpFormatError(f"{stem}: second line must be 'audience: all|staff'")
    page_title = lines[0][2:].strip()
    kind = KINDS[audience.group(1)]
    sections: list[HelpSection] = []
    current: str | None = None
    body: list[str] = []

    def close() -> None:
        if current is None:
            return
        text_body = "\n".join(body).strip()
        if not text_body:
            raise HelpFormatError(f"{stem}: section '{current}' is empty")
        if len(text_body) > MAX_SECTION_CHARS:
            raise HelpFormatError(f"{stem}: section '{current}' is too long for one chunk")
        sections.append(HelpSection(stem, kind, f"{stem}#{_slug(current)}",
                                    f"{page_title}: {current}", text_body))

    for line in lines[2:]:
        if line.startswith("## "):
            close()
            current, body = line[3:].strip(), []
        elif current is None:
            if line.strip():
                raise HelpFormatError(f"{stem}: text before the first section")
        else:
            body.append(line)
    close()
    refs = [s.ref for s in sections]
    if len(set(refs)) != len(refs):
        raise HelpFormatError(f"{stem}: two sections share a title")
    if not sections:
        raise HelpFormatError(f"{stem}: no sections")
    return sections


def load_pages() -> list[HelpSection]:
    folder = resources.files("cloudops_agents").joinpath("help")
    sections: list[HelpSection] = []
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".md"):
            sections += parse_page(entry.name.removesuffix(".md"), entry.read_text("utf-8"))
    return sections


_UPSERT: Final = text(
    "INSERT INTO cloudops.global_knowledge_chunk (source_kind, source_ref, title, body)"
    " VALUES (:kind, :ref, :title, :body)"
    " ON CONFLICT (source_kind, source_ref)"
    "   WHERE source_kind IN ('platform_help', 'platform_help_staff')"
    " DO UPDATE SET title = EXCLUDED.title, body = EXCLUDED.body, embedding = NULL,"
    "   embedded_at = NULL, updated_at = now()"
    " WHERE global_knowledge_chunk.body IS DISTINCT FROM EXCLUDED.body"
    "    OR global_knowledge_chunk.title IS DISTINCT FROM EXCLUDED.title"
)
_REMOVE: Final = text(
    "DELETE FROM cloudops.global_knowledge_chunk"
    " WHERE source_kind = ANY(CAST(:kinds AS text[]))"
    "   AND NOT ((source_kind || ' ' || source_ref) = ANY(CAST(:keep AS text[])))"
)


def sync(connection: Connection, sections: list[HelpSection] | None = None) -> int:
    """Make the help chunks match the pages. Runs as the schema owner; returns
    how many rows were added, changed or removed. The caller commits."""
    sections = load_pages() if sections is None else sections
    changed = 0
    for s in sections:
        changed += connection.execute(_UPSERT, {
            "kind": s.kind, "ref": s.ref, "title": s.title, "body": s.body}).rowcount
    changed += connection.execute(_REMOVE, {
        "kinds": list(HELP_KINDS), "keep": [f"{s.kind} {s.ref}" for s in sections]}).rowcount
    return changed
