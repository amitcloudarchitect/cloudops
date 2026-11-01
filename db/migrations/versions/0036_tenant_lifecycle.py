"""Tenant lifecycle: edit, offboard, restore and purge a customer (expand).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-01

ADR 0038 (user decision, 2026-11-01): deleting a customer offboards it at once,
and a platform administrator purges it after a retention period, keeping the audit
log. Customer names are editable.

- **Edit.** ``access.update_tenant(actor, tenant, name, code)``: the customer
  master's permission (``mapping.manage``; an internal tenant also needs
  ``access.manage``, as onboarding it does), the tenant in scope. The short code
  names a stored credential's secrets, schedules and task queue (ADR 0027), so it
  is refused while the tenant holds a credential that is not revoked. Aliases and
  mappings are keyed by tenant id and do not change.
- **Offboard** (the console's Delete). ``access.offboard_tenant(actor, tenant,
  confirm_name, reason)``: a platform administrator (role class ``platform_admin``
  holding ``access.manage``), the tenant in scope, its exact name typed, a reason.
  In one transaction the tenant becomes ``offboarded``, its stored credentials are
  revoked and its portal, if live, is suspended. ``purge_after`` is now plus the
  retention (``access.lifecycle_policy``, 30 days).
- **An offboarded tenant is in no scope.** Every reader that lists tenants leaves it
  out: ``resolve_principal_v2`` (all-tenant and named principals alike; a customer
  principal of it resolves to nothing), ``resolve_tenant_alias`` (its names go to
  quarantine), ``finops_tenants``, ``knowledge_tenants`` and ``pending_drafts``.
  Row-level security does the rest: every table is filtered by scope.
- **Restore** within the retention: ``access.restore_tenant(actor, tenant)``. The
  status it had comes back; its credentials stay revoked and its portal suspended.
- **Purge** after the retention: ``access.request_tenant_purge(actor, tenant,
  confirm_name, confirm_code)`` records a request in ``audit.tenant_purge`` and
  marks the tenant ``purging``; restore is no longer possible. The API starts
  nothing (ADR 0015's pattern). The worker claims it
  (``cloudops.claim_tenant_purge``), deletes the tenant's credential bronze pages
  and telemetry, then calls ``cloudops.purge_tenant``, which deletes the tenant's
  rows from every table with a ``tenant_id`` column, clears every other column
  that references the tenant, deletes customer principals that held only it, and
  marks the tenant ``purged``. Kept: ``audit.action_log`` (LLD: never dropped),
  ``audit.tenant_purge`` and the tenant row itself, the tombstone, which the audit
  log's foreign key needs.
- **Append-only tables yield to a purge only.** Their delete triggers allow a
  delete when ``cloudops.tenant_purging(tenant)``: the tenant is ``purging`` and
  this transaction is the purge (a transaction-local marker set by
  ``purge_tenant``). Updates stay refused.
- **The lifecycle columns move only through these functions.** A trigger refuses
  any other writer (the ingest role keeps its seed-era UPDATE on the tenant), and
  freezes an offboarded or purged row.
- A purged tenant's name and short code may be used again: uniqueness is now among
  tenants not purged.

Owners: the in-scope writers (``update_tenant``, ``offboard_tenant``) belong to
``cloudops_access_admin``, as every access writer does. Restore, the purge request
and the offboarded list reach tenants outside every scope, so they belong to
``cloudops_resolver``, the one role that reads every tenant (0004), with one more
policy: it may update a tenant that is offboarded or being purged. The purge itself
belongs to the tables' owner, which alone may delete from all of them.
"""

from __future__ import annotations

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

RETENTION_DAYS = 30
OUT = "('offboarded', 'purging', 'purged')"  # not in any scope
LIFECYCLE_COLUMNS = ("status", "offboarded_from", "offboarded_at", "offboarded_by",
                     "offboard_reason", "purge_after")

