"""Revenue & margin: whether Getronics resells the customer's cloud (expand).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-01

ADR 0038. The Revenue & margin view reads what the commercial model already
holds: the SOW's monthly manage revenue, hosting and manage ACV and professional
services (0001, entered through Settings since 0033), margin snapshots and cost
facts. One field was missing:

- ``cloudops.sow.csp``: whether Getronics resells the customer's cloud as a Cloud
  Solution Provider. Nullable: a SOW recorded before this field is *not recorded*,
  never "no". Part of the version like every other field, so ``sow_history``
  (0033) refuses to edit it; a change is the next amendment.
- ``access.record_sow`` is replaced, same signature, to take ``csp``. It stays
  owned by ``cloudops_access_admin``; the migrator acts as it (SET LOCAL ROLE, as
  0016 does), so ownership and the grant to ``cloudops_app`` are unchanged.

No new table: the column inherits ``cloudops.sow``'s FORCE RLS, tenant isolation
and the restrictive ``commercial_visibility`` policy, and ``cloudops_customer``
still holds no grant on it.
"""

from __future__ import annotations

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = "ALTER TABLE cloudops.sow ADD COLUMN csp boolean;"

FUNCTION = """
CREATE OR REPLACE FUNCTION access.record_sow(p_actor uuid, p_tenant uuid, p_sow jsonb) RETURNS uuid
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
        billing_frequency, amends, amendment_reason, entered_by, csp)
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
        p_actor::text, (p_sow->>'csp')::boolean)
    RETURNING id INTO v_id;
    RETURN v_id;
END $$;
"""


def _as_owner() -> str:
    return "\n".join([
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
        "SET LOCAL ROLE cloudops_access_admin;",
        FUNCTION,
        "RESET ROLE;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;",
    ])


def upgrade() -> None:
    op.execute(DDL)
    op.execute(_as_owner())


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
