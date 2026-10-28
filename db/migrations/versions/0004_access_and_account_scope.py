"""Access control: principals, roles, the permission matrix, assignments,
subscription-level scope (expand).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28

ADR 0002. Who a principal is, which role it holds and which tenants and
subscriptions it may reach live here, in the database, and nowhere else: not
in the token, not in a header, not in a query parameter.

Schema ``access`` holds no customer data, only the mapping to it, and no
application role can read or write it directly. Two NOLOGIN roles own the
functions that can. Infrastructure creates them and makes the migration role a
member (WITH INHERIT FALSE), as it does for the group roles; this migration
checks that first and stops if not, because it must hand function ownership
to them:

  cloudops_resolver       read-only. Owns access.resolve_principal(), which
                          the API calls once per request, before any scope exists.
  cloudops_access_admin   owns the functions that change assignments and the
                          matrix. Each checks that the acting principal holds
                          access.manage, and records the change in
                          access.change_log (append-only).

Subscription scope (D16) is a second, RESTRICTIVE policy, account_scope, on
every table that names an account or a resource. ``app.account_ids`` is set by
packages/tenancy beside app.tenant_ids: ``all``, or a list of account ids.
Unset means no account, so a path that forgets it sees nothing.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

STAFF_CLASSES = (
    "ceo",
    "cfo",
    "finops",
    "secops",
    "delivery_lead",
    "commercial",
    "auditor",
    "platform_admin",
    "engineer",
)
# The roles commercial_visibility admits (0001). The matrix may only narrow this.
COMMERCIAL_CLASSES = (
    "ceo",
    "cfo",
    "finops",
    "delivery_lead",
    "commercial",
    "auditor",
    "platform_admin",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


ACCOUNT_SCOPE = """
CREATE FUNCTION cloudops.account_scope_is_all() RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT coalesce(pg_catalog.current_setting('app.account_ids', true), '') = 'all'
$$;

-- 'all' and unset both yield an empty array; only account_scope_is_all()
-- distinguishes them, and it is true for 'all' alone. Malformed raises.
CREATE FUNCTION cloudops.current_account_ids() RETURNS uuid[]
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN coalesce(pg_catalog.current_setting('app.account_ids', true), '') IN ('', 'all')
            THEN ARRAY[]::uuid[]
        ELSE pg_catalog.string_to_array(
                 pg_catalog.current_setting('app.account_ids', true), ',')::uuid[]
    END
$$;

CREATE POLICY account_scope ON cloudops.cloud_account AS RESTRICTIVE
  USING (cloudops.account_scope_is_all() OR id = ANY(cloudops.current_account_ids()));
CREATE POLICY account_scope ON cloudops.resource AS RESTRICTIVE
  USING (cloudops.account_scope_is_all() OR account_id = ANY(cloudops.current_account_ids()));
CREATE POLICY account_scope ON cloudops.cost_fact AS RESTRICTIVE
  USING (cloudops.account_scope_is_all() OR account_id = ANY(cloudops.current_account_ids()));
CREATE POLICY account_scope ON staging.resource_quarantine AS RESTRICTIVE
  USING (cloudops.account_scope_is_all() OR account_id = ANY(cloudops.current_account_ids()));
-- Tickets and findings follow their resource. The subquery runs under the
-- resource table's own policies, so a resource outside the accounts is not
-- found. Unlinked rows (resource_id NULL) are hidden from a restricted scope:
-- it sees only what is provably its own.
CREATE POLICY account_scope ON cloudops.work_item AS RESTRICTIVE
  USING (cloudops.account_scope_is_all()
         OR EXISTS (SELECT 1 FROM cloudops.resource r WHERE r.id = resource_id));
CREATE POLICY account_scope ON cloudops.vulnerability AS RESTRICTIVE
  USING (cloudops.account_scope_is_all()
         OR EXISTS (SELECT 1 FROM cloudops.resource r WHERE r.id = resource_id));
"""

ACCESS_SCHEMA = f"""
CREATE SCHEMA access;
REVOKE ALL ON SCHEMA access FROM PUBLIC;


CREATE TABLE access.permission (
    name          text PRIMARY KEY CHECK (name ~ '^[a-z_]+\\.[a-z_]+$'),
    description   text NOT NULL,
    is_commercial boolean NOT NULL DEFAULT false
);

