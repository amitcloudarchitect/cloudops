"""Cloud onboarding: stored customer credentials, their checks, shared accounts (expand).

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-31

ADR 0003 (stored, read-only customer credentials until production) and ADR 0027.
A platform administrator onboards an Azure customer from an app registration, or
an AWS account from an access key pair. The secret itself never reaches the
database: it is a Key Vault secret, named here, which the connector worker reads
through the ``SecretStore``.

- ``cloudops.customer_credential``: the inventory (ADR 0003, guardrail 6). One
  row per stored credential: tenant, provider, the identifiers (directory and
  client id, or access key id), the accounts it is bound to, the Key Vault
  secret names, owner, created, and an expiry at most 90 days after the secret
  was set (guardrail 3). One live credential per tenant and provider, and one
  tenant per app registration or access key (guardrail 5). ``credential_source``
  can only be ``stored_key``: delegated access stores nothing.
- ``cloudops.credential_check``: the read-only validation and role check, as a
  request the connector worker claims (ADR 0015's re-process pattern: the API has
  no secret and no Temporal client). Status only moves forward.
- ``cloudops.cloud_account``: one row per tenant per provider id (index
  ``cloud_account_tenant_provider_native_key``; the old global unique constraint
  goes in 0026, the contract step). A trigger lets the same id be held by the
  internal tenant and at most one customer tenant, never two customers, and marks
  both rows ``shared_with_platform``: the provenance of the platform's own
  subscription appearing under a customer as well (ADR 0023, amended).
- Both new tables are tenant data with FORCE RLS, ``tenant_isolation`` and a
  restrictive ``staff_only`` policy. The application only reads them; every write
  is a SECURITY DEFINER function owned by ``cloudops_access_admin``:

    onboard_cloud_credential(actor, ...)   record a credential and queue its check
    request_credential_check(actor, id, e) recheck, or record a rotated secret
    revoke_cloud_credential(actor, id)     stop using it (the secret is deleted
                                           out of band, from the inventory)

  Each requires ``access.manage`` as staff, and the tenant assigned and in scope.
  The worker's side (ingest): ``claim_credential_check``, ``finish_credential_check``
  and ``active_credentials``, which lists only what is active and unexpired.
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = """
-- The platform's own subscription may also be a customer's (ADR 0023, amended).
ALTER TABLE cloudops.cloud_account
    ADD COLUMN shared_with_platform boolean NOT NULL DEFAULT false;

-- ADR 0003, guardrail 6: every stored customer credential, findable and revocable.
-- Identifiers only. The secret is in Key Vault under ``secret_names``.
CREATE TABLE cloudops.customer_credential (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    provider          text NOT NULL CHECK (provider IN ('azure', 'aws')),
    credential_source text NOT NULL DEFAULT 'stored_key' CHECK (credential_source = 'stored_key'),
    code              text NOT NULL CHECK (code ~ '^[a-z0-9]{2,10}$'),
    directory_id      uuid,
    client_id         uuid,
    access_key_id     text CHECK (access_key_id ~ '^AKIA[A-Z0-9]{16}$'),
    account_ids       text[] NOT NULL,
    secret_names      text[] NOT NULL CHECK (cardinality(secret_names) BETWEEN 1 AND 2),
    owner_oid         uuid NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    secret_set_at     timestamptz NOT NULL DEFAULT now(),
    expires_at        timestamptz NOT NULL,
    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'active', 'refused', 'revoked')),
    validated_at      timestamptz,
    revoked_at        timestamptz,
    revoked_by        uuid,
    UNIQUE (id, tenant_id),
    -- ADR 0003, guardrail 3.
    CHECK (expires_at > secret_set_at AND expires_at <= secret_set_at + interval '90 days'),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
    CHECK (provider = 'azure' AND directory_id IS NOT NULL AND client_id IS NOT NULL
               AND access_key_id IS NULL AND cardinality(account_ids) BETWEEN 1 AND 50
           OR provider = 'aws' AND access_key_id IS NOT NULL AND directory_id IS NULL
               AND client_id IS NULL AND cardinality(account_ids) = 1)
);

