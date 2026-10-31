"""Observability: the ticket's trace, LLM trace ids, the internal tenant (expand).

Revision ID: 0014
Revises: 0012
Create Date: 2026-10-31

Step 19, LLD Observability; ADR 0021.

- ``cloudops.work_item.trace_parent``: the W3C traceparent of the ticket's root
  span, written at first sighting, so every later stage (update, triage,
  closure) opens its span in the same trace. Random, never derived from the
  tenant or the ticket number. Under the table's existing RLS.
- ``cloudops.llm_call.trace_id`` and ``cloudops.agent_run.trace_id``: the trace
  a call or run belongs to. Azure Monitor holds no tenant attribute (ADR 0021),
  so per-tenant LLM attribution (prompt, retrieval set, tool calls, tokens, cost,
  user) stays here under RLS, and the trace id is how an engineer gets from one
  to the other.
- ``cloudops.tenant.internal``: at most one tenant is Getronics itself, so the
  platform's own Azure cost runs through the customer FinOps code unchanged
  (LLD, Observability: the platform's own cost). Assigned like any tenant.

The customer database role (0011) gets no grant on any new column: none is
for customers.

Revises 0012 on this branch. Step 17 adds 0013 in parallel; whichever merges
second sets ``down_revision`` to the other.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0012"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

TRACEPARENT = r"'^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$'"
TRACE_ID = r"'^[0-9a-f]{32}$'"

DDL = f"""
ALTER TABLE cloudops.work_item ADD COLUMN trace_parent text
    CHECK (trace_parent ~ {TRACEPARENT});
ALTER TABLE cloudops.llm_call ADD COLUMN trace_id text CHECK (trace_id ~ {TRACE_ID});
ALTER TABLE cloudops.agent_run ADD COLUMN trace_id text CHECK (trace_id ~ {TRACE_ID});
ALTER TABLE cloudops.tenant ADD COLUMN internal boolean NOT NULL DEFAULT false;
"""

CONCURRENT_INDEXES = (
    # At most one internal tenant.
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS tenant_one_internal_idx"
    " ON cloudops.tenant ((true)) WHERE internal",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS llm_call_trace_idx"
    " ON cloudops.llm_call (trace_id) WHERE trace_id IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS agent_run_trace_idx"
    " ON cloudops.agent_run (trace_id) WHERE trace_id IS NOT NULL",
)


def upgrade() -> None:
    op.execute(DDL)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
