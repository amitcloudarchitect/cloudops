"""Customer portal: the customer database role, customer roles, onboarding and
tenant administrators (expand).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-31

Step 18, HLD M2 and M22; ADR 0018.

**The data boundary is the database's (ADR 0018, D1).** Row-level security
decides which rows a scope sees, but it cannot hide a column, and customers may
read a ticket without its effort hours or its Getronics assignee. So a customer
scope runs as its own database role. ``packages/tenancy`` issues
``SET LOCAL ROLE cloudops_customer`` in every transaction whose role class is
``customer``, and that role holds:

- SELECT on the columns a customer may see, listed below table by table. A
  query naming any other column fails with ``permission denied``;
- nothing on commercial, internal or ingest tables. A table added later has no
  grant until a migration gives it one, so new data fails closed;
- INSERT on ``audit.action_log``, because every access is still recorded.

The restrictive policies stay as the second lock. ``commercial_visibility``
already guards ``sow``. ``staff_only`` now guards the agents' and the AI
gateway's records (triage, agent runs, LLM calls), so a grant made by mistake
still yields no row.

Infrastructure creates ``cloudops_customer`` (NOLOGIN) and makes
``cloudops_app`` a member WITH INHERIT FALSE, SET TRUE (``db/bootstrap/roles.sql``).
The application can switch to it and never holds its rights otherwise. This
migration checks for it and stops if it is missing.

**Customer roles (D15 default matrix; ADR 0018, D4).** Six roles of class
``customer``, each with a landing page: CXO, VP or director, Architect,
Customer engineer, Security officer, Approver. A new ``approvals.read``
permission covers changes that wait on the customer. ``role_definition.landing``
holds where each role opens.

**Onboarding (M2; ADR 0018, D3).** ``portal_onboarding`` and
``portal_role_agreement`` record a tenant's federation, the role set agreed with
the customer, its first administrator and whether its portal is live. A
platform administrator (``access.manage``) performs each step through the
``access.portal_*`` functions. A customer principal resolves only while its
tenant is live **and** its role is in the agreed set. Both the existing
``resolve_principal`` and the new ``resolve_principal_v2`` enforce this, so an
API pod still running the previous release fails closed as well.

Refusals carry SQLSTATEs the API maps to codes: ``POSEQ`` a step out of order,
``POLST`` the tenant's last administrator, 42501 not permitted, P0002 not found.

**Tenant administrators (ADR 0018, D2).** Platform administrators still
maintain every assignment (ADR 0002, D2). A customer principal flagged
``is_tenant_admin`` can also add, change and disable users of its own tenant,
with roles from the agreed set only, through the ``access.tenant_*`` functions.
It can never reach staff principals, another tenant, or a subscription-limited
user's restriction, and the tenant's last administrator cannot be removed.
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

# What a customer scope may read, column by column. Everything absent is
# refused by the database: effort and the FTE model (planned/actual hours),
# Getronics assignees and workgroups, raw source payloads, internal source
# identifiers and the mapping of source company names.
CUSTOMER_COLUMNS: dict[str, tuple[str, ...]] = {
    "cloudops.tenant": (
        "id", "canonical_name", "short_code", "status", "coverage", "support",
        "data_residency", "created_at",
    ),
    "cloudops.cloud_account": (
        "id", "tenant_id", "provider", "native_id", "display_name", "environment",
        "region_default", "onboarded_at", "connection_state", "last_discovery",
    ),
    "cloudops.resource": (
        "id", "tenant_id", "account_id", "native_id", "name", "fqdn", "ip_addresses",
        "type_native", "type_canonical", "region", "operating_system", "is_critical_ci",
        "tags", "state", "first_seen", "last_seen", "deleted_at",
    ),
    "cloudops.work_item": (
        "id", "tenant_id", "kind", "number", "change_request", "opened_at", "closed_at",
        "priority", "state", "short_description", "configuration_item", "resource_id",
        "change_type", "resolve_sla", "response_sla", "made_sla", "sla_breached",
        "resolve_time_seconds", "closure_code", "close_notes", "is_workaround",
    ),
    "cloudops.vulnerability": (
        "id", "tenant_id", "qid", "asset_name", "asset_ipv4", "resource_id", "scope",
        "title", "severity", "operating_system", "solution", "threat", "detection_type",
        "first_detected", "last_detected", "remediated_at", "detection_span_days",
        "fix_days", "status",
    ),
    "cloudops.cost_fact": (
        "id", "tenant_id", "account_id", "resource_id", "usage_date", "invoice_period",
        "service_native", "service_canonical", "charge_class", "unblended_cost",
        "amortized_cost", "currency",
    ),
    "cloudops.cost_finding": (
        "id", "tenant_id", "account_id", "resource_id", "category", "title",
        "recommendation", "status", "estimated_saving", "saving_currency", "detected_at",
        "updated_at",
    ),
    "cloudops.fx_rate": (
        "id", "from_currency", "to_currency", "rate", "rate_type", "effective_date", "source",
    ),
}

# Internal commentary: the agents' and the gateway's records. Never a customer's,
# whatever is granted.
STAFF_ONLY = (
    "cloudops.triage_result",
    "cloudops.triage_review",
    "cloudops.agent_run",
    "cloudops.llm_call",
    "cloudops.llm_cache",
)
STAFF_ONLY_PREDICATE = "coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer')"

# (name, landing, permissions, description)
CUSTOMER_ROLES = (
    ("CXO", "/customers", ("tickets.read", "inventory.read", "cost.read", "vulnerabilities.read"),
     "Executive summary: service health, SLA, cloud cost and security posture"),
    ("VP or director", "/performance", ("tickets.read", "inventory.read", "cost.read"),
     "Service performance: SLA attainment, ticket volumes and cloud cost"),
    ("Architect", "/resources", ("inventory.read", "cost.read", "knowledge.read"),
     "The estate: resources, subscriptions and their cost"),
    ("Customer engineer", "/work", ("tickets.read", "inventory.read", "knowledge.read"),
     "The work queue and the resources it concerns"),
    ("Security officer", "/posture", ("vulnerabilities.read", "inventory.read"),
     "Security posture: open vulnerabilities by severity and age"),
    ("Approver", "/approvals", ("approvals.read", "inventory.read"),
     "Changes awaiting the customer's approval (read-only until execution exists)"),
)

PRECONDITION = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cloudops_customer') THEN
        RAISE EXCEPTION 'role cloudops_customer does not exist; infrastructure creates it'
            ' before migrating (db/bootstrap/roles.sql)';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cloudops_customer'
               AND (rolsuper OR rolbypassrls OR rolcanlogin)) THEN
        RAISE EXCEPTION 'role cloudops_customer must be NOLOGIN, NOSUPERUSER, NOBYPASSRLS';
    END IF;
    IF NOT pg_has_role('cloudops_app', 'cloudops_customer', 'SET') THEN
        RAISE EXCEPTION 'cloudops_app must be a member of cloudops_customer'
            ' (WITH INHERIT FALSE, SET TRUE)';
    END IF;
    IF pg_has_role('cloudops_app', 'cloudops_customer', 'USAGE') THEN
        RAISE EXCEPTION 'cloudops_app must not inherit cloudops_customer (WITH INHERIT FALSE)';
    END IF;
END $$;
"""


