"""Platform-level onboarding: many internal tenants, one platform host, one owner
per account (expand).

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-01

ADR 0031; amends ADR 0023 and ADR 0027 (user decisions, 2026-11-01).

- ``cloudops.tenant.internal`` stays: a tenant is internal to Getronics or a
  customer, chosen by a platform administrator when it is onboarded and changeable
  later. Many tenants may be internal (0030 drops 0019's "at most one" index); an
  internal tenant is reported on exactly like a customer.
- ``cloudops.tenant.hosts_platform``: at most one tenant, internal, is the one the
  platform itself runs in. Its platform resources are the platform's own cost
  (``platform_metrics``, the home page), and the assistant's help-only history and
  the help pages' embeddings are recorded under it. ``cloudops.platform_tenant()``
  returns it; ``cloudops.internal_tenant()`` (0023) now returns the same, so a
  pod of the previous release reads one tenant, not an arbitrary internal one.
- **One owner per account.** PR 62's sharing (the internal tenant and one customer
  on one subscription) is withdrawn. Each pair it made is folded into its customer:
  the customer row is kept, the provider id's alias moves to it (so the platform's
  own cost feed, ADR 0023, writes there), and the internal tenant's row is deleted
  with what hangs off it (the same subscription's cost, read a second time). 0030,
  the contract, drops the trigger and enforces it with a unique index.
- The writers, SECURITY DEFINER functions owned by ``cloudops_access_admin``, the
  last two needing ``access.manage`` as staff and the tenant in scope, all logged:

    onboard_tenant(actor, name, code, internal)    0009's, with the choice made at
                                                   onboarding (internal: access.manage)
    set_tenant_internal(actor, tenant, internal)   internal or customer, later
    set_platform_host(actor, tenant)               the platform's host (NULL: none)

  An internal tenant that hosts the platform cannot become a customer without first
  giving the host flag up (a table check).
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = """
ALTER TABLE cloudops.tenant ADD COLUMN hosts_platform boolean NOT NULL DEFAULT false;
ALTER TABLE cloudops.tenant ADD CONSTRAINT tenant_platform_host_is_internal
    CHECK (NOT hosts_platform OR internal);

-- The writers below change these two columns on any tenant, a second one included
-- when the host moves. Update only, and only for the function owner, which no login
-- can become; the functions check the actor.
GRANT SELECT (id, internal, hosts_platform), UPDATE (internal, hosts_platform)
    ON cloudops.tenant TO cloudops_access_admin;
CREATE POLICY tenant_kind_change ON cloudops.tenant FOR UPDATE
    TO cloudops_access_admin USING (true) WITH CHECK (true);
CREATE POLICY tenant_kind_read ON cloudops.tenant FOR SELECT
    TO cloudops_access_admin USING (true);
GRANT SELECT (hosts_platform) ON cloudops.tenant TO cloudops_resolver;
"""

# Each shared pair (0025) back to one owner: the customer. Runs once; 0030 makes
# sure no pair comes back. The tables are FORCE RLS, so the migrator names its
# scope, every tenant, for this transaction only (set_config local, as SET LOCAL):
# the resolver, which may list tenant ids, reads them.
FOLD = """
SET LOCAL ROLE cloudops_resolver;
SELECT set_config('app.tenant_ids',
                  coalesce((SELECT string_agg(id::text, ',') FROM cloudops.tenant), ''), true);
RESET ROLE;
SELECT set_config('app.account_ids', 'all', true);
DO $$
DECLARE
    v_pair record;
    v_moved int := 0;
BEGIN
    FOR v_pair IN
        SELECT i.id AS dropped, c.tenant_id AS owner, i.tenant_id AS was,
               i.provider, i.native_id
        FROM cloudops.cloud_account i
        JOIN cloudops.tenant ti ON ti.id = i.tenant_id AND ti.internal
        JOIN cloudops.cloud_account c ON c.provider = i.provider
             AND c.native_id = i.native_id AND c.tenant_id <> i.tenant_id
        JOIN cloudops.tenant tc ON tc.id = c.tenant_id AND NOT tc.internal
    LOOP
        UPDATE cloudops.tenant_alias SET tenant_id = v_pair.owner
         WHERE source_system = v_pair.provider AND alias = v_pair.native_id
           AND tenant_id = v_pair.was;
        DELETE FROM cloudops.cloud_account WHERE id = v_pair.dropped;
        v_moved := v_moved + 1;
    END LOOP;
    UPDATE cloudops.cloud_account SET shared_with_platform = false WHERE shared_with_platform;
    RAISE NOTICE 'accounts folded into their single owner: %', v_moved;