DDL = f"""
ALTER TABLE cloudops.tenant
    ADD COLUMN offboarded_from text,
    ADD COLUMN offboarded_at   timestamptz,
    ADD COLUMN offboarded_by   uuid,
    ADD COLUMN offboard_reason text,
    ADD COLUMN purge_after     timestamptz,
    ADD COLUMN purged_at       timestamptz;
ALTER TABLE cloudops.tenant DROP CONSTRAINT tenant_status_check;
ALTER TABLE cloudops.tenant ADD CONSTRAINT tenant_status_check CHECK (status IN
    ('prospect', 'transitioning', 'active', 'offboarding', 'exited',
     'offboarded', 'purging', 'purged')) NOT VALID;
ALTER TABLE cloudops.tenant VALIDATE CONSTRAINT tenant_status_check;
ALTER TABLE cloudops.tenant ADD CONSTRAINT tenant_offboarded_complete CHECK (
    (status IN {OUT}) = (offboarded_at IS NOT NULL AND offboarded_from IS NOT NULL
                         AND offboarded_by IS NOT NULL AND offboard_reason IS NOT NULL
                         AND purge_after IS NOT NULL)) NOT VALID;
ALTER TABLE cloudops.tenant VALIDATE CONSTRAINT tenant_offboarded_complete;
ALTER TABLE cloudops.tenant ADD CONSTRAINT tenant_purged_at CHECK (
    (status = 'purged') = (purged_at IS NOT NULL)) NOT VALID;
ALTER TABLE cloudops.tenant VALIDATE CONSTRAINT tenant_purged_at;
-- The platform's host is never offboarded (the function refuses it first).
ALTER TABLE cloudops.tenant ADD CONSTRAINT tenant_host_not_offboarded CHECK (
    NOT (hosts_platform AND status IN {OUT})) NOT VALID;
ALTER TABLE cloudops.tenant VALIDATE CONSTRAINT tenant_host_not_offboarded;

-- How long an offboarded tenant is kept before it may be purged. One row; changed
-- by a migration, never by the application.
CREATE TABLE access.lifecycle_policy (
    singleton      boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    retention_days integer NOT NULL DEFAULT {RETENTION_DAYS}
                   CHECK (retention_days BETWEEN 1 AND 3650)
);
INSERT INTO access.lifecycle_policy DEFAULT VALUES;
GRANT SELECT ON access.lifecycle_policy TO cloudops_access_admin, cloudops_resolver;

-- A purge: the request a platform administrator made, the worker's claim, and
-- what it removed. Kept with the audit log, never deleted. Tenant data like any
-- other (FORCE, tenant_isolation): once the tenant is out of every scope only its
-- functions read it.
CREATE TABLE audit.tenant_purge (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES cloudops.tenant(id),
    requested_by  uuid NOT NULL,
    requested_at  timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'requested'
                  CHECK (status IN ('requested', 'running', 'succeeded', 'failed')),
    claimed_at    timestamptz,
    finished_at   timestamptz,
    outcome       text CHECK (outcome IS NULL OR outcome ~ '^[a-z_]{{1,64}}$'),
    row_counts    jsonb,
    blobs_deleted integer CHECK (blobs_deleted >= 0),
    telemetry     text CHECK (telemetry IN ('deleted', 'not_configured')),
    CHECK ((status IN ('requested', 'running')) = (finished_at IS NULL)),
    CHECK (status <> 'succeeded' OR row_counts IS NOT NULL),
    CHECK (status <> 'failed' OR outcome IS NOT NULL)
);
ALTER TABLE audit.tenant_purge ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.tenant_purge FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON audit.tenant_purge
    USING (tenant_id = ANY(cloudops.current_tenant_ids()))
    WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY purge_registry ON audit.tenant_purge FOR SELECT
    TO cloudops_resolver USING (true);
CREATE POLICY purge_request ON audit.tenant_purge FOR INSERT
    TO cloudops_resolver WITH CHECK (status = 'requested');
CREATE POLICY purge_progress ON audit.tenant_purge FOR UPDATE
    TO cloudops_resolver USING (status IN ('requested', 'running'))
    WITH CHECK (status IN ('running', 'failed'));
GRANT USAGE ON SCHEMA audit TO cloudops_resolver;
GRANT SELECT, INSERT, UPDATE (status, claimed_at, finished_at, outcome)
    ON audit.tenant_purge TO cloudops_resolver;

CREATE FUNCTION audit.check_purge_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.requested_by, NEW.requested_at)
       IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.requested_by, OLD.requested_at)
       OR NOT (OLD.status = 'requested' AND NEW.status = 'running'
               OR OLD.status = 'running' AND NEW.status IN ('running', 'succeeded', 'failed'))
    THEN
        RAISE EXCEPTION 'a purge only moves forward' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tenant_purge_forward_only BEFORE UPDATE ON audit.tenant_purge
    FOR EACH ROW EXECUTE FUNCTION audit.check_purge_transition();
CREATE TRIGGER tenant_purge_no_delete BEFORE DELETE ON audit.tenant_purge
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
CREATE TRIGGER tenant_purge_no_truncate BEFORE TRUNCATE ON audit.tenant_purge
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();

-- Who may change the lifecycle, and nothing changes a tenant that has left.
CREATE FUNCTION cloudops.guard_tenant_lifecycle() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_owner name := (SELECT r.rolname FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner
                     WHERE c.oid = 'cloudops.tenant'::regclass);
    -- The lifecycle functions' owners, the tables' owner (the purge, migrations) and a
    -- superuser (the server's administrator).
    v_writer boolean := current_user IN ('cloudops_access_admin', 'cloudops_resolver')
                        OR current_user = v_owner
                        OR (SELECT r.rolsuper FROM pg_roles r WHERE r.rolname = current_user);
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status IN {OUT} OR NEW.offboarded_at IS NOT NULL OR NEW.purge_after IS NOT NULL
           OR NEW.purged_at IS NOT NULL THEN
            RAISE EXCEPTION 'a tenant starts live' USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'purged' AND current_user <> v_owner THEN
        RAISE EXCEPTION 'a purged tenant is a tombstone' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF ((NEW.status, NEW.offboarded_from, NEW.offboarded_at, NEW.offboarded_by,
         NEW.offboard_reason, NEW.purge_after, NEW.purged_at)
        IS DISTINCT FROM (OLD.status, OLD.offboarded_from, OLD.offboarded_at,
                          OLD.offboarded_by, OLD.offboard_reason, OLD.purge_after,
                          OLD.purged_at)
        AND (NEW.status IN {OUT} OR OLD.status IN {OUT} OR NEW.offboarded_at IS NOT NULL
             OR NEW.purged_at IS NOT NULL)
        OR OLD.status IN {OUT}
           AND (NEW.canonical_name, NEW.short_code, NEW.internal, NEW.hosts_platform)
               IS DISTINCT FROM (OLD.canonical_name, OLD.short_code, OLD.internal,
                                 OLD.hosts_platform))
       AND NOT v_writer THEN
        RAISE EXCEPTION 'a tenant''s lifecycle changes only through its functions'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tenant_lifecycle_guard BEFORE INSERT OR UPDATE ON cloudops.tenant
    FOR EACH ROW EXECUTE FUNCTION cloudops.guard_tenant_lifecycle();

-- The append-only tables' delete triggers (0009, 0013, 0025) yield to a purge, and
-- only to one: the tenant is being purged and this transaction is the purge.
CREATE FUNCTION cloudops.reject_unless_purge() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' AND cloudops.tenant_purging(OLD.tenant_id) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE OR REPLACE FUNCTION cloudops.refuse_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND cloudops.tenant_purging(OLD.tenant_id) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE OR REPLACE FUNCTION cloudops.check_narrative_fixed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND cloudops.tenant_purging(OLD.tenant_id) THEN
        RETURN OLD;
    END IF;
    IF EXISTS (SELECT 1 FROM cloudops.report_pack p
               WHERE p.id = OLD.pack_id AND p.status <> 'draft')
       AND (TG_OP = 'DELETE'
            OR (NEW.text, NEW.edited_by, NEW.edited_at, NEW.section, NEW.pack_id,
                NEW.tenant_id)
               IS DISTINCT FROM (OLD.text, OLD.edited_by, OLD.edited_at, OLD.section,
                                 OLD.pack_id, OLD.tenant_id)) THEN
        RAISE EXCEPTION 'a released pack''s narrative is fixed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
"""

