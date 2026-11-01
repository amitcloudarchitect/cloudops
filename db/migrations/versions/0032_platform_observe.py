"""The console's Observability page: a matrix permission and one role lookup (expand).

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-01

The Observability page (``/observability``) shows how the platform itself is used
and behaving: who signs in, by role; AI gateway calls, tokens and spend per agent
and customer; triage throughput; connector health; the platform's own cost. No
permission in the matrix answered "may see platform usage across tenants", so:

- ``platform.observe``, a staff matrix permission, granted by default to the
  Platform administrator only. Administrators may grant it to another staff role
  in the matrix like any other.
- A trigger refusing ``platform.observe`` to a customer-class role, as 0011 does
  for ``access.manage``: usage across tenants is never a customer's to read,
  whatever an administrator grants.
- ``access.observed_roles(actor, oids)``: the role of each principal named, for the
  active-users-by-role figures. The application passes only the actors it read
  from ``audit.action_log`` under its own scope, so the function reveals a role,
  never a principal the caller could not already see acting. It refuses anyone
  but active staff holding ``platform.observe``. Display names are not returned.
"""

from __future__ import annotations

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

PERMISSION = """
INSERT INTO access.permission (name, description, is_commercial)
VALUES ('platform.observe', 'Platform usage, AI, triage and connector health across tenants',
        false);
INSERT INTO access.role_permission (role_id, permission)
SELECT r.id, 'platform.observe' FROM access.role_definition r
WHERE r.is_system AND r.name = 'Platform administrator';

CREATE FUNCTION access.reject_customer_platform_observe() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NEW.permission = 'platform.observe' AND EXISTS (
        SELECT 1 FROM access.role_definition r
        WHERE r.id = NEW.role_id AND r.role_class = 'customer') THEN
        RAISE EXCEPTION 'platform.observe cannot be granted to a customer role'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER role_permission_customer_platform_observe
    BEFORE INSERT OR UPDATE ON access.role_permission
    FOR EACH ROW EXECUTE FUNCTION access.reject_customer_platform_observe();
REVOKE ALL ON FUNCTION access.reject_customer_platform_observe() FROM PUBLIC;
"""

FUNCTION = """
CREATE FUNCTION access.observed_roles(p_actor uuid, p_oids uuid[])
RETURNS TABLE (oid uuid, kind text, role_name text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class <> 'customer' AND rp.permission = 'platform.observe'
    ) THEN
        RAISE EXCEPTION 'not permitted to observe the platform'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN QUERY
    SELECT p.oid, p.kind, r.name FROM access.principal p
    JOIN access.role_definition r ON r.id = p.role_id
    WHERE p.oid = ANY(p_oids)
    ORDER BY p.oid;
END $$;
REVOKE ALL ON FUNCTION access.observed_roles(uuid, uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION access.observed_roles(uuid, uuid[]) TO cloudops_app;
-- Grants first: once ownership moves, the migrator can no longer grant.
GRANT CREATE ON SCHEMA access TO cloudops_resolver;
ALTER FUNCTION access.observed_roles(uuid, uuid[]) OWNER TO cloudops_resolver;
REVOKE CREATE ON SCHEMA access FROM cloudops_resolver;
"""


def upgrade() -> None:
    op.execute(PERMISSION)
    op.execute(FUNCTION)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
