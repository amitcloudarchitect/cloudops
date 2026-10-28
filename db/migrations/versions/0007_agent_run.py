"""Agent run trace (expand).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-28

One row per agent run (packages/agents): which agent, which prompt version
and hash, which tier, the retrieval set, tool calls, totals of tokens,
latency and cost, the validated output or the error, and the gateway calls
(``cloudops.llm_call``) it was made of. This is what makes a disputed action
explainable months later. Tenant data: RLS FORCEd; append-only for the
application.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = """
CREATE TABLE cloudops.agent_run (
    id              uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    principal       text NOT NULL,
    conversation_id uuid,
    agent           text NOT NULL,
    prompt_version  text NOT NULL,
    prompt_hash     text NOT NULL CHECK (prompt_hash ~ '^[0-9a-f]{64}$'),
    tier            text NOT NULL CHECK (tier IN ('small', 'large', 'embed')),
    status          text NOT NULL
                    CHECK (status IN ('answered', 'declined', 'invalid_output', 'escalated',
                                      'refused', 'failed')),
    attempts        integer NOT NULL CHECK (attempts >= 0),
    retrieval       jsonb,
    tool_calls      jsonb,
    llm_call_ids    bigint[] NOT NULL DEFAULT '{}',
    tokens_in       integer NOT NULL DEFAULT 0,
    tokens_out      integer NOT NULL DEFAULT 0,
    cost            numeric(18,6) NOT NULL DEFAULT 0,
    cost_currency   char(3) NOT NULL CHECK (cost_currency ~ '^[A-Z]{3}$'),
    latency_ms      integer NOT NULL DEFAULT 0,
    confidence      numeric(4,3),
    output          jsonb,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE cloudops.agent_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.agent_run FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.agent_run
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));

GRANT SELECT, INSERT ON cloudops.agent_run TO cloudops_app;
"""

CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS agent_run_tenant_created_idx"
    " ON cloudops.agent_run (tenant_id, created_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS agent_run_agent_created_idx"
    " ON cloudops.agent_run (agent, created_at DESC)",
)


def upgrade() -> None:
    op.execute(DDL)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