def _customer_grants() -> str:
    lines = [
        "GRANT USAGE ON SCHEMA cloudops, audit TO cloudops_customer;",
        "GRANT INSERT ON audit.action_log TO cloudops_customer;",
        "GRANT USAGE ON SEQUENCE audit.action_log_id_seq TO cloudops_customer;",
        "GRANT EXECUTE ON FUNCTION cloudops.current_tenant_ids(),"
        " cloudops.account_scope_is_all(), cloudops.current_account_ids(),"
        " cloudops.fx_rate_on(char, char, date, text),"
        " cloudops.to_currency(numeric, char, char, date, text) TO cloudops_customer;",
    ]
    for table, columns in CUSTOMER_COLUMNS.items():
        lines.append(f"GRANT SELECT ({', '.join(columns)}) ON {table} TO cloudops_customer;")
    for table in STAFF_ONLY:
        lines.append(
            f"CREATE POLICY staff_only ON {table} AS RESTRICTIVE USING ({STAFF_ONLY_PREDICATE});"
        )
    return "\n".join(lines)


ACCESS_DDL = """
ALTER TABLE access.role_definition ADD COLUMN landing text
    CHECK (landing ~ '^/[a-z-]*$');
ALTER TABLE access.principal ADD COLUMN is_tenant_admin boolean NOT NULL DEFAULT false;
ALTER TABLE access.principal ADD CONSTRAINT principal_tenant_admin_is_customer
    CHECK (NOT is_tenant_admin OR kind = 'customer');

INSERT INTO access.permission (name, description, is_commercial)
VALUES ('approvals.read', 'Changes awaiting the customer''s approval', false);
INSERT INTO access.role_permission (role_id, permission)
SELECT r.id, 'approvals.read' FROM access.role_definition r
WHERE r.is_system AND r.name = 'Platform administrator';
UPDATE access.role_definition SET landing = '/customers'
WHERE is_system AND name = 'Customer viewer';

-- Access management is staff-only, like mapping.manage (0009): a tenant
-- administrator manages its users through the tenant functions, never through
-- the platform-wide ones, whatever an administrator grants.
CREATE FUNCTION access.reject_customer_access_manage() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NEW.permission = 'access.manage' AND EXISTS (
        SELECT 1 FROM access.role_definition r
        WHERE r.id = NEW.role_id AND r.role_class = 'customer') THEN
        RAISE EXCEPTION 'access.manage cannot be granted to a customer role'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER role_permission_customer_access_manage
    BEFORE INSERT OR UPDATE ON access.role_permission
    FOR EACH ROW EXECUTE FUNCTION access.reject_customer_access_manage();
REVOKE ALL ON FUNCTION access.reject_customer_access_manage() FROM PUBLIC;
"""

