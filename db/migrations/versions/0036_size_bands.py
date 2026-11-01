"""Customer size bands: thresholds editable from Settings (expand).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-01

ADR 0038. The home dashboard's Platform section puts every customer in a size
band: Large, Medium or Small, the higher of a spend band and a resource-count
band (user decision, 2026-11-01). The thresholds are the user's, and editable.

- ``cloudops.size_band_policy``: one row per version of the thresholds. The
  latest ``version`` is in force; nothing is ever edited or removed (a trigger
  refuses UPDATE, DELETE and TRUNCATE, for the owner too), so every change and
  its reason stays readable. Spend is monthly and in ``spend_currency``
  (``numeric(18,6)``, CLAUDE.md rule 5). Below ``*_medium_from`` is Small, above
  ``*_large_above`` is Large, and the closed range between is Medium.
- Platform-wide reference data with no ``tenant_id``, like ``fx_rate``. FORCE RLS:
  staff scopes read it; a customer scope reads no row, and ``cloudops_customer``
  holds no grant at all. Only ``cloudops_access_admin`` inserts.
- ``access.set_size_bands(actor, doc, reason)`` is the only write path: SECURITY
  DEFINER, owned by ``cloudops_access_admin``, executable by ``cloudops_app``. It
  requires ``sizebands.manage`` held as staff. The request itself is in
  ``audit.action_log`` (the API's audit stage).
- ``sizebands.manage`` is a new matrix permission, not commercial, granted by
  default to the Platform administrator.

Version 1 is the user's rule: spend Large above EUR 50,000 a month, Medium from
EUR 10,000; resources Large above 1,000, Medium from 200.
"""

from __future__ import annotations

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = """
CREATE TABLE cloudops.size_band_policy (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version               integer NOT NULL UNIQUE CHECK (version > 0),
    spend_currency        char(3) NOT NULL CHECK (spend_currency ~ '^[A-Z]{3}$'),
    spend_medium_from     numeric(18,6) NOT NULL CHECK (spend_medium_from > 0),
    spend_large_above     numeric(18,6) NOT NULL,
    resources_medium_from integer NOT NULL CHECK (resources_medium_from > 0),
    resources_large_above integer NOT NULL,
    reason                text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
    set_by                text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CHECK (spend_large_above >= spend_medium_from),
    CHECK (resources_large_above >= resources_medium_from)
);

CREATE FUNCTION cloudops.reject_size_band_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'a size band version is history (% refused): set a new one', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER size_band_policy_immutable
    BEFORE UPDATE OR DELETE ON cloudops.size_band_policy
    FOR EACH ROW EXECUTE FUNCTION cloudops.reject_size_band_change();
CREATE TRIGGER size_band_policy_no_truncate
    BEFORE TRUNCATE ON cloudops.size_band_policy
    FOR EACH STATEMENT EXECUTE FUNCTION cloudops.reject_size_band_change();

INSERT INTO cloudops.size_band_policy (version, spend_currency, spend_medium_from,
    spend_large_above, resources_medium_from, resources_large_above, reason, set_by)
VALUES (1, 'EUR', 10000, 50000, 200, 1000,
    'The user''s rule (ADR 0038): the higher of the spend and resource bands',
    'migration:0036');
"""

# FORCE, not just ENABLE: without it the owner bypasses every policy.
SECURITY = """
ALTER TABLE cloudops.size_band_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.size_band_policy FORCE ROW LEVEL SECURITY;
CREATE POLICY size_band_read ON cloudops.size_band_policy FOR SELECT
  USING (coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer'));
CREATE POLICY size_band_write ON cloudops.size_band_policy FOR INSERT
  TO cloudops_access_admin WITH CHECK (true);
GRANT SELECT ON cloudops.size_band_policy TO cloudops_app;
GRANT SELECT, INSERT ON cloudops.size_band_policy TO cloudops_access_admin;
"""

SET = """
-- Who: sizebands.manage, held as staff. What: a whole set of thresholds, checked
-- by the table's constraints; the version before it is untouched.
CREATE FUNCTION access.set_size_bands(p_actor uuid, p_doc jsonb, p_reason text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_id uuid;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class <> 'customer' AND rp.permission = 'sizebands.manage'
    ) THEN
        RAISE EXCEPTION 'not permitted to set size bands'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- One writer at a time decides the next version.
    PERFORM pg_advisory_xact_lock(hashtext('size_band_policy'));
    INSERT INTO cloudops.size_band_policy (version, spend_currency, spend_medium_from,
        spend_large_above, resources_medium_from, resources_large_above, reason, set_by)
    VALUES (
        (SELECT coalesce(max(version), 0) + 1 FROM cloudops.size_band_policy),
        p_doc->>'spend_currency', (p_doc->>'spend_medium_from')::numeric,
        (p_doc->>'spend_large_above')::numeric, (p_doc->>'resources_medium_from')::integer,
        (p_doc->>'resources_large_above')::integer, btrim(p_reason), p_actor::text)
    RETURNING id INTO v_id;
    RETURN v_id;
END $$;
"""

SIGNATURE = "access.set_size_bands(uuid, jsonb, text)"


def _ownership() -> str:
    return "\n".join([
        f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC;",
        f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO cloudops_app;",
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
        f"ALTER FUNCTION {SIGNATURE} OWNER TO cloudops_access_admin;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;",
    ])


PERMISSION = """
INSERT INTO access.permission (name, description, is_commercial) VALUES
    ('sizebands.manage', 'Set the customer size band thresholds (spend and resources)', false);
INSERT INTO access.role_permission (role_id, permission)
    SELECT r.id, 'sizebands.manage' FROM access.role_definition r
    WHERE r.is_system AND r.name = 'Platform administrator';
"""


def upgrade() -> None:
    # Seeded before row-level security is on: the migrator has no app.role.
    op.execute(DDL)
    op.execute(SECURITY)
    op.execute(SET)
    op.execute(_ownership())
    op.execute(PERMISSION)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