-- A read-only validation and role check of one credential, claimed and run by the
-- connector worker, which alone holds the secret.
CREATE TABLE cloudops.credential_check (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL,
    credential_id uuid NOT NULL,
    reason        text NOT NULL CHECK (reason IN ('onboard', 'rotation', 'recheck')),
    requested_by  uuid NOT NULL,
    requested_at  timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'requested'
                  CHECK (status IN ('requested', 'running', 'passed', 'refused', 'failed')),
    started_at    timestamptz,
    finished_at   timestamptz,
    outcome       text CHECK (outcome ~ '^[a-z_]{1,60}$'),  -- our own code, never source text
    -- Role names, account ids and counts the check found. Never a secret or a token.
    detail        jsonb CHECK (jsonb_typeof(detail) = 'object' AND length(detail::text) <= 8000),
    FOREIGN KEY (credential_id, tenant_id)
        REFERENCES cloudops.customer_credential (id, tenant_id) ON DELETE CASCADE,
    CHECK ((status IN ('requested', 'running')) = (finished_at IS NULL)),
    CHECK (status IN ('requested', 'running') OR outcome IS NOT NULL)
);

ALTER TABLE cloudops.customer_credential ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.customer_credential FORCE ROW LEVEL SECURITY;
ALTER TABLE cloudops.credential_check ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.credential_check FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.customer_credential
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY tenant_isolation ON cloudops.credential_check
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
-- How Getronics reaches a customer's estate is Getronics' record, never the customer's.
CREATE POLICY staff_only ON cloudops.customer_credential AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer'));
CREATE POLICY staff_only ON cloudops.credential_check AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer'));

-- The worker claims the next check, and lists what it may use, before it knows
-- the tenant: through the functions below only, whose owner no login can become.
CREATE POLICY check_queue_read ON cloudops.credential_check FOR SELECT
  TO cloudops_access_admin USING (status IN ('requested', 'running'));
CREATE POLICY check_queue_claim ON cloudops.credential_check FOR UPDATE
  TO cloudops_access_admin USING (status = 'requested') WITH CHECK (status = 'running');
CREATE POLICY credential_registry_read ON cloudops.customer_credential FOR SELECT
  TO cloudops_access_admin USING (revoked_at IS NULL);

-- A check is history: what it asked never changes, and its status only moves
-- forward, requested -> running -> passed | refused | failed.
CREATE FUNCTION cloudops.check_credential_check_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.credential_id, NEW.reason, NEW.requested_by,
        NEW.requested_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.credential_id, OLD.reason, OLD.requested_by,
        OLD.requested_at)
       OR NOT (OLD.status = 'requested' AND NEW.status = 'running'
               OR OLD.status = 'running' AND NEW.status IN ('passed', 'refused', 'failed')) THEN
        RAISE EXCEPTION 'credential checks only move forward'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER credential_check_forward_only
    BEFORE UPDATE ON cloudops.credential_check
    FOR EACH ROW EXECUTE FUNCTION cloudops.check_credential_check_transition();