PORTAL_DDL = """
-- One row per tenant once its portal onboarding starts (HLD M2).
CREATE TABLE cloudops.portal_onboarding (
    tenant_id              uuid PRIMARY KEY REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    status                 text NOT NULL DEFAULT 'setup'
                           CHECK (status IN ('setup', 'live', 'suspended')),
    -- Federation (Entra B2B): how the customer's users authenticate. Identifiers,
    -- never a secret.
    idp_kind               text CHECK (idp_kind IN ('entra', 'saml', 'wsfed')),
    idp_reference          text CHECK (length(btrim(idp_reference)) > 0),
    domains                text[] NOT NULL DEFAULT '{}',
    federation_recorded_at timestamptz,
    federation_recorded_by uuid,
    -- The role set agreed with the customer, and the record of that agreement.
    roles_agreed_at        timestamptz,
    roles_agreed_by        uuid,
    roles_agreement_ref    text CHECK (length(btrim(roles_agreement_ref)) > 0),
    first_admin_oid        uuid,
    first_admin_at         timestamptz,
    first_admin_by         uuid,
    live_at                timestamptz,
    live_by                uuid,
    suspended_at           timestamptz,
    suspended_by           uuid,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CHECK (status = 'setup' OR (federation_recorded_at IS NOT NULL
                                AND roles_agreed_at IS NOT NULL
                                AND first_admin_oid IS NOT NULL))
);

CREATE TABLE cloudops.portal_role_agreement (
    tenant_id  uuid NOT NULL REFERENCES cloudops.portal_onboarding(tenant_id) ON DELETE CASCADE,
    role_id    uuid NOT NULL REFERENCES access.role_definition(id) ON DELETE CASCADE,
    agreed_at  timestamptz NOT NULL DEFAULT now(),
    agreed_by  uuid NOT NULL,
    PRIMARY KEY (tenant_id, role_id)
);

ALTER TABLE cloudops.portal_onboarding ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.portal_onboarding FORCE ROW LEVEL SECURITY;
ALTER TABLE cloudops.portal_role_agreement ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.portal_role_agreement FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON cloudops.portal_onboarding
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY tenant_isolation ON cloudops.portal_role_agreement
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
-- How a customer was onboarded is Getronics' record, not the customer's.
CREATE POLICY staff_only ON cloudops.portal_onboarding AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer'));
CREATE POLICY staff_only ON cloudops.portal_role_agreement AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer'));

-- The resolver reads both before any scope exists (only in the unresolved scope,
-- which staff_only admits); the access functions write them.
CREATE POLICY resolver_read ON cloudops.portal_onboarding FOR SELECT
  TO cloudops_resolver USING (true);
CREATE POLICY resolver_read ON cloudops.portal_role_agreement FOR SELECT
  TO cloudops_resolver USING (true);
CREATE POLICY access_admin_all ON cloudops.portal_onboarding
  TO cloudops_access_admin USING (true) WITH CHECK (true);
CREATE POLICY access_admin_all ON cloudops.portal_role_agreement
  TO cloudops_access_admin USING (true) WITH CHECK (true);

GRANT SELECT ON cloudops.portal_onboarding, cloudops.portal_role_agreement TO cloudops_app;
GRANT SELECT ON cloudops.portal_onboarding, cloudops.portal_role_agreement
    TO cloudops_resolver;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON cloudops.portal_onboarding, cloudops.portal_role_agreement TO cloudops_access_admin;
"""


