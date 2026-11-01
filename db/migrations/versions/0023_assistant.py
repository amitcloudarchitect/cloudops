"""The AMARA assistant: conversations, the customer trace path, help knowledge (expand).

Revision ID: 0023
Revises: 0020
Create Date: 2026-11-01

ADR 0029. The assistant answers questions in the console and the portal, read-only,
about one tenant per conversation.

- ``cloudops.assistant_conversation`` and ``cloudops.assistant_turn``: the history,
  tenant data with the isolation policy FORCEd, and a second, restrictive
  ``owner_only`` policy: a row is visible to the principal who wrote it and nobody
  else, whatever the role. ``app.principal_id`` is set by packages/tenancy with
  ``SET LOCAL``, like the tenant ids. Kept 90 days (``assistant_purge``).
- ``cloudops.tenant.assistant_enabled``: the portal's switch, off by default. Staff
  always have the assistant; a customer only once its tenant is switched on.
- **Quotas for any caller.** ``cloudops.llm_usage``, a definer function returning
  token totals and the tenant breaker's verdict for the caller's tenant and
  principal; never a cost, never a row. 0024 (contract) then lets a customer scope
  write its own trace and use the cache under its own scope key.
- **Help knowledge.** User-facing help pages are Getronics-wide chunks
  (``platform_help``, ``platform_help_staff``) in ``global_knowledge_chunk``. That table
  gains row-level security for one purpose: a customer scope reads ``platform_help``
  and no other kind. ``cloudops.fill_global_embedding`` lets the AI worker fill an
  empty embedding of an unchanged body, and nothing else.
- ``cloudops.internal_tenant()``: the Getronics tenant's id, where a staff help-only
  conversation and the help embeddings are recorded.
- ``cloudops.assistant_forget``: deleting a conversation also clears the prompt text
  and agent output the gateway recorded for it, for the same principal only.
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0020"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

OWNER = "principal = current_setting('app.principal_id', true)"

DDL = f"""
ALTER TABLE cloudops.tenant ADD COLUMN assistant_enabled boolean NOT NULL DEFAULT false;
GRANT SELECT (assistant_enabled) ON cloudops.tenant TO cloudops_customer;
-- Switched by a platform administrator only (the API also requires access.manage).
GRANT UPDATE (assistant_enabled) ON cloudops.tenant TO cloudops_app;
CREATE POLICY assistant_switch ON cloudops.tenant AS RESTRICTIVE FOR UPDATE TO cloudops_app
  USING (current_setting('app.role', true) = 'platform_admin')
  WITH CHECK (current_setting('app.role', true) = 'platform_admin');

CREATE TABLE cloudops.assistant_conversation (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    principal   text NOT NULL,
    mode        text NOT NULL CHECK (mode IN ('account', 'help')),
    title       text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);

CREATE TABLE cloudops.assistant_turn (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    conversation_id uuid NOT NULL,
    principal       text NOT NULL,
    seq             integer NOT NULL CHECK (seq >= 1),
    question        text NOT NULL CHECK (length(question) BETWEEN 1 AND 2000),
    -- The screen the question was asked from, after the server checked its ids.
    context         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'answered', 'dont_know', 'refused',
                                      'failed')),
    stage           text CHECK (stage IN ('understanding', 'retrieving', 'looking_up',
                                          'composing', 'checking')),
    answer          jsonb,
    reason          text CHECK (reason ~ '^[a-z_]{{1,64}}$'),
    agent_run_ids   uuid[] NOT NULL DEFAULT '{{}}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    answered_at     timestamptz,
    FOREIGN KEY (tenant_id, conversation_id)
        REFERENCES cloudops.assistant_conversation (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (conversation_id, seq)
);

ALTER TABLE cloudops.assistant_conversation ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.assistant_conversation FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.assistant_conversation
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY owner_only ON cloudops.assistant_conversation AS RESTRICTIVE
  TO cloudops_app, cloudops_customer USING ({OWNER}) WITH CHECK ({OWNER});

ALTER TABLE cloudops.assistant_turn ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.assistant_turn FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.assistant_turn
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY owner_only ON cloudops.assistant_turn AS RESTRICTIVE
  TO cloudops_app, cloudops_customer USING ({OWNER}) WITH CHECK ({OWNER});

GRANT SELECT, INSERT, UPDATE, DELETE
  ON cloudops.assistant_conversation, cloudops.assistant_turn
  TO cloudops_app, cloudops_customer;

-- Help knowledge: a customer scope reads the customer help pages and nothing else
-- Getronics-wide. The owner (no app.role) loads it; every staff scope reads it all.
ALTER TABLE cloudops.global_knowledge_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.global_knowledge_chunk FORCE ROW LEVEL SECURITY;
CREATE POLICY audience ON cloudops.global_knowledge_chunk
  USING (coalesce(current_setting('app.role', true), '') <> 'customer'
         OR source_kind = 'platform_help');