CREATE TRIGGER credential_check_no_delete
    BEFORE DELETE ON cloudops.credential_check
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
-- The inventory is kept: a revoked credential stays listed (ADR 0003, Exit).
CREATE TRIGGER customer_credential_no_delete
    BEFORE DELETE ON cloudops.customer_credential
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
"""

SHARING = """
-- One provider id in two tenants only when one of them is the internal tenant: the
-- platform's own subscription, also onboarded as a customer's (ADR 0023, amended).
-- Two customers never share an id. The check needs every tenant's rows, so it is
-- SECURITY DEFINER, owned by cloudops_access_admin, which two policies below let
-- read cloud_account and mark its flag, and read which tenant is internal.
CREATE FUNCTION cloudops.check_account_sharing() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_others uuid[];
    v_internal uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtext('cloudops.cloud_account:' || NEW.provider || ':' || NEW.native_id));
    SELECT array_agg(DISTINCT a.tenant_id) INTO v_others FROM cloudops.cloud_account a
     WHERE a.provider = NEW.provider AND a.native_id = NEW.native_id
       AND a.tenant_id <> NEW.tenant_id;
    IF v_others IS NULL THEN
        NEW.shared_with_platform := false;
        RETURN NEW;
    END IF;
    SELECT t.id INTO v_internal FROM cloudops.tenant t WHERE t.internal;
    IF cardinality(v_others) > 1 OR v_internal IS NULL
       OR NOT (v_internal = NEW.tenant_id OR v_internal = v_others[1]) THEN
        -- Says nothing about which tenant holds it.
        RAISE EXCEPTION 'that account is held by another tenant' USING ERRCODE = 'CAOWN';
    END IF;
    NEW.shared_with_platform := true;
    UPDATE cloudops.cloud_account SET shared_with_platform = true
     WHERE provider = NEW.provider AND native_id = NEW.native_id
       AND tenant_id = v_others[1] AND NOT shared_with_platform;
    RETURN NEW;
END $$;
CREATE TRIGGER cloud_account_sharing
    BEFORE INSERT OR UPDATE OF tenant_id, provider, native_id ON cloudops.cloud_account
    FOR EACH ROW EXECUTE FUNCTION cloudops.check_account_sharing();

-- The internal tenant cannot stop being internal while it shares an account: the
-- customer's copy would then be a second customer holding the same id.
CREATE FUNCTION cloudops.check_internal_sharing() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF OLD.internal AND NOT NEW.internal AND EXISTS (
        SELECT 1 FROM cloudops.cloud_account a
        WHERE a.tenant_id = OLD.id AND a.shared_with_platform) THEN
        RAISE EXCEPTION 'the internal tenant shares an account with a customer'
            USING ERRCODE = 'CAOWN';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tenant_internal_sharing
    BEFORE UPDATE OF internal ON cloudops.tenant
    FOR EACH ROW EXECUTE FUNCTION cloudops.check_internal_sharing();

CREATE POLICY account_sharing_read ON cloudops.cloud_account FOR SELECT
    TO cloudops_access_admin USING (true);
CREATE POLICY account_sharing_mark ON cloudops.cloud_account FOR UPDATE
    TO cloudops_access_admin USING (true) WITH CHECK (true);
CREATE POLICY internal_tenant_read ON cloudops.tenant FOR SELECT
    TO cloudops_access_admin USING (internal);
"""

FUNCTIONS = """
-- The actor holds access.manage as staff and, when a tenant is named, is assigned
-- it and has it in this transaction's scope.
CREATE FUNCTION access.require_credential_admin(p_actor uuid, p_tenant uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class <> 'customer' AND rp.permission = 'access.manage'
    ) THEN
        RAISE EXCEPTION 'not permitted to onboard cloud credentials'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_tenant IS NULL THEN
        RETURN;
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

