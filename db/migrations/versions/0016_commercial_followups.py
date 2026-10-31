"""Step 16 follow-ups: margin knows whether its cost reconciled; an FX correction
keeps every directed pair of its day consistent.

Revision ID: 0016
Revises: 0015

``margin_snapshot.cost_verified`` (step 16 decision: stored, not looked up at
read time). True when every account with consumption cost in the month has a
latest reconciliation check that is verified: the invoice once one exists,
else the provider's month-to-date total. False otherwise, including an account
never checked. NULL when there is no cost to verify (no cost data). Stored, so
a closed month keeps what was true when it closed, and a change in
verification restates it as a new version like any other figure.

``access.correct_fx_rate`` (step 16 decisions: the inverse and the crosses move
with a correction; EUR is the anchor; for a pair without EUR, Finance names the
EUR leg that moves). Rates are stored for every directed pair because
``fx_rate_on`` has no inverse fallback, and the ECB connector derives every pair
from the EUR reference rates. A correction keeps that shape:

- The correction names one moving currency M (not EUR): for a pair with EUR it
  is the other currency; for a pair without EUR Finance names which of the two
  (``p_moves``). The other one, H, is held, and so is every other EUR leg: a
  GBP/USD fix never rewrites EUR/USD unless Finance says USD moves.
- From the corrected rate and the held leg EUR -> H (the latest on or before the
  day, as ``fx_rate_on`` reads it; 1 when H is EUR), EUR -> M follows. Then, on
  the correction's day: A -> B is the corrected rate, B -> A its inverse, and
  every other pair with M (EUR <-> M, X <-> M for each X with an EUR rate on or
  before the day) is re-derived from the EUR rates. No pair without M moves.
- A held leg with no rate on or before the day is refused (FXHLD): the crosses
  would be neither derivable nor consistent. A pair without EUR and no
  ``p_moves`` is refused (FXLEG).
- Each pair that moves gets its own ``fx_restatement`` entry; derived ones name
  the correction. Source ``finance``, so the ECB feed does not undo them.
  Resubmitting a rate that is already stored repairs derived pairs that
  disagree; it is refused as unchanged (FXSAM) only when nothing moves.

The 7-argument function stays (expand-only) for pairs with EUR and refuses a
pair without; the 8-argument one takes ``p_moves``. Both run the same code.
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
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

-- Finance corrects a rate. EUR anchors: M moves, H and every other EUR leg are
-- held; A -> B, its inverse and every pair with M are set on the day.
CREATE FUNCTION access.correct_fx_rate(
    p_actor uuid, p_from char(3), p_to char(3), p_rate_type text, p_effective date,
    p_rate numeric, p_reason text, p_moves char(3)
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_moves char(3);
    v_held char(3);
    v_eur_held numeric;
    v_eur_moves numeric;
    v_first bigint;
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
    IF p_from = 'EUR' OR p_to = 'EUR' THEN
        v_moves := CASE WHEN p_from = 'EUR' THEN p_to ELSE p_from END;
        IF p_moves IS NOT NULL AND p_moves <> v_moves THEN
            RAISE EXCEPTION 'with EUR in the pair, the other currency moves'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    ELSIF p_moves IS NULL THEN
        RAISE EXCEPTION 'name the EUR leg that moves' USING ERRCODE = 'FXLEG';
    ELSIF p_moves NOT IN (p_from, p_to) THEN
        RAISE EXCEPTION 'the moving currency is one of the pair'
            USING ERRCODE = 'invalid_parameter_value';
    ELSE
        v_moves := p_moves;
    END IF;
    v_held := CASE WHEN v_moves = p_from THEN p_to ELSE p_from END;
    -- Every pair of the day and type may move: one lock for all of them.
    PERFORM pg_advisory_xact_lock(hashtext('fx:' || p_rate_type || p_effective::text));
    IF v_held = 'EUR' THEN
        v_eur_held := 1;
    ELSE
        SELECT f.rate INTO v_eur_held FROM cloudops.fx_rate f
        WHERE f.from_currency = 'EUR' AND f.to_currency = v_held
          AND f.rate_type = p_rate_type AND f.effective_date <= p_effective
        ORDER BY f.effective_date DESC LIMIT 1;
        IF v_eur_held IS NULL THEN
            RAISE EXCEPTION 'no EUR rate for the held currency on or before that day'
                USING ERRCODE = 'FXHLD';
        END IF;
    END IF;
    -- A -> B = EUR->B / EUR->A, solved for the moving side.
    v_eur_moves := CASE WHEN v_moves = p_to THEN p_rate * v_eur_held
                        ELSE v_eur_held / p_rate END;
    v_first := access.set_fx_rate_entry(p_actor, p_from, p_to, p_rate_type, p_effective,
                                        p_rate, p_reason);
    v_derived := 'Derived from correction of ' || p_from || '->' || p_to || ': '
                 || btrim(p_reason);
    v_id := access.set_fx_rate_entry(p_actor, p_to, p_from, p_rate_type, p_effective,
                                     1 / p_rate, v_derived);
    v_first := coalesce(v_first, v_id);
    FOR r IN
        -- EUR itself, and each other currency's latest EUR rate on or before the day.
        SELECT 'EUR'::char(3) AS x, 1::numeric AS rate
        UNION ALL
        SELECT legs.x, legs.rate FROM (
            SELECT DISTINCT ON (f.to_currency) f.to_currency AS x, f.rate
            FROM cloudops.fx_rate f
            WHERE f.from_currency = 'EUR' AND f.rate_type = p_rate_type
              AND f.effective_date <= p_effective AND f.to_currency NOT IN ('EUR', v_moves)
            ORDER BY f.to_currency, f.effective_date DESC) legs
    LOOP
        CONTINUE WHEN r.x IN (p_from, p_to);  -- the corrected pair, set above
        v_id := access.set_fx_rate_entry(p_actor, v_moves, r.x, p_rate_type, p_effective,
                                         r.rate / v_eur_moves, v_derived);
        v_first := coalesce(v_first, v_id);
        v_id := access.set_fx_rate_entry(p_actor, r.x, v_moves, p_rate_type, p_effective,
                                         v_eur_moves / r.rate, v_derived);
        v_first := coalesce(v_first, v_id);
    END LOOP;
    IF v_first IS NULL THEN
        RAISE EXCEPTION 'that is already the rate' USING ERRCODE = 'FXSAM';
    END IF;
    RETURN v_first;
END $$;

-- The 0012 signature, kept (expand-only): pairs with EUR; a pair without is
-- refused until the EUR leg is named.
CREATE OR REPLACE FUNCTION access.correct_fx_rate(
    p_actor uuid, p_from char(3), p_to char(3), p_rate_type text, p_effective date,
    p_rate numeric, p_reason text
) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT access.correct_fx_rate(p_actor, p_from, p_to, p_rate_type, p_effective,
                                  p_rate, p_reason, NULL::char(3))
$$;
"""

HELPER = "access.set_fx_rate_entry(uuid, char, char, text, date, numeric, text)"
CORRECT = "access.correct_fx_rate(uuid, char, char, text, date, numeric, text, char)"


def _as_owner() -> str:
    """The access functions belong to cloudops_access_admin (0012). The migrator
    acts as it, by SET ROLE as 0011 does, to replace one and define its helper:
    ownership, and the grants on correct_fx_rate, stay as they were."""
    return "\n".join([
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
        "SET LOCAL ROLE cloudops_access_admin;",
        FUNCTIONS,
        f"REVOKE ALL ON FUNCTION {HELPER} FROM PUBLIC;",
        f"REVOKE ALL ON FUNCTION {CORRECT} FROM PUBLIC;",
        f"GRANT EXECUTE ON FUNCTION {CORRECT} TO cloudops_app;",
        "RESET ROLE;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;",
    ])


def upgrade() -> None:
    op.execute(DDL)
    op.execute(_as_owner())


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
