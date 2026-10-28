"""Retrieval: chunking, embedding, hybrid search (LLD, Vector store; ADR 0001).

    index = KnowledgeIndex(db, gateway)
    await index.add(ctx, source_kind="close_note", source_ref="INC0040123", title=..., text=...)
    hits = await index.search(ctx, "disk full on the file server", kinds=["close_note", "runbook"])

**Indexing:** text is split into chunks of about 600 tokens on paragraph, then
sentence, boundaries, overlapping so an answer straddling a boundary survives.
Each chunk is embedded through the gateway, which redacts the text first,
counts it against quotas and traces it. The original text is stored for
full-text search and citation; only the redacted text ever reaches a model.

**Search** is one SQL statement:
- four ranked branches: vector (HNSW, cosine) and full-text, over the
  tenant's chunks and over Getronics-wide chunks;
- fused by reciprocal rank (k = 60), then cut to the top results.

The tenant predicate is **inside** the SQL of every customer branch, so RLS and
the predicate agree, and no result set is ever filtered after the fact (CLAUDE.md
rule 1). A query that names another customer simply finds nothing of theirs: their
rows are not in any branch.

Re-ranking by the ``small`` tier (LLD) is a later addition, measured against the
evaluation set (step 08) before it is switched on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_gateway.gateway import CallContext, Gateway, _vector
from cloudops_tenancy import Database

TARGET_CHARS: Final = 2400  # about 600 tokens
OVERLAP_CHARS: Final = 300
RRF_K: Final = 60
BRANCH_DEPTH: Final = 40
FTS: Final = "to_tsvector('english', coalesce(title, '') || ' ' || body)"


def chunk_text(body: str, *, target: int = TARGET_CHARS, overlap: int = OVERLAP_CHARS
               ) -> list[str]:
    """Split on paragraphs, then sentences, then hard; overlap the tails."""
    body = body.strip()
    if not body:
        return []
    if len(body) <= target:
        return [body]
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if len(para) <= target:
            pieces.append(para)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", para)
        for s in sentences:
            while len(s) > target:
                pieces.append(s[:target])
                s = s[target - overlap:]
            pieces.append(s)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > target:
            chunks.append(current)
            current = current[-overlap:].strip() + " " + piece
        else:
            current = f"{current} {piece}".strip() if current else piece
    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True)
class Hit:
    origin: str  # "tenant" or "global"
    chunk_id: UUID
    tenant_id: UUID | None
    title: str | None
    body: str
    source_kind: str
    source_ref: str | None
    score: float

    def trace(self) -> dict[str, Any]:
        return {"origin": self.origin, "id": str(self.chunk_id), "score": round(self.score, 6),
                "source_ref": self.source_ref}


def _branches(table: str, tenant_predicate: str) -> str:
    # Module constants only (two table names, one predicate), never input.
    return f"""
    (SELECT id, row_number() OVER (ORDER BY d) AS r FROM (
        SELECT id, embedding <=> CAST(:qvec AS vector) AS d FROM {table}
        WHERE {tenant_predicate} source_kind = ANY(CAST(:kinds AS text[]))
          AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :depth) v)
    UNION ALL
    (SELECT id, row_number() OVER (ORDER BY rank DESC) AS r FROM (
        SELECT id, ts_rank_cd({FTS}, plainto_tsquery('english', :qtext)) AS rank FROM {table}
        WHERE {tenant_predicate} source_kind = ANY(CAST(:kinds AS text[]))
          AND {FTS} @@ plainto_tsquery('english', :qtext)
        ORDER BY rank DESC LIMIT :depth) f)
    """  # noqa: S608


# The tenant predicate is in the SQL, not applied to results afterwards.
SEARCH: Final = text(f"""
WITH tenant_ranks AS ({_branches(
    "cloudops.knowledge_chunk", "tenant_id = ANY(cloudops.current_tenant_ids()) AND")}),
global_ranks AS ({_branches("cloudops.global_knowledge_chunk", "")}),
fused AS (
    SELECT 'tenant' AS origin, id, sum(1.0 / (:k + r)) AS score FROM tenant_ranks GROUP BY id
    UNION ALL
    SELECT 'global', id, sum(1.0 / (:k + r)) FROM global_ranks GROUP BY id
)
SELECT f.origin, f.id, c.tenant_id, coalesce(c.title, g.title), coalesce(c.body, g.body),
       coalesce(c.source_kind, g.source_kind), coalesce(c.source_ref, g.source_ref), f.score
FROM fused f
LEFT JOIN cloudops.knowledge_chunk c
       ON f.origin = 'tenant' AND c.id = f.id
      AND c.tenant_id = ANY(cloudops.current_tenant_ids())
LEFT JOIN cloudops.global_knowledge_chunk g ON f.origin = 'global' AND g.id = f.id
ORDER BY f.score DESC, f.id LIMIT :limit
""")  # noqa: S608 - table names and predicates are module constants

_INSERT: Final = text(
    "INSERT INTO cloudops.knowledge_chunk (tenant_id, source_kind, source_ref, title, body,"
    " embedding) VALUES (:tenant_id, :kind, :ref, :title, :body, CAST(:embedding AS vector))"
    " RETURNING id"
)


class KnowledgeIndex:
    def __init__(self, db: Database, gateway: Gateway) -> None:
        self._db = db
        self._gateway = gateway

    async def add(self, ctx: CallContext, *, source_kind: str, source_ref: str | None,
                  title: str | None, text_body: str) -> list[UUID]:
        chunks = chunk_text(text_body)
        if not chunks:
            return []
        vectors = await self._gateway.embed(ctx, chunks, task="index")
        ids: list[UUID] = []
        async with self._db.transaction(ctx.scope) as conn:
            for body, vector in zip(chunks, vectors, strict=True):
                ids.append(await conn.scalar(_INSERT, {
                    "tenant_id": ctx.tenant_id, "kind": source_kind, "ref": source_ref,
                    "title": title, "body": body, "embedding": _vector(vector),
                }))
        return ids

    async def search(self, ctx: CallContext, query: str, *, kinds: Sequence[str],
                     limit: int = 8) -> list[Hit]:
        (qvec,) = await self._gateway.embed(ctx, [query], task="retrieve")
        async with self._db.transaction(ctx.scope) as conn:
            rows = [tuple(r) for r in await conn.execute(SEARCH, {
                "qvec": _vector(qvec), "qtext": query, "kinds": list(kinds),
                "depth": BRANCH_DEPTH, "k": RRF_K, "limit": limit,
            })]
        return [
            Hit(origin=r[0], chunk_id=r[1], tenant_id=r[2], title=r[3], body=r[4],
                source_kind=r[5], source_ref=r[6], score=float(r[7]))
            for r in rows if r[4] is not None
        ]