-- ADR 0003: identifiers only. The secret names follow one pattern per tenant
-- (customer-<code>-azure-secret; for AWS the key id and the secret key).
CREATE FUNCTION access.onboard_cloud_credential(
    p_actor uuid, p_tenant uuid, p_provider text, p_directory_id uuid, p_client_id uuid,
    p_access_key_id text, p_account_ids text[], p_expires_at timestamptz
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_id uuid := gen_random_uuid();
    v_code text;
    v_accounts text[];
    v_secrets text[];
BEGIN
    PERFORM access.require_credential_admin(p_actor, p_tenant);
    SELECT lower(t.short_code) INTO v_code FROM cloudops.tenant t WHERE t.id = p_tenant;
    SELECT array_agg(DISTINCT lower(btrim(a)) ORDER BY lower(btrim(a))) INTO v_accounts
      FROM unnest(p_account_ids) a;
    IF p_provider = 'azure' THEN
        IF EXISTS (SELECT 1 FROM unnest(v_accounts) a
                   WHERE a !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RAISE EXCEPTION 'subscription ids are GUIDs' USING ERRCODE = 'check_violation';
        END IF;
        v_secrets := ARRAY['customer-' || v_code || '-azure-secret'];
    ELSIF p_provider = 'aws' THEN
        IF EXISTS (SELECT 1 FROM unnest(v_accounts) a WHERE a !~ '^[0-9]{12}$') THEN
            RAISE EXCEPTION 'an AWS account id is twelve digits' USING ERRCODE = 'check_violation';
        END IF;
        v_secrets := ARRAY['customer-' || v_code || '-aws-access-key-id',
                           'customer-' || v_code || '-aws-secret-access-key'];
    ELSE
        RAISE EXCEPTION 'provider is azure or aws' USING ERRCODE = 'check_violation';
    END IF;
    IF p_expires_at IS NULL OR p_expires_at <= now()
       OR p_expires_at > now() + interval '90 days' THEN
        RAISE EXCEPTION 'the secret must expire within 90 days' USING ERRCODE = 'check_violation';
    END IF;
    BEGIN
        INSERT INTO cloudops.customer_credential (id, tenant_id, provider, code, directory_id,
            client_id, access_key_id, account_ids, secret_names, owner_oid, expires_at)
        VALUES (v_id, p_tenant, p_provider, v_code, p_directory_id, p_client_id,
                p_access_key_id, v_accounts, v_secrets, p_actor, p_expires_at);
    EXCEPTION WHEN unique_violation THEN
        -- One live credential per tenant and provider; one tenant per credential.
        RAISE EXCEPTION 'that credential or tenant already has a live credential'
            USING ERRCODE = 'CMDUP';
    END;
    INSERT INTO cloudops.credential_check (tenant_id, credential_id, reason, requested_by)
    VALUES (p_tenant, v_id, 'onboard', p_actor);
    PERFORM access.log_change(p_actor::text, 'onboard_cloud_credential', jsonb_build_object(
        'credential_id', v_id, 'tenant_id', p_tenant, 'provider', p_provider,
        'accounts', to_jsonb(v_accounts), 'expires_at', p_expires_at));
    RETURN v_id;
END $$;

-- Check again. With an expiry, a rotated secret was put in Key Vault under the same
-- name: the expiry restarts from now, at most 90 days.
CREATE FUNCTION access.request_credential_check(
    p_actor uuid, p_credential uuid, p_expires_at timestamptz
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v cloudops.customer_credential;
    v_id uuid := gen_random_uuid();
BEGIN
    SELECT * INTO v FROM cloudops.customer_credential WHERE id = p_credential FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM access.require_credential_admin(p_actor, NULL);
        RAISE EXCEPTION 'credential not found' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM access.require_credential_admin(p_actor, v.tenant_id);
    IF v.status = 'revoked' THEN
        RAISE EXCEPTION 'a revoked credential is not checked again' USING ERRCODE = 'CMDUP';
    END IF;
    IF EXISTS (SELECT 1 FROM cloudops.credential_check c
               WHERE c.credential_id = p_credential AND c.status IN ('requested', 'running')) THEN
        RAISE EXCEPTION 'a check is already waiting or running' USING ERRCODE = 'CMDUP';
    END IF;
    IF p_expires_at IS NOT NULL THEN
        IF p_expires_at <= now() OR p_expires_at > now() + interval '90 days' THEN
            RAISE EXCEPTION 'the secret must expire within 90 days'
                USING ERRCODE = 'check_violation';
        END IF;
        UPDATE cloudops.customer_credential
           SET secret_set_at = now(), expires_at = p_expires_at
         WHERE id = p_credential;
    END IF;
    INSERT INTO cloudops.credential_check (id, tenant_id, credential_id, reason, requested_by)
    VALUES (v_id, v.tenant_id, p_credential,
            CASE WHEN p_expires_at IS NULL THEN 'recheck' ELSE 'rotation' END, p_actor);
    PERFORM access.log_change(p_actor::text, 'request_credential_check', jsonb_build_object(
        'credential_id', p_credential, 'tenant_id', v.tenant_id, 'check_id', v_id,
        'expires_at', p_expires_at));
    RETURN v_id;
END $$;

-- Stop using it at once. The Key Vault secret and the app registration's secret
-- (or the access key) are removed out of band, from this inventory.
CREATE FUNCTION access.revoke_cloud_credential(p_actor uuid, p_credential uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v cloudops.customer_credential;
BEGIN
    SELECT * INTO v FROM cloudops.customer_credential WHERE id = p_credential FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM access.require_credential_admin(p_actor, NULL);
        RAISE EXCEPTION 'credential not found' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM access.require_credential_admin(p_actor, v.tenant_id);
    IF v.status = 'revoked' THEN
        RETURN;
    END IF;
    UPDATE cloudops.customer_credential
       SET status = 'revoked', revoked_at = now(), revoked_by = p_actor
     WHERE id = p_credential;
    PERFORM access.log_change(p_actor::text, 'revoke_cloud_credential', jsonb_build_object(
        'credential_id', p_credential, 'tenant_id', v.tenant_id));
END $$;

-- The connector worker's side, under the ingest login. Claim the oldest waiting
-- check (none: no row); the worker reads the credential under that tenant's scope.
CREATE FUNCTION cloudops.claim_credential_check(p_provider text)
RETURNS TABLE (id uuid, tenant_id uuid, credential_id uuid, reason text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    UPDATE cloudops.credential_check c SET status = 'running', started_at = now()
    WHERE c.id = (SELECT q.id FROM cloudops.credential_check q
                  JOIN cloudops.customer_credential k ON k.id = q.credential_id
                  WHERE q.status = 'requested' AND k.provider = p_provider
                  ORDER BY q.requested_at LIMIT 1 FOR UPDATE OF q SKIP LOCKED)
    RETURNING c.id, c.tenant_id, c.credential_id, c.reason;
END $$;

-- Under the check's tenant scope: a check outside it is not found. Passed makes the
-- credential active; refused stops it being used; failed (the cloud did not answer)
-- leaves it as it was.
CREATE FUNCTION cloudops.finish_credential_check(
    p_id uuid, p_status text, p_outcome text, p_detail jsonb
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_credential uuid;
BEGIN
    UPDATE cloudops.credential_check
    SET status = p_status, finished_at = now(), outcome = p_outcome, detail = p_detail
    WHERE id = p_id AND status = 'running'
    RETURNING credential_id INTO v_credential;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no running check with that id' USING ERRCODE = 'no_data_found';
    END IF;
    IF p_status = 'passed' THEN
        UPDATE cloudops.customer_credential SET status = 'active', validated_at = now()
         WHERE id = v_credential AND status <> 'revoked';
    ELSIF p_status = 'refused' THEN
        UPDATE cloudops.customer_credential SET status = 'refused'
         WHERE id = v_credential AND status <> 'revoked';
    END IF;
END $$;

-- What the worker may use now: active, unexpired, never revoked. Identifiers and
-- secret names only.
CREATE FUNCTION cloudops.active_credentials(p_provider text)
RETURNS TABLE (id uuid, tenant_id uuid, code text, directory_id uuid, client_id uuid,
               access_key_id text, account_ids text[], secret_names text[],
               expires_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT k.id, k.tenant_id, k.code, k.directory_id, k.client_id, k.access_key_id,
           k.account_ids, k.secret_names, k.expires_at
    FROM cloudops.customer_credential k
    WHERE k.provider = p_provider AND k.status = 'active' AND k.expires_at > now()
    ORDER BY k.code
$$;
"""

HELPERS = ("require_credential_admin(uuid, uuid)",)
WRITERS = (
    "onboard_cloud_credential(uuid, uuid, text, uuid, uuid, text, text[], timestamptz)",
    "request_credential_check(uuid, uuid, timestamptz)",
    "revoke_cloud_credential(uuid, uuid)",
)
WORKER = (
    "claim_credential_check(text)",
    "finish_credential_check(uuid, text, text, jsonb)",
    "active_credentials(text)",
)
TRIGGERS = ("check_account_sharing()", "check_internal_sharing()")

PRIVILEGES = """
REVOKE ALL ON FUNCTION cloudops.check_credential_check_transition() FROM PUBLIC;
GRANT SELECT ON cloudops.customer_credential, cloudops.credential_check TO cloudops_app;
-- The worker reads a credential and its check under the tenant's scope.
GRANT SELECT ON cloudops.customer_credential, cloudops.credential_check TO cloudops_ingest;
GRANT SELECT, INSERT, UPDATE ON cloudops.customer_credential, cloudops.credential_check
    TO cloudops_access_admin;
GRANT SELECT (id, tenant_id, provider, native_id, shared_with_platform),
      UPDATE (shared_with_platform) ON cloudops.cloud_account TO cloudops_access_admin;
GRANT SELECT (id, internal) ON cloudops.tenant TO cloudops_access_admin;
-- Provenance, to whoever may read the account: staff, and the customer itself.
GRANT SELECT (shared_with_platform) ON cloudops.cloud_account TO cloudops_customer;
"""


def _ownership() -> str:
    lines = [f"REVOKE ALL ON FUNCTION access.{fn} FROM PUBLIC;" for fn in (*HELPERS, *WRITERS)]
    lines += [f"REVOKE ALL ON FUNCTION cloudops.{fn} FROM PUBLIC;" for fn in (*WORKER, *TRIGGERS)]
    lines += [f"GRANT EXECUTE ON FUNCTION access.{fn} TO cloudops_app;" for fn in WRITERS]
    lines += [f"GRANT EXECUTE ON FUNCTION access.{fn} TO cloudops_access_admin;"
              for fn in HELPERS]
    lines += [f"GRANT EXECUTE ON FUNCTION cloudops.{fn} TO cloudops_ingest;" for fn in WORKER]
    # A trigger function runs for whoever writes the row: the connectors, the seed.
    lines += [f"GRANT EXECUTE ON FUNCTION cloudops.{fn} TO cloudops_ingest;" for fn in TRIGGERS]
    # Grants first: once ownership moves, the migrator can no longer grant.
    lines.append("GRANT CREATE ON SCHEMA access TO cloudops_access_admin;")
    lines += [f"ALTER FUNCTION access.{fn} OWNER TO cloudops_access_admin;"
              for fn in (*HELPERS, *WRITERS)]
    lines.append("REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;")
    lines.append("GRANT CREATE ON SCHEMA cloudops TO cloudops_access_admin;")
    lines += [f"ALTER FUNCTION cloudops.{fn} OWNER TO cloudops_access_admin;"
              for fn in (*WORKER, *TRIGGERS)]
    lines.append("REVOKE CREATE ON SCHEMA cloudops FROM cloudops_access_admin;")
    return "\n".join(lines)


CONCURRENT_INDEXES = (
    # ON CONFLICT (tenant_id, provider, native_id): the connectors' upsert (identity.py).
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS cloud_account_tenant_provider_native_key"
    " ON cloudops.cloud_account (tenant_id, provider, native_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cloud_account_provider_native_idx"
    " ON cloudops.cloud_account (provider, native_id)",
    # ADR 0003, guardrail 5: one live credential per tenant and provider, and an app
    # registration or access key bound to one tenant.
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS customer_credential_tenant_live_key"
    " ON cloudops.customer_credential (tenant_id, provider) WHERE revoked_at IS NULL",
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS customer_credential_client_live_key"
    " ON cloudops.customer_credential (client_id) WHERE revoked_at IS NULL",
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS customer_credential_key_live_key"
    " ON cloudops.customer_credential (access_key_id) WHERE revoked_at IS NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS credential_check_credential_idx"
    " ON cloudops.credential_check (credential_id, requested_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS credential_check_queue_idx"
    " ON cloudops.credential_check (requested_at) WHERE status = 'requested'",
)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(SHARING)
    op.execute(FUNCTIONS)
    op.execute(PRIVILEGES)
    op.execute(_ownership())
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