END $$;
SELECT set_config('app.tenant_ids', '', true), set_config('app.account_ids', '', true);
"""

WRITERS = (
    "onboard_tenant(uuid, text, text, boolean)",
    "set_tenant_internal(uuid, uuid, boolean)",
    "set_platform_host(uuid, uuid)",
)

FUNCTIONS = """
-- 0009's onboarding, choosing internal or customer at once. A customer needs the
-- mapper's permission only, as before; an internal tenant also a platform
-- administrator's (it is reported like a customer, but may host the platform).
CREATE FUNCTION access.onboard_tenant(
    p_actor uuid, p_canonical_name text, p_short_code text, p_internal boolean
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_id uuid;
BEGIN
    IF p_internal IS NULL THEN
        RAISE EXCEPTION 'internal or not' USING ERRCODE = 'check_violation';
    END IF;
    IF p_internal THEN
        PERFORM access.require_credential_admin(p_actor, NULL);
    END IF;
    v_id := access.onboard_tenant(p_actor, p_canonical_name, p_short_code);
    IF p_internal THEN
        UPDATE cloudops.tenant SET internal = true WHERE id = v_id;
        PERFORM access.log_change(p_actor::text, 'set_tenant_internal', jsonb_build_object(
            'tenant_id', v_id, 'internal', true));
    END IF;
    RETURN v_id;
END $$;

CREATE FUNCTION access.set_tenant_internal(p_actor uuid, p_tenant uuid, p_internal boolean)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_credential_admin(p_actor, p_tenant);
    IF p_internal IS NULL THEN
        RAISE EXCEPTION 'internal or not' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT p_internal AND EXISTS (SELECT 1 FROM cloudops.tenant t
                                  WHERE t.id = p_tenant AND t.hosts_platform) THEN
        RAISE EXCEPTION 'the platform host is internal; move the host first'
            USING ERRCODE = 'TKHST';
    END IF;
    UPDATE cloudops.tenant SET internal = p_internal WHERE id = p_tenant;
    PERFORM access.log_change(p_actor::text, 'set_tenant_internal', jsonb_build_object(
        'tenant_id', p_tenant, 'internal', p_internal));
END $$;

-- One host at most: the old one gives the flag up in the same statement order,
-- under a lock, so two administrators cannot both win.
CREATE FUNCTION access.set_platform_host(p_actor uuid, p_tenant uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_credential_admin(p_actor, p_tenant);
    PERFORM pg_advisory_xact_lock(hashtext('cloudops.tenant:hosts_platform'));
    IF p_tenant IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM cloudops.tenant t WHERE t.id = p_tenant AND t.internal) THEN
        RAISE EXCEPTION 'only an internal tenant can host the platform'
            USING ERRCODE = 'TKHST';
    END IF;
    UPDATE cloudops.tenant SET hosts_platform = false
     WHERE hosts_platform AND id IS DISTINCT FROM p_tenant;
    IF p_tenant IS NOT NULL THEN
        UPDATE cloudops.tenant SET hosts_platform = true WHERE id = p_tenant;
    END IF;
    PERFORM access.log_change(p_actor::text, 'set_platform_host', jsonb_build_object(
        'tenant_id', p_tenant));
END $$;
"""

# The resolver's functions (0023): the platform's host, by id only.
RESOLVER = """
CREATE FUNCTION cloudops.platform_tenant()
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT t.id FROM cloudops.tenant t WHERE t.hosts_platform
$$;
-- 0023's name, for the previous release's pods until 0030+1 drops it.
CREATE OR REPLACE FUNCTION cloudops.internal_tenant()
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT t.id FROM cloudops.tenant t WHERE t.hosts_platform
$$;
REVOKE ALL ON FUNCTION cloudops.platform_tenant() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.platform_tenant() TO cloudops_app, cloudops_ingest;
"""


def _as_owners() -> str:
    """The access writers belong to cloudops_access_admin and the tenant reader to
    cloudops_resolver; the migrator acts as each by SET ROLE, as 0011 and 0016 do."""
    return "\n".join([
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
        "SET LOCAL ROLE cloudops_access_admin;",
        FUNCTIONS,
        *[f"REVOKE ALL ON FUNCTION access.{fn} FROM PUBLIC;" for fn in WRITERS],
        *[f"GRANT EXECUTE ON FUNCTION access.{fn} TO cloudops_app;" for fn in WRITERS],
        "RESET ROLE;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;",
        "GRANT CREATE ON SCHEMA cloudops TO cloudops_resolver;",
        "SET LOCAL ROLE cloudops_resolver;",
        RESOLVER,
        "RESET ROLE;",
        "REVOKE CREATE ON SCHEMA cloudops FROM cloudops_resolver;",
    ])


CONCURRENT_INDEXES = (
    # At most one tenant hosts the platform.
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS tenant_one_platform_host_idx"
    " ON cloudops.tenant ((true)) WHERE hosts_platform",
)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(FOLD)
    op.execute(_as_owners())
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
