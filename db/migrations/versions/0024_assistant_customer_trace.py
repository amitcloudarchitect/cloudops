"""The assistant's customer trace and cache paths: staff-only policies replaced (contract).

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-01

ADR 0029, D5 and D6. ADR 0018 made ``llm_call``, ``agent_run`` and ``llm_cache``
staff-only with one restrictive policy each, for every command. A customer's
assistant must record its own calls, and may use the semantic cache under its own
scope key, so those policies are replaced:

- ``llm_call`` and ``agent_run``: staff-only for SELECT, UPDATE and DELETE, each its
  own restrictive policy. INSERT is left to ``tenant_isolation``, and
  ``cloudops_customer`` is granted INSERT only, so a customer scope writes the trace
  of its own calls and never reads one. The gateway takes ``llm_call`` ids from the
  sequence, because ``RETURNING`` would need SELECT.
- ``llm_cache``: ``cache_scope``. Staff as before; a customer scope only rows whose
  scope key is its own (role class and subscription scope, hashed exactly as the
  gateway's ``CallContext.cache_scope_key``).

The locks name the two roles a request runs as, so the definer functions of 0023
(owned by ``cloudops_resolver``) are governed by their own policies.

**Contract, and why it ships with its expand.** It drops and recreates policies. For
staff nothing changes; the only new access is a customer scope's insert, which the
release before never attempts. So the running release keeps working on either side
of it, and the two-release spacing is not needed.
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

CHANGE_KIND = "contract"

# What the gateway's CallContext.cache_scope_key hashes: role class and subscription
# scope, exactly as packages/tenancy sets them.
CACHE_SCOPE_KEY = (
    "encode(sha256(convert_to(coalesce(current_setting('app.role', true), '') || '|'"
    " || coalesce(current_setting('app.account_ids', true), ''), 'UTF8')), 'hex')"
)
STAFF = "coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer')"

DDL = f"""
-- The ledger and trace: a customer scope may write its own rows, never read them.
-- The staff-only locks name the two roles a request runs as, so the definer
-- functions of 0023 (owned by cloudops_resolver) are governed by their own policies.
DROP POLICY staff_only ON cloudops.llm_call;
DROP POLICY staff_only ON cloudops.agent_run;
CREATE POLICY staff_only ON cloudops.llm_call AS RESTRICTIVE FOR SELECT
  TO cloudops_app, cloudops_customer USING ({STAFF});
CREATE POLICY staff_only_update ON cloudops.llm_call AS RESTRICTIVE FOR UPDATE
  TO cloudops_app, cloudops_customer USING ({STAFF});
CREATE POLICY staff_only_delete ON cloudops.llm_call AS RESTRICTIVE FOR DELETE
  TO cloudops_app, cloudops_customer USING ({STAFF});
CREATE POLICY staff_only ON cloudops.agent_run AS RESTRICTIVE FOR SELECT
  TO cloudops_app, cloudops_customer USING ({STAFF});
CREATE POLICY staff_only_update ON cloudops.agent_run AS RESTRICTIVE FOR UPDATE
  TO cloudops_app, cloudops_customer USING ({STAFF});
CREATE POLICY staff_only_delete ON cloudops.agent_run AS RESTRICTIVE FOR DELETE
  TO cloudops_app, cloudops_customer USING ({STAFF});
GRANT INSERT ON cloudops.llm_call, cloudops.agent_run TO cloudops_customer;
GRANT USAGE ON SEQUENCE cloudops.llm_call_id_seq TO cloudops_customer;

-- The cache: staff as before; a customer scope only under its own scope key.
DROP POLICY staff_only ON cloudops.llm_cache;
CREATE POLICY cache_scope ON cloudops.llm_cache AS RESTRICTIVE
  TO cloudops_app, cloudops_customer
  USING      ({STAFF} OR scope_key = {CACHE_SCOPE_KEY})
  WITH CHECK ({STAFF} OR scope_key = {CACHE_SCOPE_KEY});
GRANT SELECT (tenant_id, scope_key, tier, task, embedding, response, expires_at),
      INSERT ON cloudops.llm_cache TO cloudops_customer;
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    raise NotImplementedError("roll forward, or restore with PITR")
