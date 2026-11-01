"""Commercial setup: the SOW register written through the platform (expand).

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-01

ADR 0035. Until now SOWs came only from migrations and the seed (ADR 0019,
"margin needs SOW capture"; ADR 0021, "no API writes the uplift"). Settings →
Commercial setup records them, and asks the FinOps worker to recompute margin.

- ``cloudops.sow`` gains the register's fields: the contract term
  (``term_start``, ``term_end``), ``service_scope``, the ``recurring_fee`` and its
  ``billing_frequency``, who entered it, the version it ``amends`` and why. The
  monthly manage revenue margin reads is derived from the fee (fee / months in
  the billing period), stored at numeric(18,6) like every figure.
- **Versioned, never overwritten.** A tenant's SOWs, ordered by
  ``effective_from``, are its versions. An amendment is a new row effective from
  a later date; the version before it is closed the day before (its
  ``effective_to``), so exactly one is in force on any day, as margin, the
  customer price and report packs read it. ``sow_history`` refuses every other
  change, for the owner too: no figure, date or reference of a version is ever
  edited, and an end date can only move earlier, never later or away.
- ``access.record_sow(actor, tenant, sow)`` is the only write path the
  application has: SECURITY DEFINER, owned by ``cloudops_access_admin``,
  executable by ``cloudops_app``. It requires ``commercial.write`` (commercial,
  so 0004's trigger keeps it off every non-commercial role class, customer roles
  above all, ADR 0002 D10), the tenant assigned and in scope, and an effective
  date after the current version's. Row-level security still applies inside it:
  ``tenant_isolation`` and ``commercial_visibility`` read the caller's settings.
- ``cloudops.margin_recompute``: a request to recompute one customer's margin
  now rather than at the next daily run. The API inserts it through
  ``access.request_margin_recompute``; the FinOps worker claims it under the
  finops role and closes it. Status only moves forward; the request never
  changes. FORCE RLS, ``tenant_isolation``, ``commercial_visibility``, and
  per-customer only (no subscription-limited scope), like margin.
"""

from __future__ import annotations

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

COMMERCIAL_ROLES = ("ceo", "cfo", "finops", "delivery_lead", "commercial", "auditor",
                    "platform_admin")
# Default holders of commercial.write: who owns SOW commercials (HLD: the platform
# is sole master; commercial and Finance enter them), and the platform administrator.
WRITERS = ("CFO", "Commercial", "Platform administrator")

DDL = """
ALTER TABLE cloudops.sow
    ADD COLUMN term_start        date,
    ADD COLUMN term_end          date,
    ADD COLUMN service_scope     text CHECK (length(btrim(service_scope)) BETWEEN 1 AND 500),
    ADD COLUMN recurring_fee     numeric(18,6) CHECK (recurring_fee >= 0),
    ADD COLUMN billing_frequency text
               CHECK (billing_frequency IN ('monthly', 'quarterly', 'annual')),
    ADD COLUMN amends            uuid REFERENCES cloudops.sow(id),
    ADD COLUMN amendment_reason  text
               CHECK (length(btrim(amendment_reason)) BETWEEN 3 AND 500),
    ADD COLUMN entered_by        text;
ALTER TABLE cloudops.sow ADD CONSTRAINT sow_fee_has_frequency
    CHECK ((recurring_fee IS NULL) = (billing_frequency IS NULL));
ALTER TABLE cloudops.sow ADD CONSTRAINT sow_term_in_order
    CHECK (term_end IS NULL OR term_start IS NULL OR term_end >= term_start);
ALTER TABLE cloudops.sow ADD CONSTRAINT sow_effective_in_order
    CHECK (effective_to IS NULL OR effective_to >= effective_from);

-- A version is history. Its end date may only be brought forward (an amendment
-- closes it), and nothing else about it changes, whoever asks.
CREATE FUNCTION cloudops.check_sow_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - 'effective_to') IS DISTINCT FROM (to_jsonb(OLD) - 'effective_to')
       OR NEW.effective_to IS NULL AND OLD.effective_to IS NOT NULL
       OR NEW.effective_to > OLD.effective_to THEN
        RAISE EXCEPTION 'a SOW version is never edited: record an amendment'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sow_history
    BEFORE UPDATE ON cloudops.sow
    FOR EACH ROW EXECUTE FUNCTION cloudops.check_sow_history();

CREATE TABLE cloudops.margin_recompute (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    requested_by uuid NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    status       text NOT NULL DEFAULT 'requested'
                 CHECK (status IN ('requested', 'running', 'succeeded', 'failed')),
    started_at   timestamptz,
    finished_at  timestamptz,
    CHECK ((status IN ('requested', 'running')) = (finished_at IS NULL)),
    CHECK ((status = 'requested') = (started_at IS NULL))
);

CREATE FUNCTION cloudops.check_recompute_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.requested_by, NEW.requested_at)
       IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.requested_by, OLD.requested_at)
       OR NOT (OLD.status = 'requested' AND NEW.status = 'running'
               OR OLD.status = 'running' AND NEW.status IN ('succeeded', 'failed')) THEN
        RAISE EXCEPTION 'recompute requests only move forward'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER margin_recompute_forward_only
    BEFORE UPDATE ON cloudops.margin_recompute
    FOR EACH ROW EXECUTE FUNCTION cloudops.check_recompute_transition();
"""


