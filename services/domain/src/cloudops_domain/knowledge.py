"""Knowledge and document intelligence (HLD M20; ADR 0019).

**Document kinds** come from a document's name and folder, never from what it
says: document text is data, never instruction (CLAUDE.md rule 4), and a
document that calls itself a handover pack does not make itself one.

**Staleness: superseded by the live estate.** A design, handover, runbook or
policy document is stale when any of these holds:

- it names a resource that has been removed from the estate since the
  document was last modified (``removed_resources``);
- the estate has grown by at least ``ESTATE_DRIFT_FRACTION`` since then
  (``estate_changed``). Resources found by the first discovery are the
  baseline, not growth: they existed before the platform saw them;
- it has not been modified for ``STALE_AFTER_DAYS`` (``not_updated``).

A resource is "named" when its name (at least ``MIN_NAME_CHARS`` long)
appears in one of the document's chunks. The match runs in SQL, under the
caller's scope, so only the tenant's own resources can ever match.

**Gap analysis:** for every customer in scope, whether it has a current (not
stale) design document and handover pack. Missing and stale are both gaps.

**Citations** name the source document and page of a chunk, with its link.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import text

from cloudops_tenancy import ScopedConnection

DocKind = Literal["design", "handover", "policy", "runbook", "report", "other"]
Coverage = Literal["current", "stale", "missing"]

STALE_AFTER_DAYS: Final = 365
ESTATE_DRIFT_FRACTION: Final = Decimal("0.20")  # not money: a share of the estate
MIN_NAME_CHARS: Final = 4
ASSESSED_KINDS: Final = ("design", "handover", "runbook", "policy")
REQUIRED_KINDS: Final = ("design", "handover")

# First match wins; handover before design, since a handover pack often
# contains the word "design" too.
_KIND_PATTERNS: Final = (
    ("handover", re.compile(r"hand[\s_-]?over|transition[\s_-]?pack", re.I)),
    ("design", re.compile(
        r"\b(hld|lld)\b|design|architecture|solution[\s_-]?overview", re.I)),
    ("runbook", re.compile(r"run[\s_-]?book|\bsop\b|procedure|playbook", re.I)),
    ("policy", re.compile(r"polic(y|ies)|\bstandard\b|guideline", re.I)),
    ("report", re.compile(r"report|monthly[\s_-]?pack|service[\s_-]?review", re.I)),
)


def classify_document(name: str, folder_path: str | None = None) -> DocKind:
    """The kind of a document, from its file name, then its folder."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name or "")
    for candidate in (stem, folder_path or ""):
        for kind, pattern in _KIND_PATTERNS:
            if pattern.search(candidate.replace("_", " ")):
                return kind  # type: ignore[return-value]
    return "other"


@dataclass(frozen=True)
class DocumentStatus:
    document_id: UUID
    tenant_id: UUID
    tenant_name: str
    name: str
    doc_kind: DocKind
    web_url: str | None
    modified_at: datetime | None
    resources_named: int
    removed_resources: tuple[str, ...]
    resources_added_since: int
    live_resources: int
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def stale(self) -> bool:
        return bool(self.reasons)


_STATUS: Final = text("""
WITH docs AS (
    SELECT d.id, d.tenant_id, d.name, d.doc_kind, d.web_url,
           coalesce(d.modified_at, d.ingested_at) AS as_of
    FROM cloudops.document d
    WHERE d.deleted_at IS NULL AND d.extraction = 'text'
      AND d.doc_kind = ANY(CAST(:kinds AS text[]))
),
named AS (
    SELECT DISTINCT c.document_id, r.id AS resource_id, r.name, r.deleted_at
    FROM cloudops.knowledge_chunk c
    JOIN docs ON docs.id = c.document_id
    JOIN cloudops.resource r ON r.tenant_id = c.tenant_id
    WHERE length(r.name) >= :min_chars
      AND strpos(lower(c.body), lower(r.name)) > 0
),
estate AS (
    SELECT tenant_id, min(first_seen) AS baseline,
           count(*) FILTER (WHERE deleted_at IS NULL) AS live
    FROM cloudops.resource GROUP BY tenant_id
)
SELECT d.id, d.tenant_id, t.canonical_name, d.name, d.doc_kind, d.web_url, d.as_of,
       (SELECT count(*) FROM named n WHERE n.document_id = d.id),
       coalesce((SELECT array_agg(DISTINCT n.name ORDER BY n.name) FROM named n
                 WHERE n.document_id = d.id AND n.deleted_at > d.as_of), '{}'),
       (SELECT count(*) FROM cloudops.resource r
         WHERE r.tenant_id = d.tenant_id AND r.deleted_at IS NULL
           AND r.first_seen > greatest(d.as_of, e.baseline + interval '1 day')),
       coalesce(e.live, 0)
FROM docs d
JOIN cloudops.tenant t ON t.id = d.tenant_id
LEFT JOIN estate e ON e.tenant_id = d.tenant_id
ORDER BY t.canonical_name, d.doc_kind, d.name, d.id
""")