# Replaced in place (CREATE OR REPLACE TRIGGER): updates stay refused, deletes
# yield to a purge. The function is created by RESOLVER below, so these run after it.
APPEND_ONLY = """
CREATE OR REPLACE TRIGGER alias_proposal_append_only BEFORE UPDATE ON cloudops.alias_proposal
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
CREATE TRIGGER alias_proposal_no_delete BEFORE DELETE ON cloudops.alias_proposal
    FOR EACH ROW EXECUTE FUNCTION cloudops.reject_unless_purge();
CREATE OR REPLACE TRIGGER alias_decision_append_only BEFORE UPDATE ON cloudops.alias_decision
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
CREATE TRIGGER alias_decision_no_delete BEFORE DELETE ON cloudops.alias_decision
    FOR EACH ROW EXECUTE FUNCTION cloudops.reject_unless_purge();
CREATE OR REPLACE TRIGGER alias_reprocess_no_delete BEFORE DELETE ON cloudops.alias_reprocess
    FOR EACH ROW EXECUTE FUNCTION cloudops.reject_unless_purge();
CREATE OR REPLACE TRIGGER credential_check_no_delete BEFORE DELETE ON cloudops.credential_check
    FOR EACH ROW EXECUTE FUNCTION cloudops.reject_unless_purge();
CREATE OR REPLACE TRIGGER customer_credential_no_delete
    BEFORE DELETE ON cloudops.customer_credential
    FOR EACH ROW EXECUTE FUNCTION cloudops.reject_unless_purge();
"""

# Grants for the writers. The access functions' owner changes the name, the code
# and the lifecycle of a tenant in the caller's scope (tenant_isolation); the
# resolver reads every tenant already (tenant_registry_read) and may now update one
# that has left every scope.
GRANTS = f"""
GRANT SELECT (offboarded_from, offboarded_at, offboarded_by, offboard_reason, purge_after,
              purged_at),
      UPDATE (canonical_name, short_code, {", ".join(LIFECYCLE_COLUMNS)})
    ON cloudops.tenant TO cloudops_access_admin;
GRANT SELECT (canonical_name, short_code, status, created_at, offboarded_from, offboarded_at,
              offboarded_by, offboard_reason, purge_after, purged_at),
      UPDATE ({", ".join(LIFECYCLE_COLUMNS)})
    ON cloudops.tenant TO cloudops_resolver;
CREATE POLICY tenant_lifecycle ON cloudops.tenant FOR UPDATE TO cloudops_resolver
    USING (status IN ('offboarded', 'purging')) WITH CHECK (status <> 'purged');
"""

LIVE = f"t.status NOT IN {OUT}"
# 0013's claimable drafts: requested, or claimed half an hour ago and abandoned.
CLAIMABLE = ("(draft_state = 'requested' OR (draft_state = 'drafting'"
             " AND draft_claimed_at < now() - interval '30 minutes'))")