def _security() -> str:
    roles = ",".join(f"'{r}'" for r in COMMERCIAL_ROLES)
    return f"""
ALTER TABLE cloudops.margin_recompute ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.margin_recompute FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.margin_recompute
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY commercial_visibility ON cloudops.margin_recompute
  AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') IN ({roles}));
-- Margin is per customer, never per subscription (D16).
CREATE POLICY account_scope ON cloudops.margin_recompute AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());

GRANT SELECT ON cloudops.margin_recompute TO cloudops_app;
GRANT SELECT, UPDATE (status, started_at, finished_at) ON cloudops.margin_recompute
    TO cloudops_ingest;
GRANT SELECT, INSERT ON cloudops.margin_recompute TO cloudops_access_admin;
GRANT SELECT, INSERT, UPDATE (effective_to) ON cloudops.sow TO cloudops_access_admin;
"""


FUNCTIONS = """
-- The actor holds commercial.write as staff, is assigned the tenant, and has it
-- in this transaction's scope. A tenant outside any of these is indistinguishable
-- from one that does not exist.
CREATE FUNCTION access.require_commercial_writer(p_actor uuid, p_tenant uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class <> 'customer' AND rp.permission = 'commercial.write'
    ) THEN
        RAISE EXCEPTION 'not permitted to record commercial terms'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT (p_tenant = ANY(cloudops.current_tenant_ids()))
       OR NOT EXISTS (
           SELECT 1 FROM access.principal p WHERE p.oid = p_actor
             AND (p.all_tenants OR EXISTS (
                 SELECT 1 FROM access.principal_tenant pt
                 WHERE pt.oid = p.oid AND pt.tenant_id = p_tenant)))
       OR NOT EXISTS (SELECT 1 FROM cloudops.tenant t WHERE t.id = p_tenant) THEN
        RAISE EXCEPTION 'tenant not found' USING ERRCODE = 'CMSCP';
    END IF;
END $$;

-- Records a SOW version. The first for a tenant, or an amendment effective after
-- the current one: the current one is then closed the day before, and is
-- otherwise untouched. Money arrives as decimal strings and is cast here.
CREATE FUNCTION access.record_sow(p_actor uuid, p_tenant uuid, p_sow jsonb) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_from      date;
    v_months    integer;
    v_fee       numeric(18,6);
    v_prev      uuid;
    v_prev_from date;
    v_prev_to   date;
    v_id        uuid;
BEGIN
    PERFORM access.require_commercial_writer(p_actor, p_tenant);
    v_from := (p_sow->>'effective_from')::date;
    v_fee := (p_sow->>'recurring_fee')::numeric;
    v_months := CASE p_sow->>'billing_frequency'
                    WHEN 'monthly' THEN 1 WHEN 'quarterly' THEN 3 WHEN 'annual' THEN 12 END;
    IF v_from IS NULL OR v_fee IS NULL OR v_months IS NULL THEN
        RAISE EXCEPTION 'effective date, fee and billing frequency are required'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- One writer per customer decides what "the current version" is.
    PERFORM pg_advisory_xact_lock(hashtext('sow:' || p_tenant::text));
    SELECT s.id, s.effective_from, s.effective_to INTO v_prev, v_prev_from, v_prev_to
    FROM cloudops.sow s WHERE s.tenant_id = p_tenant
    ORDER BY s.effective_from DESC, s.created_at DESC LIMIT 1;
    IF v_prev IS NOT NULL AND v_from <= v_prev_from THEN
        RAISE EXCEPTION 'an amendment takes effect after the current version'
            USING ERRCODE = 'SWDAT';
    END IF;
    IF v_prev IS NOT NULL AND length(btrim(coalesce(p_sow->>'amendment_reason', ''))) < 3 THEN
        RAISE EXCEPTION 'an amendment says why' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF v_prev IS NOT NULL AND (v_prev_to IS NULL OR v_prev_to >= v_from) THEN
        UPDATE cloudops.sow SET effective_to = v_from - 1 WHERE id = v_prev;
    END IF;
    INSERT INTO cloudops.sow (tenant_id, reference, effective_from, effective_to, currency,
        monthly_manage_revenue, hosting_acv, manage_acv, professional_services,
        contracted_ci_count, coverage, support, document_uri, target_margin,
        consumption_uplift, term_start, term_end, service_scope, recurring_fee,
        billing_frequency, amends, amendment_reason, entered_by)
    VALUES (p_tenant, btrim(p_sow->>'reference'), v_from, (p_sow->>'term_end')::date,
        p_sow->>'currency', round(v_fee / v_months, 6),
        (p_sow->>'hosting_acv')::numeric, (p_sow->>'manage_acv')::numeric,
        coalesce((p_sow->>'professional_services')::numeric, 0),
        (p_sow->>'contracted_ci_count')::integer,
        (p_sow->>'coverage')::cloudops.coverage_model,
        (p_sow->>'support')::cloudops.support_model,
        btrim(p_sow->>'document_uri'), (p_sow->>'target_margin')::numeric,
        (p_sow->>'consumption_uplift')::numeric, (p_sow->>'term_start')::date,
        (p_sow->>'term_end')::date, btrim(p_sow->>'service_scope'), v_fee,
        p_sow->>'billing_frequency', v_prev,
        CASE WHEN v_prev IS NULL THEN NULL ELSE btrim(p_sow->>'amendment_reason') END,
        p_actor::text)
    RETURNING id INTO v_id;
    RETURN v_id;
END $$;

-- Ask the FinOps worker to recompute a customer's margin now. One open request
-- per customer is enough: a second while one waits returns the waiting one.
CREATE FUNCTION access.request_margin_recompute(p_actor uuid, p_tenant uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_id uuid;
BEGIN
    PERFORM access.require_commercial_writer(p_actor, p_tenant);
    PERFORM pg_advisory_xact_lock(hashtext('margin_recompute:' || p_tenant::text));
    SELECT r.id INTO v_id FROM cloudops.margin_recompute r
    WHERE r.tenant_id = p_tenant AND r.status = 'requested';
    IF v_id IS NULL THEN
        INSERT INTO cloudops.margin_recompute (tenant_id, requested_by)
        VALUES (p_tenant, p_actor) RETURNING id INTO v_id;
    END IF;
    RETURN v_id;
END $$;
"""