_TENANTS: Final = text("SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")


def staleness_reasons(*, as_of: datetime | None, now: datetime, removed: Sequence[str],
                      added_since: int, live: int) -> tuple[str, ...]:
    reasons: list[str] = []
    if removed:
        reasons.append("removed_resources")
    if live > 0 and Decimal(added_since) / Decimal(live) >= ESTATE_DRIFT_FRACTION:
        reasons.append("estate_changed")
    if as_of is None or now - as_of > timedelta(days=STALE_AFTER_DAYS):
        reasons.append("not_updated")
    return tuple(reasons)


async def document_statuses(conn: ScopedConnection, now: datetime,
                            kinds: Sequence[str] = ASSESSED_KINDS) -> list[DocumentStatus]:
    """Every current document of the given kinds in scope, with its staleness."""
    rows = [tuple(r) for r in await conn.execute(
        _STATUS, {"kinds": list(kinds), "min_chars": MIN_NAME_CHARS})]
    out: list[DocumentStatus] = []
    for (doc, tenant, tname, name, kind, url, as_of, named, removed, added, live) in rows:
        removed_names = tuple(str(n) for n in removed or ())
        out.append(DocumentStatus(
            document_id=doc, tenant_id=tenant, tenant_name=tname, name=name, doc_kind=kind,
            web_url=url, modified_at=as_of, resources_named=int(named),
            removed_resources=removed_names, resources_added_since=int(added),
            live_resources=int(live),
            reasons=staleness_reasons(as_of=as_of, now=now, removed=removed_names,
                                      added_since=int(added), live=int(live)),
        ))
    return out


@dataclass(frozen=True)
class CoverageGap:
    tenant_id: UUID
    tenant_name: str
    coverage: Mapping[str, Coverage]  # required kind -> current | stale | missing

    @property
    def has_gap(self) -> bool:
        return any(v != "current" for v in self.coverage.values())


def coverage(tenants: Iterable[tuple[UUID, str]],
             statuses: Iterable[DocumentStatus]) -> list[CoverageGap]:
    """Per customer: is there a current design document and handover pack?"""
    best: dict[tuple[UUID, str], Coverage] = {}
    for s in statuses:
        if s.doc_kind not in REQUIRED_KINDS:
            continue
        key = (s.tenant_id, s.doc_kind)
        if best.get(key) != "current":
            best[key] = "stale" if s.stale else "current"
    return [
        CoverageGap(tid, name, {k: best.get((tid, k), "missing") for k in REQUIRED_KINDS})
        for tid, name in tenants
    ]


async def coverage_gaps(conn: ScopedConnection, now: datetime) -> list[CoverageGap]:
    tenants = [(r[0], str(r[1])) for r in (tuple(x) for x in await conn.execute(_TENANTS))]
    return coverage(tenants, await document_statuses(conn, now, REQUIRED_KINDS))


# --- citations ---------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """Where a retrieved passage came from: document and page, or the record."""

    chunk_id: UUID
    source_kind: str
    source_ref: str | None
    title: str | None
    document_id: UUID | None
    document_name: str | None
    web_url: str | None
    page: int | None
    ordinal: int | None

    def label(self) -> str:
        if self.document_name:
            page = f", page {self.page}" if self.page is not None else ""
            return f"{self.document_name}{page}"
        return self.source_ref or self.title or str(self.chunk_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": str(self.chunk_id), "source_kind": self.source_kind,
            "source_ref": self.source_ref, "title": self.title,
            "document_id": None if self.document_id is None else str(self.document_id),
            "document_name": self.document_name, "web_url": self.web_url,
            "page": self.page, "ordinal": self.ordinal, "label": self.label(),
        }


_CITATIONS: Final = text("""
SELECT c.id, c.source_kind, c.source_ref, c.title, d.id, d.name, d.web_url, c.page, c.ordinal
FROM cloudops.knowledge_chunk c
LEFT JOIN cloudops.document d ON d.id = c.document_id AND d.deleted_at IS NULL
WHERE c.id = ANY(CAST(:ids AS uuid[]))
  AND c.tenant_id = ANY(cloudops.current_tenant_ids())
""")


async def citations(conn: ScopedConnection, chunk_ids: Iterable[UUID]) -> dict[UUID, Citation]:
    """Citations for chunks in scope. A chunk outside scope is simply absent."""
    ids = sorted({str(c) for c in chunk_ids})
    if not ids:
        return {}
    rows = [tuple(r) for r in await conn.execute(_CITATIONS, {"ids": ids})]
    return {r[0]: Citation(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows}


_PAGE_BREAK: Final = "\f"


def page_at(text_body: str, offset: int) -> int:
    """The 1-based page an offset falls on. Pages are separated by form feeds,
    which is how text extraction marks them; text without any is one page."""
    return 1 + text_body.count(_PAGE_BREAK, 0, max(0, offset))
