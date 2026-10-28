"""Getronics-wide knowledge moves to its own table (expand).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28

ADR 0001 (docs/adr/0001-global-knowledge-table.md). Content that belongs to no
customer — shared runbooks, best practice — lives in
cloudops.global_knowledge_chunk, which has no tenant_id. That lets 0003 make
knowledge_chunk.tenant_id NOT NULL, so every customer-data table carries a
tenant with no exceptions.

The global table is reference data protected by grants, like fx_rate: every
scope may read it, no application role may write it. Who authors global content
is a later decision; until then only the schema owner can.

Expand step: create the table and copy existing NULL-tenant rows into it, ids
preserved. knowledge_chunk is untouched here.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

TABLE = """
CREATE TABLE cloudops.global_knowledge_chunk (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_kind text NOT NULL,
    source_ref  text,
    title       text,
    body        text NOT NULL,
    embedding   vector(1536),
    created_at  timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON cloudops.global_knowledge_chunk TO cloudops_app, cloudops_ingest;
"""

# Runs as the owner. knowledge_chunk's policy lets an unscoped session see
# exactly the NULL-tenant rows, which are the ones to copy; RLS is FORCEd, so
# nothing else is visible to this statement.
BACKFILL = """
INSERT INTO cloudops.global_knowledge_chunk
    (id, source_kind, source_ref, title, body, embedding, created_at)
SELECT id, source_kind, source_ref, title, body, embedding, created_at
FROM cloudops.knowledge_chunk
WHERE tenant_id IS NULL
ON CONFLICT (id) DO NOTHING;
"""

CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS global_knowledge_chunk_embedding_idx"
    " ON cloudops.global_knowledge_chunk USING hnsw (embedding vector_cosine_ops)"
    " WITH (m = 16, ef_construction = 64)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS global_knowledge_chunk_kind_idx"
    " ON cloudops.global_knowledge_chunk (source_kind)",
)


def upgrade() -> None:
    op.execute(TABLE)
    op.execute(BACKFILL)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