SIGNATURES = (
    "access.require_commercial_writer(uuid, uuid)",
    "access.record_sow(uuid, uuid, jsonb)",
    "access.request_margin_recompute(uuid, uuid)",
)


def _ownership() -> str:
    # Grants first: once cloudops_access_admin owns a function, only it may grant on it.
    lines = [f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC;" for fn in SIGNATURES]
    lines += [
        "GRANT EXECUTE ON FUNCTION access.record_sow(uuid, uuid, jsonb) TO cloudops_app;",
        "GRANT EXECUTE ON FUNCTION access.request_margin_recompute(uuid, uuid)"
        " TO cloudops_app;",
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
    ]
    lines += [f"ALTER FUNCTION {fn} OWNER TO cloudops_access_admin;" for fn in SIGNATURES]
    lines.append("REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;")
    return "\n".join(lines)


def _permission() -> str:
    writers = ", ".join(f"'{r}'" for r in WRITERS)
    return (
        "INSERT INTO access.permission (name, description, is_commercial) VALUES"
        " ('commercial.write', 'Record SOWs and amendments; recompute margin', true);\n"
        "INSERT INTO access.role_permission (role_id, permission)"
        " SELECT r.id, 'commercial.write' FROM access.role_definition r"
        f" WHERE r.is_system AND r.name IN ({writers});\n"
    )


CONCURRENT = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS margin_recompute_waiting_idx"
    " ON cloudops.margin_recompute (requested_at) WHERE status = 'requested'",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS margin_recompute_tenant_idx"
    " ON cloudops.margin_recompute (tenant_id, requested_at DESC)",
)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(_security())
    op.execute(FUNCTIONS)
    op.execute(_ownership())
    op.execute(_permission())
    with op.get_context().autocommit_block():
        for statement in CONCURRENT:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
