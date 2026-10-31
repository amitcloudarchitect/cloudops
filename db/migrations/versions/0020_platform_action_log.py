"""The platform action log: what a principal with no tenant in scope did (expand).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-31

ADR 0017, decision 12 (#38) lets an all-tenant administrator with no tenant yet
resolve to an empty scope, so a new environment's first customer can be onboarded.
But every request is recorded in ``audit.action_log``, one row per tenant in
scope, and with no tenant there was nothing to record it under: the write failed,
and the API withheld every response (``audit_failed``). No page loaded, so no
first tenant could be onboarded.

``audit.action_log`` stays as it is: a tenant table whose ``tenant_id`` is never
null (ADR 0001). A request made with an empty scope is recorded here instead:

- ``audit.platform_action_log``: actor, action, target, outcome, time. No tenant
  and no customer data: the action is the route template, the target its path
  parameters (ids), never a request body.
- Append-only, for everyone: the application may only insert; a trigger refuses
  update, delete and truncate, the owner included.
- Row-level security, forced: a row can be inserted only from a transaction whose
  scope is empty, so a scoped request can never record itself here instead of
  under its tenants. Only the ``auditor`` role class reads it.

"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = """
CREATE TABLE audit.platform_action_log (
    id          bigserial PRIMARY KEY,
    actor       text NOT NULL,
    action      text NOT NULL,
    target_kind text,
    target_ref  text,
    outcome     text NOT NULL
                CHECK (outcome IN ('pending','succeeded','failed','rolled_back','blocked')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION audit.reject_platform_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.platform_action_log is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER platform_action_log_append_only
    BEFORE UPDATE OR DELETE ON audit.platform_action_log
    FOR EACH ROW EXECUTE FUNCTION audit.reject_platform_mutation();
CREATE TRIGGER platform_action_log_no_truncate
    BEFORE TRUNCATE ON audit.platform_action_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_platform_mutation();
REVOKE ALL ON audit.platform_action_log FROM PUBLIC;

ALTER TABLE audit.platform_action_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.platform_action_log FORCE ROW LEVEL SECURITY;
-- Written only by a request with no tenant in scope.
CREATE POLICY empty_scope_only ON audit.platform_action_log FOR INSERT
  WITH CHECK (cardinality(cloudops.current_tenant_ids()) = 0);
-- Read only by the auditor role class.
CREATE POLICY auditors_read ON audit.platform_action_log FOR SELECT
  USING (coalesce(current_setting('app.role', true), '') = 'auditor');

GRANT SELECT, INSERT ON audit.platform_action_log TO cloudops_app;
GRANT USAGE ON SEQUENCE audit.platform_action_log_id_seq TO cloudops_app;
"""


CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS platform_action_log_created_idx"
    " ON audit.platform_action_log (created_at DESC)",
)


def upgrade() -> None:
    op.execute(DDL)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
