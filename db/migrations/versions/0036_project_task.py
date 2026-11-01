"""ServiceNow project tasks: delivered project price and proposed initiatives (expand).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-01

ADR 0038. The user's decision (2026-11-01): new-project and proposed-initiative
prices come from ServiceNow project tasks, closed and open.

- ``cloudops.project_task``: one ServiceNow ``pm_project_task``, keyed by its
  ``sys_id``. ``status`` is ``open`` or ``closed``, read from the state choice;
  ``price`` is ``numeric(18,6)`` with ``price_currency`` beside it, both or
  neither, and ``price_field`` names the source field it was read from (ADR 0038:
  which cost stands for the price is a setting, so a row says which one priced it). Every field not
  mapped is kept in ``raw_extra``.
- FORCE RLS, ``tenant_isolation``, and the restrictive ``commercial_visibility``:
  a price is commercial, so an engineer or a customer inside the tenant reads no
  row, whatever the API does. Unlike the other commercial tables, it is written
  by a connector, so the policy also admits the ingest role class, but only on
  a login that is a member of ``cloudops_ingest``: the application login setting
  ``app.role = 'ingest'`` still sees nothing. Per customer only, like margin: a
  subscription-limited scope sees none of it.
- ``staging.ingest_run.status`` gains ``source_absent``: the run found the
  source table does not exist on the instance (Project Portfolio Management not
  active). A supported state, not a failure: connector health shows it as such.
- ``staging.connector_schedule``: ``servicenow``/``pm_project_task`` every six
  hours (ADR 0037, business data).
- ``cloudops.project_task_source()``: whether the source has been read, whether
  it exists, and when a sync last succeeded, for the API to tell "no project
  data" from zero and say how fresh the figures are. SECURITY DEFINER over the
  run log, which the application cannot read; one word and one time.
- ``cloudops.project_services(p_first, p_last)``: delivered project price per
  tenant, close date (UTC) and currency, closed tasks closed in the range, so
  the caller converts each amount at the rate on the day it was delivered,
  for the revenue table's professional services. SECURITY INVOKER, so this
  table's FORCE RLS, tenant isolation and commercial visibility decide every
  row; EXECUTE to the application only. Unpriced tasks are not summed (the API
  counts them). ``provisional`` is true while the source is not ``available``
  (never synced, or the table absent).
"""

from __future__ import annotations

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

# 0001's list, unchanged: the roles commercial_visibility admits.
COMMERCIAL_ROLES = ("ceo", "cfo", "finops", "delivery_lead", "commercial", "auditor",
                    "platform_admin")
RUN_STATUSES = ("running", "succeeded", "dead_lettered", "failed", "source_absent")


def _list(names: tuple[str, ...]) -> str:
    return ", ".join(f"'{n}'" for n in names)


DDL = f"""
CREATE TABLE cloudops.project_task (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES cloudops.tenant(id),
    sys_id            text NOT NULL UNIQUE CHECK (sys_id ~ '^[0-9a-f]{{32}}$'),
    number            text NOT NULL,
    company_raw       text NOT NULL,
    project           text,
    short_description text,
    state             text,
    status            text NOT NULL CHECK (status IN ('open', 'closed')),
    opened_at         timestamptz,
    start_at          timestamptz,
    end_at            timestamptz,
    closed_at         timestamptz,
    source_updated_at timestamptz NOT NULL,
    price             numeric(18,6),
    price_currency    char(3) CHECK (price_currency ~ '^[A-Z]{{3}}$'),
    price_field       text,
    raw_extra         jsonb,
    ingested_at       timestamptz NOT NULL DEFAULT now(),
    CHECK ((price IS NULL) = (price_currency IS NULL)),
    CHECK (price IS NULL OR price_field IS NOT NULL)
);

ALTER TABLE cloudops.project_task ENABLE ROW LEVEL SECURITY;
-- FORCE, not just ENABLE: without it the owner bypasses every policy.
ALTER TABLE cloudops.project_task FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.project_task
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY commercial_visibility ON cloudops.project_task
  AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') IN ({_list(COMMERCIAL_ROLES)})
         OR (coalesce(current_setting('app.role', true), '') = 'ingest'
             AND pg_has_role(current_user, 'cloudops_ingest', 'USAGE')));
CREATE POLICY account_scope ON cloudops.project_task AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());

GRANT SELECT ON cloudops.project_task TO cloudops_app;
GRANT SELECT, INSERT, UPDATE ON cloudops.project_task TO cloudops_ingest;

ALTER TABLE staging.ingest_run DROP CONSTRAINT ingest_run_status_check;
ALTER TABLE staging.ingest_run ADD CONSTRAINT ingest_run_status_check
    CHECK (status IN ({_list(RUN_STATUSES)})) NOT VALID;
ALTER TABLE staging.ingest_run VALIDATE CONSTRAINT ingest_run_status_check;

INSERT INTO staging.connector_schedule (source_system, entity, every)
VALUES ('servicenow', 'pm_project_task', interval '6 hours')
ON CONFLICT (source_system, entity) DO UPDATE SET every = EXCLUDED.every;

-- No tenant data: one word ('never_synced', 'source_absent' or 'available', from
-- the latest finished incremental run) and when a run last succeeded.
CREATE FUNCTION cloudops.project_task_source()
RETURNS TABLE (state text, last_success_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT coalesce((
        SELECT CASE WHEN r.status = 'source_absent' THEN 'source_absent' ELSE 'available' END
        FROM staging.ingest_run r
        WHERE r.source_system = 'servicenow' AND r.entity = 'pm_project_task'
          AND r.mode = 'incremental' AND r.status <> 'running'
        ORDER BY r.started_at DESC LIMIT 1), 'never_synced'),
      (SELECT max(r.finished_at) FROM staging.ingest_run r
        WHERE r.source_system = 'servicenow' AND r.entity = 'pm_project_task'
          AND r.mode = 'incremental' AND r.status = 'succeeded')
$$;
REVOKE ALL ON FUNCTION cloudops.project_task_source() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.project_task_source() TO cloudops_app;

CREATE FUNCTION cloudops.project_services(p_first date, p_last date)
RETURNS TABLE (tenant_id uuid, delivered_on date, currency char(3),
               delivered numeric(18,6), tasks integer, as_of timestamptz, provisional boolean)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
    SELECT t.tenant_id, (t.closed_at AT TIME ZONE 'UTC')::date, t.price_currency,
           sum(t.price)::numeric(18,6), count(*)::integer, s.last_success_at,
           s.state <> 'available'
    FROM cloudops.project_task t CROSS JOIN cloudops.project_task_source() s
    WHERE t.status = 'closed' AND t.price IS NOT NULL
      AND (t.closed_at AT TIME ZONE 'UTC')::date BETWEEN p_first AND p_last
    GROUP BY 1, 2, 3, s.last_success_at, s.state
    ORDER BY 1, 2, 3
$$;
REVOKE ALL ON FUNCTION cloudops.project_services(date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.project_services(date, date) TO cloudops_app;
"""

CONCURRENT = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS project_task_closed_idx"
    " ON cloudops.project_task (tenant_id, closed_at) WHERE status = 'closed'",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS project_task_open_idx"
    " ON cloudops.project_task (tenant_id, opened_at) WHERE status = 'open'",
)


def upgrade() -> None:
    op.execute(DDL)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