# 0011's resolver and gate, now leaving out every tenant that has left. A customer
# principal of one resolves to nothing; staff simply lose it from their scope.
RESOLVER = f"""
CREATE OR REPLACE FUNCTION access.resolve_principal_v2(p_oid uuid)
RETURNS TABLE (kind text, role_name text, role_class text, permissions text[],
               all_tenants boolean, tenant_ids uuid[], account_ids uuid[],
               is_tenant_admin boolean, landing text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT p.kind, r.name, r.role_class,
           coalesce((SELECT array_agg(rp.permission ORDER BY rp.permission)
                     FROM access.role_permission rp WHERE rp.role_id = r.id), '{{}}'),
           p.all_tenants,
           CASE WHEN p.all_tenants
                THEN coalesce((SELECT array_agg(t.id ORDER BY t.id) FROM cloudops.tenant t
                               WHERE {LIVE}), '{{}}')
                ELSE coalesce((SELECT array_agg(pt.tenant_id ORDER BY pt.tenant_id)
                               FROM access.principal_tenant pt
                               JOIN cloudops.tenant t ON t.id = pt.tenant_id
                               WHERE pt.oid = p.oid AND {LIVE}), '{{}}')
           END,
           (SELECT array_agg(pa.account_id ORDER BY pa.account_id)
            FROM access.principal_account pa
            JOIN cloudops.tenant t ON t.id = pa.tenant_id
            WHERE pa.oid = p.oid AND {LIVE}),
           p.is_tenant_admin,
           r.landing
    FROM access.principal p
    JOIN access.role_definition r ON r.id = p.role_id
    WHERE p.oid = p_oid AND p.disabled_at IS NULL
      AND (p.kind <> 'customer' OR NOT EXISTS (
          SELECT 1 FROM access.principal_tenant pt
          WHERE pt.oid = p.oid AND NOT EXISTS (
              SELECT 1 FROM cloudops.portal_onboarding o
              JOIN cloudops.portal_role_agreement g
                ON g.tenant_id = o.tenant_id AND g.role_id = p.role_id
              JOIN cloudops.tenant t ON t.id = o.tenant_id
              WHERE o.tenant_id = pt.tenant_id AND o.status = 'live' AND {LIVE})))
$$;

-- 0005: an alias of a tenant that has left resolves to nothing, so its records go
-- to quarantine, counted, never dropped (CLAUDE.md rule 6).
CREATE OR REPLACE FUNCTION cloudops.resolve_tenant_alias(p_source text, p_alias text)
RETURNS TABLE (tenant_id uuid, outcome text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_norm text := cloudops.normalise_company(p_alias);
    v_ids uuid[];
BEGIN
    SELECT array_agg(DISTINCT a.tenant_id) INTO v_ids
    FROM cloudops.tenant_alias a JOIN cloudops.tenant t ON t.id = a.tenant_id
    WHERE a.source_system = p_source AND a.alias = p_alias AND {LIVE};
    IF cardinality(v_ids) = 1 THEN
        RETURN QUERY SELECT v_ids[1], 'exact'::text;
        RETURN;
    END IF;
    IF v_norm <> '' THEN
        SELECT array_agg(DISTINCT a.tenant_id) INTO v_ids
        FROM cloudops.tenant_alias a JOIN cloudops.tenant t ON t.id = a.tenant_id
        WHERE cloudops.normalise_company(a.alias) = v_norm AND {LIVE};
        IF cardinality(v_ids) = 1 THEN
            RETURN QUERY SELECT v_ids[1], 'normalised'::text;
            RETURN;
        END IF;
        IF cardinality(v_ids) > 1 THEN
            RETURN QUERY SELECT NULL::uuid, 'ambiguous'::text;
            RETURN;
        END IF;
    END IF;
    RETURN QUERY SELECT NULL::uuid, 'unmatched'::text;
END $$;

CREATE OR REPLACE FUNCTION cloudops.finops_tenants() RETURNS uuid[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT coalesce(array_agg(t.id ORDER BY t.id), '{{}}') FROM cloudops.tenant t WHERE {LIVE}
$$;

CREATE OR REPLACE FUNCTION cloudops.knowledge_tenants()
RETURNS TABLE (tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT t.id FROM cloudops.tenant t WHERE {LIVE} ORDER BY t.id
$$;

CREATE OR REPLACE FUNCTION cloudops.pending_drafts(p_limit integer)
RETURNS TABLE (kind text, id uuid, tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT q.kind, q.id, q.tenant_id FROM (
        SELECT 'control_response'::text AS kind, r.id, r.tenant_id
        FROM cloudops.control_response r WHERE {CLAIMABLE}
        UNION ALL
        SELECT 'report_narrative'::text, n.id, n.tenant_id
        FROM cloudops.report_narrative n WHERE {CLAIMABLE}) q
    JOIN cloudops.tenant t ON t.id = q.tenant_id
    WHERE {LIVE}
    ORDER BY q.kind, q.id
    LIMIT least(greatest(p_limit, 1), 500)
$$;

-- Whether this transaction is the purge of this tenant: the append-only tables'
-- delete triggers ask. A marker anyone could set means nothing on its own: the
-- tenant must also be purging, which only a platform administrator's confirmed
-- request after the retention makes it.
CREATE FUNCTION cloudops.tenant_purging(p_tenant uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT coalesce(current_setting('cloudops.purge_tenant', true), '') = p_tenant::text
       AND EXISTS (SELECT 1 FROM cloudops.tenant t
                   WHERE t.id = p_tenant AND t.status = 'purging')
$$;

-- The tenants that have left, for a platform administrator assigned them (or every
-- tenant): with the latest purge. Reaches outside every scope, so the caller is
-- checked here, never by RLS.
CREATE FUNCTION access.offboarded_tenants(p_actor uuid)
RETURNS TABLE (tenant_id uuid, canonical_name text, short_code text, internal boolean,
               status text, offboarded_at timestamptz, offboarded_by uuid,
               offboard_reason text, purge_after timestamptz, purged_at timestamptz,
               purge_id uuid, purge_status text, purge_requested_at timestamptz,
               purge_finished_at timestamptz, purge_outcome text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    PERFORM access.require_platform_admin(p_actor, NULL);
    RETURN QUERY
    SELECT t.id, t.canonical_name, t.short_code, t.internal, t.status, t.offboarded_at,
           t.offboarded_by, t.offboard_reason, t.purge_after, t.purged_at,
           p.id, p.status, p.requested_at, p.finished_at, p.outcome
    FROM cloudops.tenant t
    LEFT JOIN LATERAL (SELECT * FROM audit.tenant_purge x WHERE x.tenant_id = t.id
                       ORDER BY x.requested_at DESC LIMIT 1) p ON true
    WHERE t.status IN {OUT}
      AND EXISTS (SELECT 1 FROM access.principal a WHERE a.oid = p_actor
                    AND (a.all_tenants OR EXISTS (
                        SELECT 1 FROM access.principal_tenant pt
                        WHERE pt.oid = a.oid AND pt.tenant_id = t.id)))
    ORDER BY t.offboarded_at DESC, t.id;
END $$;

-- The tenant, locked, if this platform administrator may reach it. Outside every
-- scope by now, so assignment is read from the access tables alone. Unknown and
-- unassigned read the same.
CREATE FUNCTION access.lifecycle_row(
    p_actor uuid, p_tenant uuid, OUT o_status text, OUT o_name text, OUT o_code text,
    OUT o_from text, OUT o_offboarded_at timestamptz, OUT o_purge_after timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_platform_admin(p_actor, p_tenant);
    -- FOR UPDATE reads through tenant_lifecycle: a tenant still live is not found.
    SELECT t.status, t.canonical_name, t.short_code, t.offboarded_from, t.offboarded_at,
           t.purge_after
      INTO o_status, o_name, o_code, o_from, o_offboarded_at, o_purge_after
      FROM cloudops.tenant t WHERE t.id = p_tenant FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tenant not found' USING ERRCODE = 'CMSCP';
    END IF;
END $$;

-- Back, within the retention. Its credentials stay revoked (onboard them again) and
-- its portal suspended (a platform administrator sets it live again).
CREATE FUNCTION access.restore_tenant(p_actor uuid, p_tenant uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v record;
BEGIN
    SELECT * INTO v FROM access.lifecycle_row(p_actor, p_tenant);
    IF v.o_status <> 'offboarded' THEN
        RAISE EXCEPTION 'only an offboarded tenant is restored' USING ERRCODE = 'TKSTA';
    END IF;
    IF now() >= v.o_purge_after THEN
        RAISE EXCEPTION 'the retention has ended' USING ERRCODE = 'TKEXP';
    END IF;
    UPDATE cloudops.tenant
       SET status = v.o_from, offboarded_from = NULL, offboarded_at = NULL,
           offboarded_by = NULL, offboard_reason = NULL, purge_after = NULL
     WHERE id = p_tenant;
    PERFORM access.log_change(p_actor::text, 'restore_tenant', jsonb_build_object(
        'tenant_id', p_tenant, 'status', v.o_from, 'offboarded_at', v.o_offboarded_at));
END $$;

-- The purge, asked for: after the retention, with the name and the short code typed
-- (two confirmations). The tenant becomes purging and can no longer be restored; the
-- worker does the rest. A failed purge may be asked for again.
CREATE FUNCTION access.request_tenant_purge(
    p_actor uuid, p_tenant uuid, p_confirm_name text, p_confirm_code text
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v record;
    v_id uuid;
BEGIN
    SELECT * INTO v FROM access.lifecycle_row(p_actor, p_tenant);
    IF v.o_status NOT IN ('offboarded', 'purging') THEN
        RAISE EXCEPTION 'only an offboarded tenant is purged' USING ERRCODE = 'TKSTA';
    END IF;
    IF now() < v.o_purge_after THEN
        RAISE EXCEPTION 'the retention has not ended' USING ERRCODE = 'TKRET';
    END IF;
    IF p_confirm_name IS DISTINCT FROM v.o_name OR p_confirm_code IS DISTINCT FROM v.o_code THEN
        RAISE EXCEPTION 'the confirmation does not match' USING ERRCODE = 'TKCNF';
    END IF;
    IF EXISTS (SELECT 1 FROM audit.tenant_purge x WHERE x.tenant_id = p_tenant
               AND x.status IN ('requested', 'running')) THEN
        RAISE EXCEPTION 'a purge is already waiting' USING ERRCODE = 'TKSTA';
    END IF;
    UPDATE cloudops.tenant SET status = 'purging' WHERE id = p_tenant;
    INSERT INTO audit.tenant_purge (tenant_id, requested_by) VALUES (p_tenant, p_actor)
    RETURNING id INTO v_id;
    PERFORM access.log_change(p_actor::text, 'request_tenant_purge', jsonb_build_object(
        'tenant_id', p_tenant, 'purge_id', v_id));
    RETURN v_id;
END $$;

-- The worker's side, under the ingest login and an empty scope: the oldest waiting
-- purge (or one whose worker died an hour ago), before the tenant is known.
CREATE FUNCTION cloudops.claim_tenant_purge()
RETURNS TABLE (id uuid, tenant_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    UPDATE audit.tenant_purge p SET status = 'running', claimed_at = now()
    WHERE p.id = (SELECT q.id FROM audit.tenant_purge q
                  WHERE q.status = 'requested'
                     OR q.status = 'running' AND q.claimed_at < now() - interval '1 hour'
                  ORDER BY q.requested_at LIMIT 1 FOR UPDATE SKIP LOCKED)
    RETURNING p.id, p.tenant_id;
END $$;

CREATE FUNCTION cloudops.fail_tenant_purge(p_purge uuid, p_outcome text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    UPDATE audit.tenant_purge SET status = 'failed', finished_at = now(), outcome = p_outcome
     WHERE id = p_purge AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no running purge' USING ERRCODE = 'no_data_found';
    END IF;
END $$;
"""