def _role_defaults() -> str:
    rows = []
    for name, landing, perms, description in CUSTOMER_ROLES:
        rows.append(
            "INSERT INTO access.role_definition (name, role_class, is_system, description, landing)"
            f" VALUES ('{name}', 'customer', true, '{description.replace(chr(39), chr(39) * 2)}',"
            f" '{landing}');"
        )
        rows.append(
            "INSERT INTO access.role_permission (role_id, permission)"
            " SELECT r.id, p FROM access.role_definition r,"
            f" unnest(ARRAY[{','.join(repr(p) for p in perms)}]) p WHERE r.name = '{name}';"
        )
    return "\n".join(rows)


# A customer principal resolves only while every tenant it holds is live and
# has its role in the agreed set. Expressed once, used by both resolvers.
CUSTOMER_GATE = """
    (p.kind <> 'customer' OR NOT EXISTS (
        SELECT 1 FROM access.principal_tenant pt
        WHERE pt.oid = p.oid AND NOT EXISTS (
            SELECT 1 FROM cloudops.portal_onboarding o
            JOIN cloudops.portal_role_agreement g
              ON g.tenant_id = o.tenant_id AND g.role_id = p.role_id
            WHERE o.tenant_id = pt.tenant_id AND o.status = 'live')))
"""

RESOLVERS = """
-- Same signature as 0004, now with the portal gate, so a pod on the previous
-- release cannot resolve a customer whose portal is not live.
CREATE OR REPLACE FUNCTION access.resolve_principal(p_oid uuid)
RETURNS TABLE (kind text, role_name text, role_class text, permissions text[],
               all_tenants boolean, tenant_ids uuid[], account_ids uuid[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT kind, role_name, role_class, permissions, all_tenants, tenant_ids, account_ids
    FROM access.resolve_principal_v2(p_oid)
$$;
"""

RESOLVER_V2 = f"""
CREATE FUNCTION access.resolve_principal_v2(p_oid uuid)
RETURNS TABLE (kind text, role_name text, role_class text, permissions text[],
               all_tenants boolean, tenant_ids uuid[], account_ids uuid[],
               is_tenant_admin boolean, landing text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT p.kind, r.name, r.role_class,
           coalesce((SELECT array_agg(rp.permission ORDER BY rp.permission)
                     FROM access.role_permission rp WHERE rp.role_id = r.id), '{{}}'),
           p.all_tenants,
           CASE WHEN p.all_tenants
                THEN coalesce((SELECT array_agg(t.id ORDER BY t.id) FROM cloudops.tenant t), '{{}}')
                ELSE coalesce((SELECT array_agg(pt.tenant_id ORDER BY pt.tenant_id)
                               FROM access.principal_tenant pt WHERE pt.oid = p.oid), '{{}}')
           END,
           (SELECT array_agg(pa.account_id ORDER BY pa.account_id)
            FROM access.principal_account pa WHERE pa.oid = p.oid),
           p.is_tenant_admin,
           r.landing
    FROM access.principal p
    JOIN access.role_definition r ON r.id = p.role_id
    WHERE p.oid = p_oid AND p.disabled_at IS NULL AND {CUSTOMER_GATE}
$$;
"""

