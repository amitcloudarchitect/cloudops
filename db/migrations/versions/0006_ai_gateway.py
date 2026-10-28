"""AI gateway: call ledger and trace, semantic cache, full-text search (expand).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28

LLD, AI platform configuration (Gateway controls, Vector store); ADR 0005.

- ``cloudops.llm_call``: one row per gateway call. It is the usage ledger
  quotas and the spend breaker read, and the trace (redacted prompt, retrieval
  set, tool calls, tokens, latency, cost). Tenant data: RLS FORCEd like every
  other tenant table. Append-only for the application.
- ``cloudops.llm_cache``: the semantic response cache. Each entry belongs to
  one tenant (RLS) and is keyed by the caller's role class and subscription
  scope as well, so an answer is never served across tenants, roles or
  subscription limits. Entries expire (1 h live state, 24 h static knowledge).
- ``cloudops.llm_platform_spend``: the platform-wide spend breaker needs spend
  across every tenant. Like the other resolvers it is a SECURITY DEFINER
  function owned by cloudops_resolver, returning two totals and nothing else.
- Full-text indexes for hybrid retrieval on both knowledge tables.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = """
CREATE TABLE cloudops.llm_call (
    id              bigserial PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    principal       text NOT NULL,
    conversation_id uuid,
    task            text NOT NULL,
    tier_requested  text NOT NULL CHECK (tier_requested IN ('small', 'large', 'embed')),
    tier_used       text CHECK (tier_used IN ('small', 'large', 'embed')),
    provider        text,
    deployment      text,
    prompt_redacted text,
    redactions      integer NOT NULL DEFAULT 0,
    retrieval       jsonb,
    tool_calls      jsonb,
    tokens_in       integer NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out      integer NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost            numeric(18,6) NOT NULL DEFAULT 0,
    cost_currency   char(3) NOT NULL CHECK (cost_currency ~ '^[A-Z]{3}$'),
    latency_ms      integer,
    cache_hit       boolean NOT NULL DEFAULT false,
    outcome         text NOT NULL
                    CHECK (outcome IN ('succeeded', 'refused', 'failed', 'escalated')),
    reason          text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cloudops.llm_cache (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    scope_key   text NOT NULL,   -- role class + subscription scope, hashed
    tier        text NOT NULL,
    task        text NOT NULL,
    prompt_hash text NOT NULL,
    embedding   vector(1536) NOT NULL,
    response    text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);

ALTER TABLE cloudops.llm_call ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.llm_call FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.llm_call
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));

ALTER TABLE cloudops.llm_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.llm_cache FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.llm_cache
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));

GRANT SELECT, INSERT ON cloudops.llm_call, cloudops.llm_cache TO cloudops_app;
GRANT USAGE ON SEQUENCE cloudops.llm_call_id_seq TO cloudops_app;

-- The breaker's view across tenants: totals only, through this function.
GRANT SELECT (cost, created_at) ON cloudops.llm_call TO cloudops_resolver;
CREATE POLICY spend_registry_read ON cloudops.llm_call FOR SELECT
    TO cloudops_resolver USING (true);

CREATE FUNCTION cloudops.llm_platform_spend(p_recent interval, p_trailing interval)
RETURNS TABLE (recent_spend numeric, trailing_spend numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT coalesce(sum(cost) FILTER (WHERE created_at >= now() - p_recent), 0),
           coalesce(sum(cost) FILTER (WHERE created_at >= now() - p_trailing), 0)
    FROM cloudops.llm_call
    WHERE created_at >= now() - greatest(p_recent, p_trailing)
$$;
REVOKE ALL ON FUNCTION cloudops.llm_platform_spend(interval, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.llm_platform_spend(interval, interval) TO cloudops_app;
GRANT CREATE ON SCHEMA cloudops TO cloudops_resolver;
ALTER FUNCTION cloudops.llm_platform_spend(interval, interval) OWNER TO cloudops_resolver;
REVOKE CREATE ON SCHEMA cloudops FROM cloudops_resolver;
"""

CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS llm_call_tenant_created_idx"
    " ON cloudops.llm_call (tenant_id, created_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS llm_call_principal_created_idx"
    " ON cloudops.llm_call (principal, created_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS llm_call_created_idx"
    " ON cloudops.llm_call (created_at)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS llm_cache_lookup_idx"
    " ON cloudops.llm_cache (tenant_id, scope_key, tier, task, expires_at)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS llm_cache_embedding_idx"
    " ON cloudops.llm_cache USING hnsw (embedding vector_cosine_ops)"
    " WITH (m = 16, ef_construction = 64)",
    # Hybrid retrieval: the query must use exactly this expression.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS knowledge_chunk_fts_idx"
    " ON cloudops.knowledge_chunk USING gin"
    " (to_tsvector('english', coalesce(title, '') || ' ' || body))",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS global_knowledge_chunk_fts_idx"
    " ON cloudops.global_knowledge_chunk USING gin"
    " (to_tsvector('english', coalesce(title, '') || ' ' || body))",
)


def upgrade() -> None:
    op.execute(DDL)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