RESOLVER_FUNCTIONS = (
    "access.offboarded_tenants(uuid)", "access.lifecycle_row(uuid, uuid)",
    "access.restore_tenant(uuid, uuid)", "access.request_tenant_purge(uuid, uuid, text, text)",
    "cloudops.claim_tenant_purge()", "cloudops.fail_tenant_purge(uuid, text)",
    "cloudops.tenant_purging(uuid)",
)
FOR_APP = ("access.offboarded_tenants(uuid)", "access.restore_tenant(uuid, uuid)",
           "access.request_tenant_purge(uuid, uuid, text, text)")
FOR_INGEST = ("cloudops.claim_tenant_purge()", "cloudops.fail_tenant_purge(uuid, text)")

# The in-scope writers, owned by cloudops_access_admin.
ACCESS = f"""
-- A platform administrator: staff, role class platform_admin, holding access.manage.
-- A tenant named: assigned it, or every tenant. Scope is not read: offboarded
-- tenants are in none. The callers that need scope check it themselves.
CREATE FUNCTION access.require_platform_admin(p_actor uuid, p_tenant uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class = 'platform_admin' AND rp.permission = 'access.manage'
    ) THEN
        RAISE EXCEPTION 'only a platform administrator' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_tenant IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM access.principal p WHERE p.oid = p_actor
          AND (p.all_tenants OR EXISTS (SELECT 1 FROM access.principal_tenant pt
                                        WHERE pt.oid = p.oid AND pt.tenant_id = p_tenant)))
    THEN
        RAISE EXCEPTION 'tenant not found' USING ERRCODE = 'CMSCP';
    END IF;
END $$;

-- A new name, or a new short code where it names nothing outside the database:
-- a credential that is not revoked has secrets, a task queue and schedules named
-- by it (ADR 0027), so the code then stays.
CREATE FUNCTION access.update_tenant(
    p_actor uuid, p_tenant uuid, p_canonical_name text, p_short_code text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v cloudops.tenant;
    v_name text := btrim(coalesce(p_canonical_name, ''));
BEGIN
    PERFORM access.require_mapper(p_actor, p_tenant);
    SELECT * INTO v FROM cloudops.tenant t WHERE t.id = p_tenant FOR UPDATE;
    IF v.internal THEN
        PERFORM access.require_credential_admin(p_actor, p_tenant);
    END IF;
    IF length(v_name) = 0 OR length(v_name) > 200
       OR coalesce(p_short_code, '') !~ '^[A-Z0-9]{{2,10}}$' THEN
        RAISE EXCEPTION 'a tenant needs a name and a short code of 2 to 10 capitals or digits'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_short_code <> v.short_code AND EXISTS (
        SELECT 1 FROM cloudops.customer_credential c
        WHERE c.tenant_id = p_tenant AND c.status <> 'revoked') THEN
        RAISE EXCEPTION 'a live credential is named by the short code' USING ERRCODE = 'TKCOD';
    END IF;
    IF v_name = v.canonical_name AND p_short_code = v.short_code THEN
        RETURN;
    END IF;
    BEGIN
        UPDATE cloudops.tenant SET canonical_name = v_name, short_code = p_short_code
         WHERE id = p_tenant;
    EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION 'a tenant with that name or short code exists' USING ERRCODE = 'CMDUP';
    END;
    PERFORM access.log_change(p_actor::text, 'update_tenant', jsonb_build_object(
        'tenant_id', p_tenant,
        'canonical_name', jsonb_build_object('from', v.canonical_name, 'to', v_name),
        'short_code', jsonb_build_object('from', v.short_code, 'to', p_short_code)));
END $$;

-- The console's Delete. Effective at once, in this transaction: out of every scope,
-- credentials revoked, portal suspended. The exact name typed, and a reason.
CREATE FUNCTION access.offboard_tenant(
    p_actor uuid, p_tenant uuid, p_confirm_name text, p_reason text
) RETURNS timestamptz
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v cloudops.tenant;
    v_reason text := btrim(coalesce(p_reason, ''));
    v_after timestamptz;
    v_revoked int;
    v_suspended int;
BEGIN
    PERFORM access.require_platform_admin(p_actor, NULL);
    PERFORM access.require_credential_admin(p_actor, p_tenant);
    SELECT * INTO v FROM cloudops.tenant t WHERE t.id = p_tenant FOR UPDATE;
    IF v.status IN {OUT} THEN
        RAISE EXCEPTION 'already offboarded' USING ERRCODE = 'TKSTA';
    END IF;
    IF v.hosts_platform THEN
        RAISE EXCEPTION 'the platform host stays; move the host first' USING ERRCODE = 'TKHST';
    END IF;
    IF p_confirm_name IS DISTINCT FROM v.canonical_name THEN
        RAISE EXCEPTION 'the confirmation does not match' USING ERRCODE = 'TKCNF';
    END IF;
    IF length(v_reason) = 0 OR length(v_reason) > 1000 THEN
        RAISE EXCEPTION 'a reason of 1 to 1000 characters' USING ERRCODE = 'check_violation';
    END IF;
    v_after := now() + make_interval(days => (SELECT retention_days FROM access.lifecycle_policy));
    UPDATE cloudops.tenant
       SET offboarded_from = status, status = 'offboarded', offboarded_at = now(),
           offboarded_by = p_actor, offboard_reason = v_reason, purge_after = v_after
     WHERE id = p_tenant;
    UPDATE cloudops.customer_credential
       SET status = 'revoked', revoked_at = now(), revoked_by = p_actor
     WHERE tenant_id = p_tenant AND status <> 'revoked';
    GET DIAGNOSTICS v_revoked = ROW_COUNT;
    UPDATE cloudops.portal_onboarding
       SET status = 'suspended', suspended_at = now(), suspended_by = p_actor, updated_at = now()
     WHERE tenant_id = p_tenant AND status = 'live';
    GET DIAGNOSTICS v_suspended = ROW_COUNT;
    PERFORM access.log_change(p_actor::text, 'offboard_tenant', jsonb_build_object(
        'tenant_id', p_tenant, 'reason', v_reason, 'purge_after', v_after,
        'credentials_revoked', v_revoked, 'portal_suspended', v_suspended = 1));
    RETURN v_after;
END $$;
"""

