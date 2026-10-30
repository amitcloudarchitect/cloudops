"""Triage results and their human review (expand).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-30

``cloudops.triage_result``: one row per triage run (stages 1 to 3), written by
the workflow's last activity: the route, the reasons, the classification and
enrichment as the agents returned them, the injection signals, the agent runs
it was made of, and its cost. Until now the outcome lived only in Temporal's
history; the console reads it here.

``cloudops.triage_review``: a person's decision on a triage result, accept or
override, with what they changed and why. Append-only: a later decision is a
new row, never an edit, so the record of who decided what stays intact.
Nothing here acts on a customer environment; planning and execution
(stages 4 and later) are not built.

Both are tenant data: RLS FORCEd, tenant_isolation, and the account_scope
restriction work items carry, so a subscription-limited principal sees triage
only for resources in its subscriptions.

New permission ``triage.review`` (ADR 0014), granted by default to the
Engineer, Delivery lead and Platform administrator roles.
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = """
CREATE TABLE cloudops.triage_result (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    number            text NOT NULL,
    resource_id       uuid REFERENCES cloudops.resource(id),
    route             text NOT NULL CHECK (route IN ('ready_for_planning', 'human')),
    stage             smallint NOT NULL CHECK (stage BETWEEN 1 AND 3),
    reasons           text[] NOT NULL DEFAULT '{}',
    classification    jsonb,
    enrichment        jsonb,
    injection_signals jsonb NOT NULL DEFAULT '[]',
    agent_run_ids     uuid[] NOT NULL DEFAULT '{}',
    cost              numeric(18,6) NOT NULL DEFAULT 0,
    cost_currency     char(3) NOT NULL CHECK (cost_currency ~ '^[A-Z]{3}$'),
    workflow_id       text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE cloudops.triage_result ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.triage_result FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.triage_result
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY account_scope ON cloudops.triage_result AS RESTRICTIVE
  USING (cloudops.account_scope_is_all()
         OR EXISTS (SELECT 1 FROM cloudops.resource r WHERE r.id = resource_id));

GRANT SELECT, INSERT ON cloudops.triage_result TO cloudops_app;


CREATE TABLE cloudops.triage_review (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    triage_result_id uuid NOT NULL REFERENCES cloudops.triage_result(id) ON DELETE CASCADE,
    decision         text NOT NULL CHECK (decision IN ('accept', 'override')),
    overrides        jsonb,
    reason           text,
    reviewer_oid     uuid NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    -- An override says what changed and why; an acceptance changes nothing. Each
    -- term is coalesced: a NULL CHECK passes, so a missing value must be false.
    CHECK (decision = 'accept' AND overrides IS NULL
           OR decision = 'override'
              AND coalesce(jsonb_typeof(overrides) = 'object', false)
              AND coalesce(overrides <> '{}'::jsonb, false)
              AND length(btrim(coalesce(reason, ''))) > 0)
);

ALTER TABLE cloudops.triage_review ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.triage_review FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.triage_review
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
-- Visible exactly when its triage result is: that row's own policies decide.
CREATE POLICY account_scope ON cloudops.triage_review AS RESTRICTIVE
  USING (EXISTS (SELECT 1 FROM cloudops.triage_result t WHERE t.id = triage_result_id));

-- Append-only for the application: no UPDATE, no DELETE.
GRANT SELECT, INSERT ON cloudops.triage_review TO cloudops_app;


INSERT INTO access.permission (name, description, is_commercial)
VALUES ('triage.review', 'Accept or override an agent triage', false);
INSERT INTO access.role_permission (role_id, permission)
SELECT r.id, 'triage.review' FROM access.role_definition r
WHERE r.is_system AND r.role_class IN ('engineer', 'delivery_lead', 'platform_admin');
"""

CONCURRENT_INDEXES = (
    # One result per workflow run: what makes record_outcome safe to retry.
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS triage_result_workflow_idx"
    " ON cloudops.triage_result (workflow_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS triage_result_tenant_number_idx"
    " ON cloudops.triage_result (tenant_id, number, created_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS triage_review_result_idx"
    " ON cloudops.triage_review (triage_result_id, created_at DESC)",
)


def upgrade() -> None:
    op.execute(DDL)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