CREATE TABLE access.role_definition (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL UNIQUE CHECK (length(btrim(name)) > 0),
    role_class  text NOT NULL CHECK (role_class IN ({_sql_list(STAFF_CLASSES)}, 'customer')),
    is_system   boolean NOT NULL DEFAULT false,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE access.role_permission (
    role_id    uuid NOT NULL REFERENCES access.role_definition(id) ON DELETE CASCADE,
    permission text NOT NULL REFERENCES access.permission(name),
    PRIMARY KEY (role_id, permission)
);

CREATE TABLE access.principal (
    oid          uuid PRIMARY KEY,  -- Entra object id (D1)
    kind         text NOT NULL CHECK (kind IN ('staff', 'customer')),
    display_name text NOT NULL,
    role_id      uuid NOT NULL REFERENCES access.role_definition(id),
    all_tenants  boolean NOT NULL DEFAULT false,
    disabled_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (NOT (kind = 'customer' AND all_tenants))
);

CREATE TABLE access.principal_tenant (
    oid       uuid NOT NULL REFERENCES access.principal(oid) ON DELETE CASCADE,
    tenant_id uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    PRIMARY KEY (oid, tenant_id)
);

-- Who changed access, and how. No tenant: these are platform events.
CREATE TABLE access.change_log (
    id         bigserial PRIMARY KEY,
    actor      text NOT NULL,
    action     text NOT NULL,
    detail     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER change_log_append_only
    BEFORE UPDATE OR DELETE ON access.change_log
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
CREATE TRIGGER change_log_no_truncate
    BEFORE TRUNCATE ON access.change_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();

-- D10: the matrix can only narrow commercial visibility. A commercial
-- permission on a role outside the RLS list is refused here, so no console
-- action can grant a customer margin data even by mistake.
CREATE FUNCTION access.reject_commercial_widening() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM access.permission p WHERE p.name = NEW.permission AND p.is_commercial)
       AND NOT EXISTS (SELECT 1 FROM access.role_definition r
                       WHERE r.id = NEW.role_id
                         AND r.role_class IN ({_sql_list(COMMERCIAL_CLASSES)})) THEN
        RAISE EXCEPTION 'commercial permission % cannot be granted to this role class',
            NEW.permission USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER role_permission_commercial_guard
    BEFORE INSERT OR UPDATE ON access.role_permission
    FOR EACH ROW EXECUTE FUNCTION access.reject_commercial_widening();

-- A role's class decides what RLS admits; changing it would bypass the guard above.
CREATE FUNCTION access.reject_class_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NEW.role_class IS DISTINCT FROM OLD.role_class THEN
        RAISE EXCEPTION 'a role class never changes; define a new role'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER role_definition_class_fixed
    BEFORE UPDATE ON access.role_definition
    FOR EACH ROW EXECUTE FUNCTION access.reject_class_change();
"""

# D15: the default matrix. A starting point for the product owner to refine
# in the console; every later change goes through the admin functions.
PERMISSIONS = (
    ("inventory.read", "Cloud accounts and resources", False),
    ("tickets.read", "Incidents, requests and changes", False),
    ("cost.read", "Cloud cost", False),
    ("vulnerabilities.read", "Vulnerability findings", False),
    ("knowledge.read", "Knowledge and runbooks", False),
    ("commercial.read", "SOWs, margin and rate card", True),
    ("audit.read", "The access and action log", False),
    ("access.manage", "Principals, assignments and the permission matrix", False),
)
OPERATIONS = ("inventory.read", "tickets.read", "vulnerabilities.read", "knowledge.read")
DEFAULT_ROLES = (
    ("CEO", "ceo", (*OPERATIONS, "cost.read", "commercial.read")),
    ("CFO", "cfo", ("inventory.read", "cost.read", "commercial.read")),
    ("FinOps", "finops", ("inventory.read", "cost.read", "commercial.read")),
    ("SecOps", "secops", OPERATIONS),
    ("Delivery lead", "delivery_lead", (*OPERATIONS, "cost.read", "commercial.read")),
    ("Commercial", "commercial", ("cost.read", "commercial.read")),
    ("Auditor", "auditor", (*OPERATIONS, "cost.read", "commercial.read", "audit.read")),
    ("Platform administrator", "platform_admin", tuple(p[0] for p in PERMISSIONS)),
    ("Engineer", "engineer", OPERATIONS),
    ("Customer viewer", "customer", (*OPERATIONS, "cost.read")),
)


def _defaults() -> str:
    rows = [
        "INSERT INTO access.permission (name, description, is_commercial) VALUES "
        + ", ".join(f"('{n}', '{d}', {str(c).lower()})" for n, d, c in PERMISSIONS)
        + ";"
    ]
    for name, cls, perms in DEFAULT_ROLES:
        rows.append(
            "INSERT INTO access.role_definition (name, role_class, is_system)"
            f" VALUES ('{name}', '{cls}', true);"
        )
        rows.append(
            "INSERT INTO access.role_permission (role_id, permission)"
            f" SELECT r.id, p FROM access.role_definition r,"
            f" unnest(ARRAY[{_sql_list(perms)}]) p WHERE r.name = '{name}';"
        )
    return "\n".join(rows)


FUNCTIONS = """
-- The one door the API has into access data. Called once per request, inside
-- a transaction with no tenant scope. Returns nothing for an unknown or
-- disabled principal. All-tenant principals get every tenant that exists
-- now, including ones onboarded after the assignment (D9).
CREATE FUNCTION access.resolve_principal(p_oid uuid)
RETURNS TABLE (kind text, role_name text, role_class text, permissions text[],
               all_tenants boolean, tenant_ids uuid[], account_ids uuid[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT p.kind, r.name, r.role_class,
           coalesce((SELECT array_agg(rp.permission ORDER BY rp.permission)
                     FROM access.role_permission rp WHERE rp.role_id = r.id), '{}'),
           p.all_tenants,
           CASE WHEN p.all_tenants
                THEN coalesce((SELECT array_agg(t.id ORDER BY t.id) FROM cloudops.tenant t), '{}')
                ELSE coalesce((SELECT array_agg(pt.tenant_id ORDER BY pt.tenant_id)
                               FROM access.principal_tenant pt WHERE pt.oid = p.oid), '{}')
           END,
           -- NULL: no subscription restriction.
           (SELECT array_agg(pa.account_id ORDER BY pa.account_id)
            FROM access.principal_account pa WHERE pa.oid = p.oid)
    FROM access.principal p
    JOIN access.role_definition r ON r.id = p.role_id
    WHERE p.oid = p_oid AND p.disabled_at IS NULL
$$;

CREATE FUNCTION access.require_manager(p_actor uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND rp.permission = 'access.manage'
    ) THEN
        RAISE EXCEPTION 'not permitted to manage access' USING ERRCODE = 'insufficient_privilege';
    END IF;
END $$;

CREATE FUNCTION access.log_change(p_actor text, p_action text, p_detail jsonb) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    INSERT INTO access.change_log (actor, action, detail) VALUES (p_actor, p_action, p_detail)
$$;

-- D14: the first platform administrator. Refuses once any active admin
-- exists, so it can never be used to add a second one quietly.
CREATE FUNCTION access.bootstrap_admin(p_oid uuid, p_display_name text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('access.bootstrap_admin'));
    IF EXISTS (SELECT 1 FROM access.principal p
               JOIN access.role_definition r ON r.id = p.role_id
               WHERE r.role_class = 'platform_admin' AND p.disabled_at IS NULL) THEN
        RAISE EXCEPTION 'a platform administrator already exists'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO access.principal (oid, kind, display_name, role_id, all_tenants)
    SELECT p_oid, 'staff', p_display_name, r.id, true
    FROM access.role_definition r WHERE r.name = 'Platform administrator' AND r.is_system;
    PERFORM access.log_change('bootstrap', 'bootstrap_admin', jsonb_build_object('oid', p_oid));
END $$;

CREATE FUNCTION access.set_principal(
    p_actor uuid, p_oid uuid, p_kind text, p_display_name text, p_role_name text,
    p_all_tenants boolean
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_role access.role_definition;
BEGIN
    PERFORM access.require_manager(p_actor);
    SELECT * INTO v_role FROM access.role_definition WHERE name = p_role_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown role' USING ERRCODE = 'no_data_found';
    END IF;
    -- Customers hold customer-class roles and staff hold staff roles, never crossed.
    IF (p_kind = 'customer') <> (v_role.role_class = 'customer') THEN
        RAISE EXCEPTION 'role class does not match principal kind'
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO access.principal (oid, kind, display_name, role_id, all_tenants)
    VALUES (p_oid, p_kind, p_display_name, v_role.id, p_all_tenants)
    ON CONFLICT (oid) DO UPDATE
        SET kind = EXCLUDED.kind, display_name = EXCLUDED.display_name,
            role_id = EXCLUDED.role_id, all_tenants = EXCLUDED.all_tenants,
            disabled_at = NULL, updated_at = now();
    PERFORM access.log_change(p_actor::text, 'set_principal', jsonb_build_object(
        'oid', p_oid, 'kind', p_kind, 'role', p_role_name, 'all_tenants', p_all_tenants));
END $$;

CREATE FUNCTION access.grant_tenant(p_actor uuid, p_oid uuid, p_tenant_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_manager(p_actor);
    INSERT INTO access.principal_tenant (oid, tenant_id) VALUES (p_oid, p_tenant_id)
    ON CONFLICT DO NOTHING;
    PERFORM access.log_change(p_actor::text, 'grant_tenant',
        jsonb_build_object('oid', p_oid, 'tenant_id', p_tenant_id));
END $$;

-- Takes effect on the principal's next request: scope is resolved per request (D4).
CREATE FUNCTION access.revoke_tenant(p_actor uuid, p_oid uuid, p_tenant_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_manager(p_actor);
    DELETE FROM access.principal_tenant WHERE oid = p_oid AND tenant_id = p_tenant_id;
    PERFORM access.log_change(p_actor::text, 'revoke_tenant',
        jsonb_build_object('oid', p_oid, 'tenant_id', p_tenant_id));
END $$;

-- D16: replace the principal's subscription restriction within one tenant.
-- An empty array removes the restriction (the whole tenant again).
CREATE FUNCTION access.restrict_accounts(
    p_actor uuid, p_oid uuid, p_tenant_id uuid, p_account_ids uuid[]
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_manager(p_actor);
    DELETE FROM access.principal_account WHERE oid = p_oid AND tenant_id = p_tenant_id;
    INSERT INTO access.principal_account (oid, tenant_id, account_id)
    SELECT p_oid, p_tenant_id, a FROM unnest(p_account_ids) a;
    PERFORM access.log_change(p_actor::text, 'restrict_accounts', jsonb_build_object(
        'oid', p_oid, 'tenant_id', p_tenant_id, 'account_ids', to_jsonb(p_account_ids)));
END $$;

CREATE FUNCTION access.disable_principal(p_actor uuid, p_oid uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_manager(p_actor);
    PERFORM pg_advisory_xact_lock(hashtext('access.bootstrap_admin'));
    -- D15: the last active platform administrator cannot be removed.
    IF EXISTS (SELECT 1 FROM access.principal p JOIN access.role_definition r ON r.id = p.role_id
               WHERE p.oid = p_oid AND r.role_class = 'platform_admin')
       AND (SELECT count(*) FROM access.principal p
            JOIN access.role_definition r ON r.id = p.role_id
            WHERE r.role_class = 'platform_admin' AND p.disabled_at IS NULL) <= 1 THEN
        RAISE EXCEPTION 'the last platform administrator cannot be disabled'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE access.principal SET disabled_at = now(), updated_at = now() WHERE oid = p_oid;
    PERFORM access.log_change(p_actor::text, 'disable_principal', jsonb_build_object('oid', p_oid));
END $$;

-- D13: platform-defined roles. Customer roles are created here, class 'customer'.
CREATE FUNCTION access.define_role(
    p_actor uuid, p_name text, p_role_class text, p_description text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_manager(p_actor);
    INSERT INTO access.role_definition (name, role_class, description)
    VALUES (p_name, p_role_class, p_description);
    PERFORM access.log_change(p_actor::text, 'define_role',
        jsonb_build_object('name', p_name, 'role_class', p_role_class));
END $$;

CREATE FUNCTION access.grant_permission(p_actor uuid, p_role_name text, p_permission text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_manager(p_actor);
    INSERT INTO access.role_permission (role_id, permission)
    SELECT r.id, p_permission FROM access.role_definition r WHERE r.name = p_role_name
    ON CONFLICT DO NOTHING;
    IF NOT FOUND AND NOT EXISTS (SELECT 1 FROM access.role_definition WHERE name = p_role_name) THEN
        RAISE EXCEPTION 'unknown role' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM access.log_change(p_actor::text, 'grant_permission',
        jsonb_build_object('role', p_role_name, 'permission', p_permission));
END $$;

CREATE FUNCTION access.revoke_permission(p_actor uuid, p_role_name text, p_permission text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM access.require_manager(p_actor);
    -- D15: the platform administrator role always keeps access management.
    IF p_permission = 'access.manage' AND EXISTS (
        SELECT 1 FROM access.role_definition
        WHERE name = p_role_name AND role_class = 'platform_admin' AND is_system) THEN
        RAISE EXCEPTION 'access.manage cannot be removed from the platform administrator role'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    DELETE FROM access.role_permission rp USING access.role_definition r
    WHERE rp.role_id = r.id AND r.name = p_role_name AND rp.permission = p_permission;
    PERFORM access.log_change(p_actor::text, 'revoke_permission',
        jsonb_build_object('role', p_role_name, 'permission', p_permission));
END $$;
"""

READERS = ("resolve_principal(uuid)",)
WRITERS = (
    "require_manager(uuid)",
    "log_change(text, text, jsonb)",
    "bootstrap_admin(uuid, text)",
    "set_principal(uuid, uuid, text, text, text, boolean)",
    "grant_tenant(uuid, uuid, uuid)",
    "revoke_tenant(uuid, uuid, uuid)",
    "restrict_accounts(uuid, uuid, uuid, uuid[])",
    "disable_principal(uuid, uuid)",
    "define_role(uuid, text, text, text)",
    "grant_permission(uuid, text, text)",
    "revoke_permission(uuid, text, text)",
)
# Entry points the application may call; helpers stay private to the owners.
APP_CALLABLE = tuple(f for f in (*READERS, *WRITERS) if not f.startswith(("require", "log")))


def _privileges() -> str:
    lines = [
        # Functions are executable by PUBLIC by default; none of these may be.
        "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA access FROM PUBLIC;",
        "GRANT USAGE ON SCHEMA access TO cloudops_resolver, cloudops_access_admin,"
        " cloudops_app;",
        "GRANT USAGE ON SCHEMA cloudops TO cloudops_resolver, cloudops_access_admin;",
        "GRANT SELECT ON ALL TABLES IN SCHEMA access TO cloudops_resolver, cloudops_access_admin;",
        "GRANT INSERT, UPDATE, DELETE ON access.principal, access.principal_tenant,"
        " access.principal_account, access.role_definition, access.role_permission"
        " TO cloudops_access_admin;",
        "GRANT INSERT ON access.change_log TO cloudops_access_admin;",
        "GRANT USAGE ON ALL SEQUENCES IN SCHEMA access TO cloudops_access_admin;",
        # The resolver lists tenants for all-tenant principals (D9); nothing else.
        "GRANT SELECT (id) ON cloudops.tenant TO cloudops_resolver;",
        "CREATE POLICY tenant_registry_read ON cloudops.tenant FOR SELECT"
        " TO cloudops_resolver USING (true);",
    ]
    for fn in APP_CALLABLE:
        lines.append(f"GRANT EXECUTE ON FUNCTION access.{fn} TO cloudops_app;")
    lines.append(
        "GRANT EXECUTE ON FUNCTION access.require_manager(uuid),"
        " access.log_change(text, text, jsonb) TO cloudops_access_admin;"
    )
    # Grants first: once ownership moves, the migrator can no longer grant.
    # A new owner needs CREATE on the schema at the moment of handover only.
    lines.append("GRANT CREATE ON SCHEMA access TO cloudops_resolver, cloudops_access_admin;")
    for fn in READERS:
        lines.append(f"ALTER FUNCTION access.{fn} OWNER TO cloudops_resolver;")
    for fn in WRITERS:
        lines.append(f"ALTER FUNCTION access.{fn} OWNER TO cloudops_access_admin;")
    lines.append("REVOKE CREATE ON SCHEMA access FROM cloudops_resolver, cloudops_access_admin;")
    return "\n".join(lines)


# A principal's subscriptions must belong to the tenant it is assigned:
# enforced by a composite key against cloud_account (id, tenant_id).
ACCOUNT_KEY_INDEX = (
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS cloud_account_id_tenant_key"
    " ON cloudops.cloud_account (id, tenant_id)"
)

PRINCIPAL_ACCOUNT = """
CREATE TABLE access.principal_account (
    oid        uuid NOT NULL,
    tenant_id  uuid NOT NULL,
    account_id uuid NOT NULL,
    PRIMARY KEY (oid, account_id),
    FOREIGN KEY (oid, tenant_id)
        REFERENCES access.principal_tenant (oid, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (account_id, tenant_id)
        REFERENCES cloudops.cloud_account (id, tenant_id) ON DELETE CASCADE
);
"""


PRECONDITION = """
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['cloudops_resolver', 'cloudops_access_admin'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            RAISE EXCEPTION 'role % does not exist; infrastructure creates it before migrating', r;
        END IF;
        IF NOT pg_has_role(current_user, r, 'SET') THEN
            RAISE EXCEPTION 'the migration role must be a member of % (WITH INHERIT FALSE)', r;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r
                   AND (rolsuper OR rolbypassrls OR rolcanlogin)) THEN
            RAISE EXCEPTION 'role % must be NOLOGIN, NOSUPERUSER, NOBYPASSRLS', r;
        END IF;
    END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(PRECONDITION)
    op.execute(ACCOUNT_SCOPE)
    op.execute(ACCESS_SCHEMA)
    op.execute(_defaults())
    with op.get_context().autocommit_block():
        op.execute(ACCOUNT_KEY_INDEX)
    op.execute(PRINCIPAL_ACCOUNT)
    op.execute(FUNCTIONS)
    op.execute(_privileges())


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