ACCESS_FUNCTIONS = ("require_platform_admin(uuid, uuid)",
                    "update_tenant(uuid, uuid, text, text)",
                    "offboard_tenant(uuid, uuid, text, text)")

# The purge, owned by the tables' owner (the migration login), the one role that may
# delete from every table. Row-level security applies to it (FORCE): it names its
# scope, the one tenant, for its own statements and puts the caller's back after.
PURGE = """
CREATE FUNCTION cloudops.purge_tenant(
    p_purge uuid, p_tenant uuid, p_blobs_deleted integer, p_telemetry text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_prev text[] := ARRAY[current_setting('app.tenant_ids', true),
                           current_setting('app.role', true),
                           current_setting('app.account_ids', true),
                           current_setting('cloudops.purge_tenant', true)];
    v_tables regclass[];
    v_left regclass[];
    v_table regclass;
    v_ref record;
    v_n bigint;
    v_counts jsonb := '{}';
    v_customers uuid[];
    v_accounts uuid[];
BEGIN
    PERFORM set_config('app.tenant_ids', p_tenant::text, true);
    PERFORM set_config('app.role', 'platform_admin', true);
    PERFORM set_config('app.account_ids', 'all', true);
    PERFORM set_config('cloudops.purge_tenant', p_tenant::text, true);
    PERFORM 1 FROM audit.tenant_purge p
     WHERE p.id = p_purge AND p.tenant_id = p_tenant AND p.status = 'running' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no running purge for this tenant' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM 1 FROM cloudops.tenant t
     WHERE t.id = p_tenant AND t.status = 'purging' AND t.purge_after <= now() FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'the tenant is not due for purge' USING ERRCODE = 'TKSTA';
    END IF;

    -- Customer principals that held this tenant and no other.
    SELECT array_agg(p.oid) INTO v_customers FROM access.principal p
     WHERE p.kind = 'customer'
       AND EXISTS (SELECT 1 FROM access.principal_tenant pt
                   WHERE pt.oid = p.oid AND pt.tenant_id = p_tenant)
       AND NOT EXISTS (SELECT 1 FROM access.principal_tenant pt
                       WHERE pt.oid = p.oid AND pt.tenant_id <> p_tenant);

    SELECT coalesce(array_agg(a.id), '{}') INTO v_accounts
      FROM cloudops.cloud_account a WHERE a.tenant_id = p_tenant;

    -- Every table with a tenant_id, but the audit log and the purge's own record.
    SELECT array_agg(c.oid::regclass ORDER BY n.nspname, c.relname) INTO v_tables
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
    WHERE n.nspname IN ('cloudops', 'staging', 'audit', 'access')
      AND c.relkind IN ('r', 'p') AND NOT c.relispartition
      AND c.oid NOT IN ('audit.action_log'::regclass, 'audit.tenant_purge'::regclass);
    -- Dependents first: a table another still references fails, and is tried again
    -- on the next pass. A pass that removes nothing new means a reference this
    -- function cannot clear: the purge fails and changes nothing.
    LOOP
        v_left := '{}';
        FOREACH v_table IN ARRAY v_tables LOOP
            BEGIN
                EXECUTE format('DELETE FROM %s WHERE tenant_id = $1', v_table) USING p_tenant;
                GET DIAGNOSTICS v_n = ROW_COUNT;
                v_counts := v_counts || jsonb_build_object(v_table::text,
                    coalesce((v_counts ->> v_table::text)::bigint, 0) + v_n);
            EXCEPTION WHEN foreign_key_violation THEN
                v_left := v_left || v_table;
            END;
        END LOOP;
        EXIT WHEN cardinality(v_left) = 0;
        IF cardinality(v_left) = cardinality(v_tables) THEN
            RAISE EXCEPTION 'tables still referenced: %', v_left
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        v_tables := v_left;
    END LOOP;

    -- Every other column that references the tenant is cleared (a quarantined name's
    -- suggestion, for one).
    FOR v_ref IN
        SELECT c.conrelid::regclass AS tbl, a.attname AS col
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        JOIN pg_class r ON r.oid = c.conrelid
        WHERE c.contype = 'f' AND c.confrelid = 'cloudops.tenant'::regclass
          AND cardinality(c.conkey) = 1 AND a.attname <> 'tenant_id' AND NOT r.relispartition
    LOOP
        EXECUTE format('UPDATE %s SET %I = NULL WHERE %I = $1', v_ref.tbl, v_ref.col, v_ref.col)
            USING p_tenant;
        GET DIAGNOSTICS v_n = ROW_COUNT;
        v_counts := v_counts || jsonb_build_object(v_ref.tbl::text || '.' || v_ref.col, v_n);
    END LOOP;

    -- A CUR assembly in progress lists the tenants and accounts it has loaded.
    UPDATE staging.cur_assembly
       SET tenant_ids = array_remove(tenant_ids, p_tenant),
           account_ids = ARRAY(SELECT x FROM unnest(account_ids) x WHERE x <> ALL(v_accounts))
     WHERE p_tenant = ANY(tenant_ids) OR account_ids && v_accounts;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('staging.cur_assembly.tenant_ids', v_n);

    DELETE FROM access.principal p WHERE p.oid = ANY(coalesce(v_customers, '{}'));
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('access.principal', v_n);

    -- The tombstone: the row the audit log points at, with what it was and when it went.
    UPDATE cloudops.tenant
       SET status = 'purged', purged_at = now(), contract_start = NULL, contract_end = NULL,
           assistant_enabled = false
     WHERE id = p_tenant;
    UPDATE audit.tenant_purge
       SET status = 'succeeded', finished_at = now(), row_counts = v_counts,
           blobs_deleted = p_blobs_deleted, telemetry = p_telemetry
     WHERE id = p_purge;

    PERFORM set_config('app.tenant_ids', coalesce(v_prev[1], ''), true);
    PERFORM set_config('app.role', coalesce(v_prev[2], ''), true);
    PERFORM set_config('app.account_ids', coalesce(v_prev[3], ''), true);
    PERFORM set_config('cloudops.purge_tenant', coalesce(v_prev[4], ''), true);
    RETURN v_counts;
END $$;
REVOKE ALL ON FUNCTION cloudops.purge_tenant(uuid, uuid, integer, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.purge_tenant(uuid, uuid, integer, text) TO cloudops_ingest;
"""