FUNCTIONS = """
-- The one onboarding row, created on first use. Locked for the step.
CREATE FUNCTION access.portal_row(p_tenant uuid) RETURNS cloudops.portal_onboarding
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v cloudops.portal_onboarding;
BEGIN
    -- An unknown tenant fails the foreign key (which RLS does not hide).
    INSERT INTO cloudops.portal_onboarding (tenant_id) VALUES (p_tenant) ON CONFLICT DO NOTHING;
    SELECT * INTO v FROM cloudops.portal_onboarding WHERE tenant_id = p_tenant FOR UPDATE;
    RETURN v;
END $$;

-- Step 1: federation. Entra B2B, to the customer's Entra tenant or to its
-- SAML / WS-Fed provider. The Entra side is manual (docs/customer-portal-federation.md).
CREATE FUNCTION access.portal_record_federation(
    p_actor uuid, p_tenant uuid, p_idp_kind text, p_idp_reference text, p_domains text[]
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_manager(p_actor);
    IF coalesce(cardinality(p_domains), 0) = 0
       OR EXISTS (SELECT 1 FROM unnest(p_domains) d
                  WHERE d !~ ('^[a-z0-9]([a-z0-9-]*[a-z0-9])?'
                              || '(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$')) THEN
        RAISE EXCEPTION 'federation needs at least one valid, lower-case domain'
            USING ERRCODE = 'check_violation';
    END IF;
    PERFORM access.portal_row(p_tenant);
    UPDATE cloudops.portal_onboarding
       SET idp_kind = p_idp_kind, idp_reference = p_idp_reference,
           domains = (SELECT array_agg(DISTINCT d ORDER BY d) FROM unnest(p_domains) d),
           federation_recorded_at = now(), federation_recorded_by = p_actor, updated_at = now()
     WHERE tenant_id = p_tenant;
    PERFORM access.log_change(p_actor::text, 'portal_record_federation', jsonb_build_object(
        'tenant_id', p_tenant, 'idp_kind', p_idp_kind, 'domains', to_jsonb(p_domains)));
END $$;

-- Step 2: the role set agreed with the customer. Replaces the previous set.
-- Customer users whose role leaves the set stop resolving on their next request.
CREATE FUNCTION access.portal_agree_roles(
    p_actor uuid, p_tenant uuid, p_role_names text[], p_agreement_ref text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v cloudops.portal_onboarding;
BEGIN
    PERFORM access.require_manager(p_actor);
    v := access.portal_row(p_tenant);
    IF v.federation_recorded_at IS NULL THEN
        RAISE EXCEPTION 'record federation first' USING ERRCODE = 'POSEQ';
    END IF;
    IF coalesce(cardinality(p_role_names), 0) = 0 THEN
        RAISE EXCEPTION 'agree at least one role' USING ERRCODE = 'check_violation';
    END IF;
    IF (SELECT count(*) FROM access.role_definition
        WHERE name = ANY(p_role_names) AND role_class = 'customer')
       <> (SELECT count(DISTINCT n) FROM unnest(p_role_names) n) THEN
        RAISE EXCEPTION 'every agreed role must be an existing customer role'
            USING ERRCODE = 'check_violation';
    END IF;
    DELETE FROM cloudops.portal_role_agreement WHERE tenant_id = p_tenant;
    INSERT INTO cloudops.portal_role_agreement (tenant_id, role_id, agreed_by)
    SELECT p_tenant, r.id, p_actor FROM access.role_definition r WHERE r.name = ANY(p_role_names);
    UPDATE cloudops.portal_onboarding
       SET roles_agreed_at = now(), roles_agreed_by = p_actor,
           roles_agreement_ref = p_agreement_ref, updated_at = now()
     WHERE tenant_id = p_tenant;
    PERFORM access.log_change(p_actor::text, 'portal_agree_roles', jsonb_build_object(
        'tenant_id', p_tenant, 'roles', to_jsonb(p_role_names), 'agreement', p_agreement_ref));
END $$;

-- Step 3: the customer's first administrator, a B2B guest identified by its oid.
-- Later administrators are the customer's own business (tenant_set_user).
CREATE FUNCTION access.portal_assign_first_admin(
    p_actor uuid, p_tenant uuid, p_oid uuid, p_display_name text, p_role_name text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v cloudops.portal_onboarding;
    v_role access.role_definition;
BEGIN
    PERFORM access.require_manager(p_actor);
    v := access.portal_row(p_tenant);
    IF v.roles_agreed_at IS NULL THEN
        RAISE EXCEPTION 'agree the role set first' USING ERRCODE = 'POSEQ';
    END IF;
    IF v.first_admin_oid IS NOT NULL THEN
        RAISE EXCEPTION 'the first administrator is already assigned'
            USING ERRCODE = 'unique_violation';
    END IF;
    SELECT r.* INTO v_role FROM access.role_definition r
    JOIN cloudops.portal_role_agreement g ON g.role_id = r.id AND g.tenant_id = p_tenant
    WHERE r.name = p_role_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'role is not in the agreed set' USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM access.principal WHERE oid = p_oid) THEN
        RAISE EXCEPTION 'principal already exists' USING ERRCODE = 'unique_violation';
    END IF;
    INSERT INTO access.principal (oid, kind, display_name, role_id, is_tenant_admin)
    VALUES (p_oid, 'customer', p_display_name, v_role.id, true);
    INSERT INTO access.principal_tenant (oid, tenant_id) VALUES (p_oid, p_tenant);
    UPDATE cloudops.portal_onboarding
       SET first_admin_oid = p_oid, first_admin_at = now(), first_admin_by = p_actor,
           updated_at = now()
     WHERE tenant_id = p_tenant;
    PERFORM access.log_change(p_actor::text, 'portal_assign_first_admin', jsonb_build_object(
        'tenant_id', p_tenant, 'oid', p_oid, 'role', p_role_name));
END $$;

-- Go live, or suspend: suspension stops every customer user of the tenant on
-- their next request (ADR 0002, D4) without touching their assignments.
CREATE FUNCTION access.portal_set_live(p_actor uuid, p_tenant uuid, p_live boolean)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v cloudops.portal_onboarding;
BEGIN
    PERFORM access.require_manager(p_actor);
    v := access.portal_row(p_tenant);
    IF p_live AND (v.federation_recorded_at IS NULL OR v.roles_agreed_at IS NULL
                   OR v.first_admin_oid IS NULL) THEN
        RAISE EXCEPTION 'federation, role set and first administrator come before go-live'
            USING ERRCODE = 'POSEQ';
    END IF;
    IF p_live THEN
        UPDATE cloudops.portal_onboarding SET status = 'live', live_at = now(), live_by = p_actor,
               suspended_at = NULL, suspended_by = NULL, updated_at = now()
         WHERE tenant_id = p_tenant;
    ELSE
        UPDATE cloudops.portal_onboarding
           SET status = CASE WHEN status = 'setup' THEN 'setup' ELSE 'suspended' END,
               suspended_at = now(), suspended_by = p_actor, updated_at = now()
         WHERE tenant_id = p_tenant;
    END IF;
    PERFORM access.log_change(p_actor::text,
        CASE WHEN p_live THEN 'portal_go_live' ELSE 'portal_suspend' END,
        jsonb_build_object('tenant_id', p_tenant));
END $$;

-- The customer roles a platform administrator can put in an agreed set.
CREATE FUNCTION access.customer_role_catalogue(p_actor uuid)
RETURNS TABLE (id uuid, name text, description text, landing text, permissions text[])
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    PERFORM access.require_manager(p_actor);
    RETURN QUERY
    SELECT r.id, r.name, r.description, r.landing,
           coalesce((SELECT array_agg(rp.permission ORDER BY rp.permission)
                     FROM access.role_permission rp WHERE rp.role_id = r.id), '{}')
    FROM access.role_definition r WHERE r.role_class = 'customer' ORDER BY r.name;
END $$;

-- The acting customer administrator's tenant, or refusal. An administrator is
-- an active, live, whole-tenant customer principal: a subscription-limited one
-- could otherwise create users wider than itself.
CREATE FUNCTION access.require_tenant_admin(p_actor uuid) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_tenant uuid;
BEGIN
    SELECT t.tenant_ids[1] INTO v_tenant
    FROM access.resolve_principal_v2(p_actor) t
    WHERE t.kind = 'customer' AND t.is_tenant_admin AND cardinality(t.tenant_ids) = 1
      AND t.account_ids IS NULL;
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'not a tenant administrator' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN v_tenant;
END $$;

-- The users of the administrator's own tenant. Customer principals only.
CREATE FUNCTION access.tenant_users(p_actor uuid)
RETURNS TABLE (oid uuid, display_name text, role_name text, is_tenant_admin boolean,
               subscription_limited boolean, disabled_at timestamptz, created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_tenant uuid := access.require_tenant_admin(p_actor);
BEGIN
    RETURN QUERY
    SELECT p.oid, p.display_name, r.name, p.is_tenant_admin,
           EXISTS (SELECT 1 FROM access.principal_account pa WHERE pa.oid = p.oid),
           p.disabled_at, p.created_at
    FROM access.principal p
    JOIN access.principal_tenant pt ON pt.oid = p.oid AND pt.tenant_id = v_tenant
    JOIN access.role_definition r ON r.id = p.role_id
    WHERE p.kind = 'customer'
    ORDER BY p.disabled_at IS NOT NULL, p.display_name, p.oid;
END $$;

-- The roles the administrator may hand out: the agreed set.
CREATE FUNCTION access.tenant_roles(p_actor uuid)
RETURNS TABLE (name text, description text, landing text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_tenant uuid := access.require_tenant_admin(p_actor);
BEGIN
    RETURN QUERY
    SELECT r.name, r.description, r.landing
    FROM cloudops.portal_role_agreement g JOIN access.role_definition r ON r.id = g.role_id
    WHERE g.tenant_id = v_tenant ORDER BY r.name;
END $$;

-- At least one active administrator remains in the tenant after a change.
CREATE FUNCTION access.require_tenant_admin_remains(p_tenant uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.principal_tenant pt ON pt.oid = p.oid AND pt.tenant_id = p_tenant
        WHERE p.kind = 'customer' AND p.is_tenant_admin AND p.disabled_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM access.principal_account pa WHERE pa.oid = p.oid)) THEN
        RAISE EXCEPTION 'the tenant''s last administrator cannot be removed'
            USING ERRCODE = 'POLST';
    END IF;
END $$;

-- Add a user to the administrator's tenant, or change one already in it. A
-- principal that exists anywhere else (staff, another tenant) is refused with
-- the same error as an unknown one: the caller learns nothing about it.
CREATE FUNCTION access.tenant_set_user(
    p_actor uuid, p_oid uuid, p_display_name text, p_role_name text, p_is_admin boolean
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_tenant uuid := access.require_tenant_admin(p_actor);
    v_role uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('access.tenant_admin:' || v_tenant::text));
    SELECT r.id INTO v_role FROM access.role_definition r
    JOIN cloudops.portal_role_agreement g ON g.role_id = r.id AND g.tenant_id = v_tenant
    WHERE r.name = p_role_name AND r.role_class = 'customer';
    IF v_role IS NULL THEN
        RAISE EXCEPTION 'role is not in the agreed set' USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM access.principal p WHERE p.oid = p_oid)
       AND NOT EXISTS (
           SELECT 1 FROM access.principal p
           WHERE p.oid = p_oid AND p.kind = 'customer'
             AND (SELECT array_agg(pt.tenant_id) FROM access.principal_tenant pt
                  WHERE pt.oid = p.oid) = ARRAY[v_tenant]) THEN
        RAISE EXCEPTION 'no such user in this tenant' USING ERRCODE = 'no_data_found';
    END IF;
    IF p_is_admin AND EXISTS (SELECT 1 FROM access.principal_account pa WHERE pa.oid = p_oid) THEN
        RAISE EXCEPTION 'a subscription-limited user cannot administer the tenant'
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO access.principal (oid, kind, display_name, role_id, is_tenant_admin)
    VALUES (p_oid, 'customer', p_display_name, v_role, p_is_admin)
    ON CONFLICT (oid) DO UPDATE
        SET display_name = EXCLUDED.display_name, role_id = EXCLUDED.role_id,
            is_tenant_admin = EXCLUDED.is_tenant_admin, disabled_at = NULL, updated_at = now();
    INSERT INTO access.principal_tenant (oid, tenant_id) VALUES (p_oid, v_tenant)
    ON CONFLICT DO NOTHING;
    PERFORM access.require_tenant_admin_remains(v_tenant);
    PERFORM access.log_change(p_actor::text, 'tenant_set_user', jsonb_build_object(
        'tenant_id', v_tenant, 'oid', p_oid, 'role', p_role_name, 'is_admin', p_is_admin));
END $$;

CREATE FUNCTION access.tenant_disable_user(p_actor uuid, p_oid uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_tenant uuid := access.require_tenant_admin(p_actor);
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('access.tenant_admin:' || v_tenant::text));
    UPDATE access.principal p SET disabled_at = now(), updated_at = now()
     WHERE p.oid = p_oid AND p.kind = 'customer' AND p.disabled_at IS NULL
       AND (SELECT array_agg(pt.tenant_id) FROM access.principal_tenant pt
            WHERE pt.oid = p.oid) = ARRAY[v_tenant];
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such user in this tenant' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM access.require_tenant_admin_remains(v_tenant);
    PERFORM access.log_change(p_actor::text, 'tenant_disable_user',
        jsonb_build_object('tenant_id', v_tenant, 'oid', p_oid));
END $$;
"""

