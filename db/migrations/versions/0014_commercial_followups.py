"""Step 16 follow-ups: margin knows whether its cost reconciled; an FX correction
keeps every directed pair of its day consistent.

Revision ID: 0014
Revises: 0013

``margin_snapshot.cost_verified`` (step 16 decision: stored, not looked up at
read time). True when every account with consumption cost in the month has a
latest reconciliation check that is verified: the invoice once one exists,
else the provider's month-to-date total. False otherwise, including an account
never checked. NULL when there is no cost to verify (no cost data). Stored, so
a closed month keeps what was true when it closed, and a change in
verification restates it as a new version like any other figure.

``access.correct_fx_rate`` (step 16 decision: the inverse and the crosses).
Rates are stored for every directed pair because ``fx_rate_on`` has no inverse
fallback (the ECB connector derives them the same way). Correcting A -> B alone
left B -> A and every cross through A or B disagreeing with it. Now, on the
same day and rate type, in one transaction:

- A -> B is the corrected rate (as before; unchanged rejects with FXSAM);
- B -> A = 1 / rate;
- for each X with a stored A -> X, the legs from A are held fixed and
  B -> X = (A -> X) / rate, X -> B = rate / (A -> X).

Each pair that moves gets its own ``fx_restatement`` entry, its reason naming
the correction it derives from, and ``source = 'finance'`` so the next ECB run
does not undo it. Rounded to numeric(18,8) by the column, once.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# "expand" or "contract" (LLD, Migrations). Expand migrations may only add.
CHANGE_KIND = "expand"

DDL = """
ALTER TABLE cloudops.margin_snapshot ADD COLUMN cost_verified boolean;
"""

FUNCTIONS = """
-- One directed rate and its explicit entry. NULL when the stored rate already
-- equals it at numeric(18,8). Executable by its owner only: the entry points
-- are the access functions that check the actor.
CREATE FUNCTION access.set_fx_rate_entry(
    p_actor uuid, p_from char(3), p_to char(3), p_rate_type text, p_effective date,
    p_rate numeric, p_reason text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_new numeric(18,8) := p_rate;
    v_previous numeric(18,8);
    v_id bigint;
BEGIN
    SELECT f.rate INTO v_previous FROM cloudops.fx_rate f
    WHERE f.from_currency = p_from AND f.to_currency = p_to AND f.rate_type = p_rate_type
      AND f.effective_date = p_effective;
    IF v_previous = v_new THEN
        RETURN NULL;
    END IF;
    INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate, rate_type,
                                  effective_date, source)
    VALUES (p_from, p_to, v_new, p_rate_type, p_effective, 'finance')
    ON CONFLICT (from_currency, to_currency, rate_type, effective_date)
    DO UPDATE SET rate = EXCLUDED.rate, source = EXCLUDED.source;
    INSERT INTO cloudops.fx_restatement (from_currency, to_currency, rate_type,
        effective_date, previous_rate, new_rate, source, reason, entered_by)
    VALUES (p_from, p_to, p_rate_type, p_effective, v_previous, v_new, 'finance',
            left(btrim(p_reason), 500), p_actor::text)
    RETURNING id INTO v_id;
    RETURN v_id;
END $$;

-- Finance corrects a rate: the rate, its inverse and the crosses through it
-- change together, each with its explicit entry, in one transaction.
CREATE OR REPLACE FUNCTION access.correct_fx_rate(
    p_actor uuid, p_from char(3), p_to char(3), p_rate_type text, p_effective date,
    p_rate numeric, p_reason text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_id bigint;
    v_derived text;
    r record;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition rd ON rd.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND rd.role_class <> 'customer' AND rp.permission = 'fx.manage'
    ) THEN
        RAISE EXCEPTION 'not permitted to correct FX rates'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_from = p_to THEN
        RAISE EXCEPTION 'a rate needs two currencies' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_rate IS NULL OR p_rate <= 0 THEN
        RAISE EXCEPTION 'a rate is positive' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- Every pair of the day and type may move: one lock for all of them.
    PERFORM pg_advisory_xact_lock(hashtext('fx:' || p_rate_type || p_effective::text));
    v_id := access.set_fx_rate_entry(p_actor, p_from, p_to, p_rate_type, p_effective,
                                     p_rate, p_reason);
    IF v_id IS NULL THEN
        RAISE EXCEPTION 'that is already the rate' USING ERRCODE = 'FXSAM';
    END IF;
    v_derived := 'Derived from correction ' || v_id || ': ' || btrim(p_reason);
    PERFORM access.set_fx_rate_entry(p_actor, p_to, p_from, p_rate_type, p_effective,
                                     1 / p_rate, v_derived);
    FOR r IN
        SELECT f.to_currency AS x, f.rate FROM cloudops.fx_rate f
        WHERE f.from_currency = p_from AND f.rate_type = p_rate_type
          AND f.effective_date = p_effective AND f.to_currency <> p_to
        ORDER BY f.to_currency
    LOOP
        PERFORM access.set_fx_rate_entry(p_actor, p_to, r.x, p_rate_type, p_effective,
                                         r.rate / p_rate, v_derived);
        PERFORM access.set_fx_rate_entry(p_actor, r.x, p_to, p_rate_type, p_effective,
                                         p_rate / r.rate, v_derived);
    END LOOP;
    RETURN v_id;
END $$;
"""

HELPER = "access.set_fx_rate_entry(uuid, char, char, text, date, numeric, text)"


def _as_owner() -> str:
    """The access functions belong to cloudops_access_admin (0012). The migrator
    acts as it, by SET ROLE as 0011 does, to replace one and define its helper:
    ownership, and the grants on correct_fx_rate, stay as they were."""
    return "\n".join([
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
        "SET LOCAL ROLE cloudops_access_admin;",
        FUNCTIONS,
        f"REVOKE ALL ON FUNCTION {HELPER} FROM PUBLIC;",
        "RESET ROLE;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;",
    ])


def upgrade() -> None:
    op.execute(DDL)
    op.execute(_as_owner())


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