def _as_owners() -> str:
    """As 0029 does: the migrator acts as each owner by SET ROLE, with CREATE on the
    schema for the moment it needs it."""
    return "\n".join([
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
        "SET LOCAL ROLE cloudops_access_admin;",
        ACCESS,
        *[f"REVOKE ALL ON FUNCTION access.{fn} FROM PUBLIC;" for fn in ACCESS_FUNCTIONS],
        *[f"GRANT EXECUTE ON FUNCTION access.{fn} TO cloudops_app;" for fn in ACCESS_FUNCTIONS],
        "GRANT EXECUTE ON FUNCTION access.require_platform_admin(uuid, uuid),"
        " access.log_change(text, text, jsonb) TO cloudops_resolver;",
        "RESET ROLE;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;",
        "GRANT CREATE ON SCHEMA access, cloudops TO cloudops_resolver;",
        "SET LOCAL ROLE cloudops_resolver;",
        RESOLVER,
        *[f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC;" for fn in RESOLVER_FUNCTIONS
          if fn != "cloudops.tenant_purging(uuid)"],
        *[f"GRANT EXECUTE ON FUNCTION {fn} TO cloudops_app;" for fn in FOR_APP],
        *[f"GRANT EXECUTE ON FUNCTION {fn} TO cloudops_ingest;" for fn in FOR_INGEST],
        "RESET ROLE;",
        "REVOKE CREATE ON SCHEMA access, cloudops FROM cloudops_resolver;",
    ])


CONCURRENT_INDEXES = (
    # One waiting or running purge per tenant.
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS tenant_purge_active_idx"
    " ON audit.tenant_purge (tenant_id) WHERE status IN ('requested', 'running')",
    # A purged tenant's name and code may be used again.
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS tenant_canonical_name_live_idx"
    " ON cloudops.tenant (canonical_name) WHERE status <> 'purged'",
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS tenant_short_code_live_idx"
    " ON cloudops.tenant (short_code) WHERE status <> 'purged'",
)
# Loosening, once the partial indexes stand in for them.
AFTER_INDEXES = """
ALTER TABLE cloudops.tenant DROP CONSTRAINT IF EXISTS tenant_canonical_name_key;
ALTER TABLE cloudops.tenant DROP CONSTRAINT IF EXISTS tenant_short_code_key;
"""


def upgrade() -> None:
    op.execute(DDL)
    op.execute(GRANTS)
    op.execute(_as_owners())
    op.execute(APPEND_ONLY)
    op.execute(PURGE)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)
    op.execute(AFTER_INDEXES)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