READER_V2 = "resolve_principal_v2(uuid)"
HELPERS = (
    "portal_row(uuid)",
    "require_tenant_admin(uuid)",
    "require_tenant_admin_remains(uuid)",
)
WRITERS = (
    "portal_record_federation(uuid, uuid, text, text, text[])",
    "portal_agree_roles(uuid, uuid, text[], text)",
    "portal_assign_first_admin(uuid, uuid, uuid, text, text)",
    "portal_set_live(uuid, uuid, boolean)",
    "customer_role_catalogue(uuid)",
    "tenant_users(uuid)",
    "tenant_roles(uuid)",
    "tenant_set_user(uuid, uuid, text, text, boolean)",
    "tenant_disable_user(uuid, uuid)",
)


def _resolver() -> str:
    """The resolver functions belong to cloudops_resolver. The migrator acts as
    it (member WITH INHERIT FALSE, so only by SET ROLE) to define them."""
    return "\n".join([
        "GRANT CREATE ON SCHEMA access TO cloudops_resolver;",
        "SET LOCAL ROLE cloudops_resolver;",
        RESOLVER_V2,
        RESOLVERS,
        f"REVOKE ALL ON FUNCTION access.{READER_V2} FROM PUBLIC;",
        f"GRANT EXECUTE ON FUNCTION access.{READER_V2} TO cloudops_app, cloudops_access_admin;",
        "RESET ROLE;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_resolver;",
    ])


def _ownership() -> str:
    lines = [f"REVOKE ALL ON FUNCTION access.{fn} FROM PUBLIC;" for fn in (*HELPERS, *WRITERS)]
    lines += [f"GRANT EXECUTE ON FUNCTION access.{fn} TO cloudops_app;" for fn in WRITERS]
    lines += [f"GRANT EXECUTE ON FUNCTION access.{fn} TO cloudops_access_admin;" for fn in HELPERS]
    # Grants first: once ownership moves, the migrator can no longer grant.
    lines.append("GRANT CREATE ON SCHEMA access TO cloudops_access_admin;")
    lines += [f"ALTER FUNCTION access.{fn} OWNER TO cloudops_access_admin;"
              for fn in (*HELPERS, *WRITERS)]
    lines.append("REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;")
    return "\n".join(lines)


def upgrade() -> None:
    op.execute(PRECONDITION)
    op.execute(_customer_grants())
    op.execute(ACCESS_DDL)
    op.execute(_role_defaults())
    op.execute(PORTAL_DDL)
    op.execute(_resolver())
    op.execute(FUNCTIONS)
    op.execute(_ownership())


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