GRANT SELECT ON cloudops.global_knowledge_chunk TO cloudops_customer;
ALTER TABLE cloudops.global_knowledge_chunk
  ADD COLUMN embedded_at timestamptz,
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

-- Usage for quotas and the tenant breaker, for any caller: totals and a verdict.
GRANT SELECT (tenant_id, principal, task, tokens_in, tokens_out)
  ON cloudops.llm_call TO cloudops_resolver;
CREATE FUNCTION cloudops.llm_usage(p_task_prefix text, p_recent interval,
                                   p_trailing interval, p_multiple numeric, p_floor numeric)
RETURNS TABLE (user_day_tokens bigint, tenant_month_tokens bigint,
               task_user_day_tokens bigint, task_tenant_month_tokens bigint,
               tenant_breaker_open boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH me AS (
        SELECT current_setting('app.principal_id', true) AS principal,
               cloudops.current_tenant_ids() AS tenants
    ), rows AS (
        SELECT c.principal, c.task, c.tokens_in + c.tokens_out AS tokens, c.cost,
               c.created_at
        FROM cloudops.llm_call c, me
        WHERE c.tenant_id = ANY(me.tenants)
          AND c.created_at >= least(date_trunc('month', now()), now() - p_trailing)
    ), totals AS (
        SELECT
          coalesce(sum(tokens) FILTER (WHERE r.principal = me.principal
                     AND created_at >= date_trunc('day', now())), 0) AS user_day,
          coalesce(sum(tokens) FILTER (WHERE created_at >= date_trunc('month', now())), 0)
            AS tenant_month,
          coalesce(sum(tokens) FILTER (WHERE r.principal = me.principal
                     AND starts_with(task, p_task_prefix)
                     AND created_at >= date_trunc('day', now())), 0) AS task_user_day,
          coalesce(sum(tokens) FILTER (WHERE starts_with(task, p_task_prefix)
                     AND created_at >= date_trunc('month', now())), 0) AS task_tenant_month,
          coalesce(sum(cost) FILTER (WHERE created_at >= now() - p_recent), 0) AS recent_spend,
          coalesce(sum(cost) FILTER (WHERE created_at >= now() - p_trailing), 0) AS trailing_spend
        FROM rows r, me
    )
    SELECT user_day::bigint, tenant_month::bigint, task_user_day::bigint,
           task_tenant_month::bigint,
           recent_spend >= p_floor AND recent_spend > p_multiple * trailing_spend
               * (extract(epoch FROM p_recent) / extract(epoch FROM p_trailing))
    FROM totals
$$;
REVOKE ALL ON FUNCTION cloudops.llm_usage(text, interval, interval, numeric, numeric)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.llm_usage(text, interval, interval, numeric, numeric)
  TO cloudops_app, cloudops_customer;
-- The platform breaker's totals (0006), for a customer caller too. Its owner grants it.
SET ROLE cloudops_resolver;
GRANT EXECUTE ON FUNCTION cloudops.llm_platform_spend(interval, interval) TO cloudops_customer;
RESET ROLE;

-- Fill an empty embedding of a help chunk whose body is unchanged; nothing else.
GRANT SELECT (id, body, embedding), UPDATE (embedding, embedded_at)
  ON cloudops.global_knowledge_chunk TO cloudops_resolver;
CREATE POLICY embedding_fill ON cloudops.global_knowledge_chunk FOR UPDATE
  TO cloudops_resolver USING (embedding IS NULL) WITH CHECK (embedding IS NOT NULL);
CREATE FUNCTION cloudops.fill_global_embedding(p_id uuid, p_digest text, p_embedding text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH filled AS (
        UPDATE cloudops.global_knowledge_chunk
           SET embedding = CAST(p_embedding AS public.vector), embedded_at = now()
         WHERE id = p_id AND embedding IS NULL AND md5(body) = p_digest
        RETURNING 1)
    SELECT EXISTS (SELECT 1 FROM filled)
$$;
REVOKE ALL ON FUNCTION cloudops.fill_global_embedding(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.fill_global_embedding(uuid, text, text) TO cloudops_app;

-- Forgetting a conversation: the prompt text and agent output recorded for it,
-- of the calling principal in the calling tenant only.
GRANT SELECT (tenant_id, principal, conversation_id), UPDATE (prompt_redacted)
  ON cloudops.llm_call TO cloudops_resolver;
GRANT SELECT (tenant_id, principal, conversation_id), UPDATE (output)
  ON cloudops.agent_run TO cloudops_resolver;
CREATE POLICY assistant_forget ON cloudops.llm_call FOR UPDATE TO cloudops_resolver
  USING (conversation_id IS NOT NULL);
CREATE POLICY assistant_forget ON cloudops.agent_run FOR UPDATE TO cloudops_resolver
  USING (conversation_id IS NOT NULL);
CREATE POLICY assistant_forget_read ON cloudops.agent_run FOR SELECT TO cloudops_resolver
  USING (conversation_id IS NOT NULL);
CREATE FUNCTION cloudops.assistant_forget(p_conversation uuid)
RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    n integer;
    m integer;
BEGIN
    UPDATE cloudops.llm_call SET prompt_redacted = NULL
     WHERE conversation_id = p_conversation
       AND tenant_id = ANY(cloudops.current_tenant_ids())
       AND principal = current_setting('app.principal_id', true);
    GET DIAGNOSTICS n = ROW_COUNT;
    UPDATE cloudops.agent_run SET output = NULL
     WHERE conversation_id = p_conversation
       AND tenant_id = ANY(cloudops.current_tenant_ids())
       AND principal = current_setting('app.principal_id', true);
    GET DIAGNOSTICS m = ROW_COUNT;
    RETURN n + m;
END
$$;
REVOKE ALL ON FUNCTION cloudops.assistant_forget(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.assistant_forget(uuid) TO cloudops_app, cloudops_customer;

-- Retention: conversations older than 90 days, and their recorded prompt text.
GRANT SELECT (id, created_at), DELETE ON cloudops.assistant_conversation TO cloudops_resolver;
GRANT SELECT (created_at) ON cloudops.llm_call TO cloudops_resolver;
GRANT SELECT (created_at) ON cloudops.agent_run TO cloudops_resolver;
CREATE POLICY assistant_purge ON cloudops.assistant_conversation FOR DELETE
  TO cloudops_resolver USING (updated_at < now() - interval '90 days');
CREATE POLICY assistant_purge_read ON cloudops.assistant_conversation FOR SELECT
  TO cloudops_resolver USING (updated_at < now() - interval '90 days');
GRANT SELECT (updated_at) ON cloudops.assistant_conversation TO cloudops_resolver;
CREATE FUNCTION cloudops.assistant_purge()
RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    n integer;
BEGIN
    DELETE FROM cloudops.assistant_conversation
     WHERE updated_at < now() - interval '90 days';
    GET DIAGNOSTICS n = ROW_COUNT;
    UPDATE cloudops.llm_call SET prompt_redacted = NULL
     WHERE conversation_id IS NOT NULL AND created_at < now() - interval '90 days';
    UPDATE cloudops.agent_run SET output = NULL
     WHERE conversation_id IS NOT NULL AND created_at < now() - interval '90 days';
    RETURN n;
END
$$;
REVOKE ALL ON FUNCTION cloudops.assistant_purge() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.assistant_purge() TO cloudops_app;

-- The internal (Getronics) tenant: where a staff help-only conversation, and the
-- help pages' embeddings, are recorded and billed. Its id and nothing else.
GRANT SELECT (internal) ON cloudops.tenant TO cloudops_resolver;
CREATE FUNCTION cloudops.internal_tenant()
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT t.id FROM cloudops.tenant t WHERE t.internal
$$;
REVOKE ALL ON FUNCTION cloudops.internal_tenant() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.internal_tenant() TO cloudops_app;

GRANT CREATE ON SCHEMA cloudops TO cloudops_resolver;
ALTER FUNCTION cloudops.internal_tenant() OWNER TO cloudops_resolver;
ALTER FUNCTION cloudops.llm_usage(text, interval, interval, numeric, numeric)
  OWNER TO cloudops_resolver;
ALTER FUNCTION cloudops.fill_global_embedding(uuid, text, text) OWNER TO cloudops_resolver;
ALTER FUNCTION cloudops.assistant_forget(uuid) OWNER TO cloudops_resolver;
ALTER FUNCTION cloudops.assistant_purge() OWNER TO cloudops_resolver;
REVOKE CREATE ON SCHEMA cloudops FROM cloudops_resolver;
"""

CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS assistant_conversation_owner_idx"
    " ON cloudops.assistant_conversation (tenant_id, principal, updated_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS assistant_conversation_updated_idx"
    " ON cloudops.assistant_conversation (updated_at)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS assistant_turn_conversation_idx"
    " ON cloudops.assistant_turn (tenant_id, conversation_id, seq)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS llm_call_conversation_idx"
    " ON cloudops.llm_call (conversation_id) WHERE conversation_id IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS agent_run_conversation_idx"
    " ON cloudops.agent_run (conversation_id) WHERE conversation_id IS NOT NULL",
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS global_knowledge_chunk_help_ref_idx"
    " ON cloudops.global_knowledge_chunk (source_kind, source_ref)"
    " WHERE source_kind IN ('platform_help', 'platform_help_staff')",
)


def upgrade() -> None:
    op.execute(DDL)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
