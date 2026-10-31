"""Customer master: onboarding tenants and mapping source names to them (expand).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30

HLD M1, LLD Identity resolution, ADR 0015. Until now an alias reached
``cloudops.tenant_alias`` only by a direct insert; every name a source sends
that no alias matches waits in ``staging.tenant_alias_quarantine``. This adds
the governed path between the two.

- Permission ``mapping.manage`` and the system role "Customer master"
  (class ``delivery_lead``: Delivery owns this reference data), holding it with
  ``tickets.read`` and ``inventory.read``. Platform administrator holds it too.
  A trigger refuses it to any customer-class role, so no console action can hand
  the cross-tenant quarantine to a customer.
- ``cloudops.alias_proposal`` and ``cloudops.alias_decision``: who proposed which
  spelling for which tenant, and who approved or rejected it. Tenant data (the
  target tenant), FORCE RLS, and a restrictive policy keeping them from any
  customer scope. The application may only read them; every write is a
  function below. One decision per proposal, and a decision never changes.
- ``access.mapping_policy``: how many people an approval needs. Defaults to two
  (propose, then a different person approves). The migration job sets one in dev
  only, from ``CLOUDOPS_MAPPING_APPROVERS`` (db/migrations/env.py). Enforced by a
  trigger on ``alias_decision``, not only by the function that writes it.
- Functions, SECURITY DEFINER, owned by ``cloudops_access_admin`` (it already
  owns the access-change functions; no new server role), executable by the
  application only:

    alias_quarantine(actor)      the quarantine, for mapping.manage holders:
                                 source, alias, count, first/last seen, a
                                 suggested tenant in the caller's scope. Never
                                 raw_ref or any payload.
    onboard_tenant(actor, ...)   a new tenant: canonical name and short code.
    propose_alias(actor, ...)    a spelling for a tenant in the caller's scope.
    decide_alias(actor, ...)     approve or reject. Approval inserts the
                                 tenant_alias row and clears the quarantine row,
                                 keeping its counts on the decision.
    mapping_approvers()          the policy, so the console can say who may approve.

    request_reprocess(actor, ...) an explicit, audited request to re-process
                                 what an approved name held (below).

  Each checks the actor holds mapping.manage as staff, and that the target
  tenant is both assigned to the actor and in the transaction's scope. Each
  change is written to ``access.change_log``.
- ``cloudops.alias_reprocess``: requests to re-process the records held under an
  approved name, through the existing bronze replay narrowed to that name. The
  connector worker claims one with ``cloudops.claim_reprocess`` (it cannot know
  the tenant first, so two policies confined to the function owner let it see and
  claim only waiting requests), re-checks it under the tenant's scope, and closes
  it with ``cloudops.finish_reprocess`` and the exact count of records moved.
  Status only moves forward; the request itself never changes.
- The application loses INSERT and UPDATE on ``cloudops.tenant`` and
  ``cloudops.tenant_alias``, which no application code used: a tenant or an
  alias now exists only through onboarding or an approved proposal. Ingest
  keeps them (the local seed and connector tests load reference data that way).
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = """
-- How many people an approval needs. One row. Changed by the migration role only,
-- from configuration (env.py); two unless an environment says otherwise.
CREATE TABLE access.mapping_policy (
    singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    approvers  smallint NOT NULL DEFAULT 2 CHECK (approvers IN (1, 2)),
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO access.mapping_policy (singleton) VALUES (true);


CREATE TABLE cloudops.alias_proposal (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    source_system text NOT NULL CHECK (source_system ~ '^[a-z0-9_]{1,40}$'),
    -- Exactly as the source sends it: resolution's first step is an exact match.
    alias         text NOT NULL CHECK (length(btrim(alias)) > 0 AND length(alias) <= 200),
    note          text CHECK (length(note) <= 1000),
    proposed_by   uuid NOT NULL,
    proposed_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id)
);

CREATE TABLE cloudops.alias_decision (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id      uuid NOT NULL UNIQUE,  -- one decision per proposal, final
    tenant_id        uuid NOT NULL,
    decision         text NOT NULL CHECK (decision IN ('approve', 'reject')),
    reason           text CHECK (length(reason) <= 1000),
    decided_by       uuid NOT NULL,
    decided_at       timestamptz NOT NULL DEFAULT now(),
    -- The quarantine row an approval cleared, as it stood: how many sightings
    -- were held and over which days. NULL when nothing was held.
    held_occurrences bigint CHECK (held_occurrences > 0),
    held_first_seen  timestamptz,
    held_last_seen   timestamptz,
    FOREIGN KEY (proposal_id, tenant_id)
        REFERENCES cloudops.alias_proposal (id, tenant_id) ON DELETE CASCADE,
    CHECK (decision = 'approve' OR length(btrim(coalesce(reason, ''))) > 0),
    CHECK (decision = 'approve' OR held_occurrences IS NULL)
);

ALTER TABLE cloudops.alias_proposal ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.alias_proposal FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.alias_proposal
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));

ALTER TABLE cloudops.alias_decision ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.alias_decision FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.alias_decision
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));

-- Staff only, whatever the tenant: a customer's scope never reads how its name
-- was mapped or by whom.
CREATE POLICY staff_only ON cloudops.alias_proposal AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer'));
CREATE POLICY staff_only ON cloudops.alias_decision AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer'));

-- Append-only for everyone: privileges stop the application, these stop the owner.
CREATE TRIGGER alias_proposal_append_only
    BEFORE UPDATE OR DELETE ON cloudops.alias_proposal
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
CREATE TRIGGER alias_decision_append_only
    BEFORE UPDATE OR DELETE ON cloudops.alias_decision
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();

-- The two-person rule, held by the table itself so no path can skip it. Runs
-- as whoever inserts; the proposal is read under that caller's scope, and a
-- proposal it cannot see is refused rather than waved through.
CREATE FUNCTION cloudops.check_alias_decision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_proposer uuid;
BEGIN
    SELECT p.proposed_by INTO v_proposer FROM cloudops.alias_proposal p
    WHERE p.id = NEW.proposal_id AND p.tenant_id = NEW.tenant_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'proposal not found' USING ERRCODE = 'no_data_found';
    END IF;
    IF NEW.decision = 'approve' AND NEW.decided_by = v_proposer
       AND (SELECT approvers FROM access.mapping_policy) <> 1 THEN
        RAISE EXCEPTION 'a second person must approve this mapping'
            USING ERRCODE = 'CMTWO';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER alias_decision_two_person
    BEFORE INSERT ON cloudops.alias_decision
    FOR EACH ROW EXECUTE FUNCTION cloudops.check_alias_decision();

-- The quarantine holds names of companies that are nobody's tenant yet: never
-- a customer's to see, whatever an administrator grants.
CREATE FUNCTION access.reject_customer_mapping() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NEW.permission = 'mapping.manage' AND EXISTS (
        SELECT 1 FROM access.role_definition r
        WHERE r.id = NEW.role_id AND r.role_class = 'customer') THEN
        RAISE EXCEPTION 'mapping.manage cannot be granted to a customer role'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER role_permission_mapping_guard
    BEFORE INSERT OR UPDATE ON access.role_permission
    FOR EACH ROW EXECUTE FUNCTION access.reject_customer_mapping();


-- An explicit, audited request to re-process the records held under an approved
-- name: the replay of that name's bronze days, run by the connector worker
-- (ADR 0015). Nothing re-processes by itself.
CREATE TABLE cloudops.alias_reprocess (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL,
    proposal_id   uuid NOT NULL,
    source_system text NOT NULL,
    alias         text NOT NULL,
    window_start  date NOT NULL,
    window_end    date NOT NULL CHECK (window_end >= window_start),
    requested_by  uuid NOT NULL,
    requested_at  timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'requested'
                  CHECK (status IN ('requested', 'running', 'succeeded', 'refused', 'failed')),
    started_at    timestamptz,
    finished_at   timestamptz,
    -- Records that were held and are now in the tenant: distinct, newly inserted.
    moved         integer CHECK (moved >= 0),
    pages         integer CHECK (pages >= 0),
    outcome       text CHECK (length(outcome) <= 200),  -- our own code, never payload text
    FOREIGN KEY (proposal_id, tenant_id)
        REFERENCES cloudops.alias_proposal (id, tenant_id) ON DELETE CASCADE,
    CHECK ((status IN ('requested', 'running')) = (finished_at IS NULL)),
    CHECK (status <> 'succeeded' OR moved IS NOT NULL)
);

ALTER TABLE cloudops.alias_reprocess ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.alias_reprocess FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.alias_reprocess
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY staff_only ON cloudops.alias_reprocess AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') NOT IN ('', 'customer'));
-- The worker claims the next request before it knows the tenant: through
-- cloudops.claim_reprocess only, whose owner no login can become, and only a
-- waiting request, which it can move to running and nothing else.
CREATE POLICY reprocess_queue_read ON cloudops.alias_reprocess FOR SELECT
  TO cloudops_access_admin USING (status IN ('requested', 'running'));
CREATE POLICY reprocess_queue_claim ON cloudops.alias_reprocess FOR UPDATE
  TO cloudops_access_admin USING (status = 'requested') WITH CHECK (status = 'running');

-- A request is history: what it asked for never changes, and its status only
-- moves forward, requested -> running -> succeeded | refused | failed.
CREATE FUNCTION cloudops.check_reprocess_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.proposal_id, NEW.source_system, NEW.alias,
        NEW.window_start, NEW.window_end, NEW.requested_by, NEW.requested_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.proposal_id, OLD.source_system, OLD.alias,
        OLD.window_start, OLD.window_end, OLD.requested_by, OLD.requested_at)
       OR NOT (OLD.status = 'requested' AND NEW.status = 'running'
               OR OLD.status = 'running' AND NEW.status IN ('succeeded', 'refused', 'failed')) THEN
        RAISE EXCEPTION 're-process requests only move forward'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER alias_reprocess_forward_only
    BEFORE UPDATE ON cloudops.alias_reprocess
    FOR EACH ROW EXECUTE FUNCTION cloudops.check_reprocess_transition();
CREATE TRIGGER alias_reprocess_no_delete
    BEFORE DELETE ON cloudops.alias_reprocess
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();


INSERT INTO access.permission (name, description, is_commercial)
VALUES ('mapping.manage', 'Onboard customers and map source names to them', false);
INSERT INTO access.role_definition (name, role_class, is_system, description)
VALUES ('Customer master', 'delivery_lead', true,
        'Delivery: the customer master and its alias map (HLD M1)');
INSERT INTO access.role_permission (role_id, permission)
SELECT r.id, p FROM access.role_definition r,
       unnest(ARRAY['mapping.manage', 'tickets.read', 'inventory.read']) p
WHERE r.name = 'Customer master' AND r.is_system;
INSERT INTO access.role_permission (role_id, permission)
SELECT r.id, 'mapping.manage' FROM access.role_definition r
WHERE r.name = 'Platform administrator' AND r.is_system;
"""

FUNCTIONS = """
-- The actor holds mapping.manage as staff and, when a tenant is named, is
-- assigned it and has it in this transaction's scope. Assignment is read from
-- the access tables, scope from the transaction: both must agree.
CREATE FUNCTION access.require_mapper(p_actor uuid, p_tenant uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class <> 'customer' AND rp.permission = 'mapping.manage'
    ) THEN
        RAISE EXCEPTION 'not permitted to maintain the customer master'
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
        -- Indistinguishable from a tenant that does not exist.
        RAISE EXCEPTION 'tenant not found' USING ERRCODE = 'CMSCP';
    END IF;
END $$;

CREATE FUNCTION access.mapping_approvers() RETURNS smallint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT approvers FROM access.mapping_policy
$$;

-- Quarantined names, most sighted first. The suggestion is the stored one if it
-- is in the caller's scope, else the one tenant in scope whose name or alias,
-- normalised, contains or is contained in the name's. A hint for a person,
-- never applied by itself.
CREATE FUNCTION access.alias_quarantine(p_actor uuid)
RETURNS TABLE (id bigint, source_system text, alias text, occurrence_count bigint,
               first_seen timestamptz, last_seen timestamptz,
               suggested_tenant_id uuid, pending_proposal_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    PERFORM access.require_mapper(p_actor, NULL);
    RETURN QUERY
    WITH named AS (
        SELECT t.id AS tenant_id, cloudops.normalise_company(t.canonical_name) AS norm
        FROM cloudops.tenant t
        UNION
        SELECT a.tenant_id, cloudops.normalise_company(a.alias) FROM cloudops.tenant_alias a
    )
    SELECT q.id, q.source_system, q.alias, q.occurrence_count, q.first_seen, q.last_seen,
           coalesce(
               CASE WHEN q.suggested_tenant_id = ANY(cloudops.current_tenant_ids())
                    THEN q.suggested_tenant_id END,
               (SELECT CASE WHEN count(DISTINCT n.tenant_id) = 1
                            THEN (array_agg(n.tenant_id))[1] END
                FROM named n, LATERAL (SELECT cloudops.normalise_company(q.alias) AS qn) x
                WHERE length(n.norm) >= 3 AND length(x.qn) >= 3
                  AND (strpos(x.qn, n.norm) > 0 OR strpos(n.norm, x.qn) > 0))),
           (SELECT p.id FROM cloudops.alias_proposal p
            WHERE p.source_system = q.source_system AND p.alias = q.alias
              AND NOT EXISTS (SELECT 1 FROM cloudops.alias_decision d
                              WHERE d.proposal_id = p.id)
            ORDER BY p.proposed_at LIMIT 1)
    FROM staging.tenant_alias_quarantine q
    ORDER BY q.occurrence_count DESC, q.last_seen DESC, q.id;
END $$;

-- ADR 0002: onboarding gives every all-tenant principal access at once (D9).
CREATE FUNCTION access.onboard_tenant(
    p_actor uuid, p_canonical_name text, p_short_code text
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_id uuid := gen_random_uuid();
    v_name text := btrim(coalesce(p_canonical_name, ''));
BEGIN
    PERFORM access.require_mapper(p_actor, NULL);
    IF length(v_name) = 0 OR length(v_name) > 200
       OR coalesce(p_short_code, '') !~ '^[A-Z0-9]{2,10}$' THEN
        RAISE EXCEPTION 'a tenant needs a name and a short code of 2 to 10 capitals or digits'
            USING ERRCODE = 'check_violation';
    END IF;
    BEGIN
        INSERT INTO cloudops.tenant (id, canonical_name, short_code)
        VALUES (v_id, v_name, p_short_code);
    EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION 'a tenant with that name or short code exists' USING ERRCODE = 'CMDUP';
    END;
    PERFORM access.log_change(p_actor::text, 'onboard_tenant', jsonb_build_object(
        'tenant_id', v_id, 'short_code', p_short_code));
    RETURN v_id;
END $$;

CREATE FUNCTION access.propose_alias(
    p_actor uuid, p_source text, p_alias text, p_tenant uuid, p_note text
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_id uuid := gen_random_uuid();
BEGIN
    PERFORM access.require_mapper(p_actor, p_tenant);
    -- One open proposal per spelling at a time, across concurrent requests.
    PERFORM pg_advisory_xact_lock(hashtext('access.alias:' || p_source || ':' || p_alias));
    IF EXISTS (SELECT 1 FROM cloudops.tenant_alias a
               WHERE a.source_system = p_source AND a.alias = p_alias)
       OR EXISTS (SELECT 1 FROM cloudops.alias_proposal p
                  WHERE p.source_system = p_source AND p.alias = p_alias
                    AND NOT EXISTS (SELECT 1 FROM cloudops.alias_decision d
                                    WHERE d.proposal_id = p.id)) THEN
        RAISE EXCEPTION 'that name is already mapped or awaiting approval'
            USING ERRCODE = 'CMDUP';
    END IF;
    INSERT INTO cloudops.alias_proposal (id, tenant_id, source_system, alias, note, proposed_by)
    VALUES (v_id, p_tenant, p_source, p_alias, nullif(btrim(coalesce(p_note, '')), ''), p_actor);
    PERFORM access.log_change(p_actor::text, 'propose_alias', jsonb_build_object(
        'proposal_id', v_id, 'tenant_id', p_tenant, 'source_system', p_source));
    RETURN v_id;
END $$;

CREATE FUNCTION access.decide_alias(
    p_actor uuid, p_proposal uuid, p_decision text, p_reason text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_p cloudops.alias_proposal;
    v_q staging.tenant_alias_quarantine;
BEGIN
    -- Under the caller's scope: another tenant's proposal is simply not found.
    SELECT * INTO v_p FROM cloudops.alias_proposal WHERE id = p_proposal;
    IF NOT FOUND THEN
        PERFORM access.require_mapper(p_actor, NULL);
        RAISE EXCEPTION 'proposal not found' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM access.require_mapper(p_actor, v_p.tenant_id);
    PERFORM pg_advisory_xact_lock(
        hashtext('access.alias:' || v_p.source_system || ':' || v_p.alias));
    IF EXISTS (SELECT 1 FROM cloudops.alias_decision d WHERE d.proposal_id = p_proposal) THEN
        RAISE EXCEPTION 'already decided' USING ERRCODE = 'CMDUP';
    END IF;
    IF p_decision = 'approve' THEN
        SELECT * INTO v_q FROM staging.tenant_alias_quarantine q
        WHERE q.source_system = v_p.source_system AND q.alias = v_p.alias FOR UPDATE;
    END IF;
    -- The two-person rule is checked by this insert's trigger.
    INSERT INTO cloudops.alias_decision (proposal_id, tenant_id, decision, reason, decided_by,
                                         held_occurrences, held_first_seen, held_last_seen)
    VALUES (p_proposal, v_p.tenant_id, p_decision,
            nullif(btrim(coalesce(p_reason, '')), ''), p_actor,
            v_q.occurrence_count, v_q.first_seen, v_q.last_seen);
    IF p_decision = 'approve' THEN
        BEGIN
            INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias, confidence,
                                               confirmed_by, confirmed_at)
            VALUES (v_p.tenant_id, v_p.source_system, v_p.alias, 1.00, p_actor::text, now());
        EXCEPTION WHEN unique_violation THEN
            -- Mapped since it was proposed, possibly to a tenant outside this scope.
            RAISE EXCEPTION 'that name is already mapped' USING ERRCODE = 'CMDUP';
        END;
        DELETE FROM staging.tenant_alias_quarantine q WHERE q.id = v_q.id;
    END IF;
    PERFORM access.log_change(p_actor::text, p_decision || '_alias', jsonb_build_object(
        'proposal_id', p_proposal, 'tenant_id', v_p.tenant_id,
        'source_system', v_p.source_system, 'held_occurrences', v_q.occurrence_count));
END $$;
"""

REPROCESS = """
-- Ask for the records held under an approved name to be re-processed. Refused
-- unless the proposal was approved, something was held, the name still maps to
-- that tenant, and no request for it is waiting or running. The window is the
-- held sightings' days, from the day before the first (a page fetched before
-- midnight may be processed after it) to the last.
CREATE FUNCTION access.request_reprocess(p_actor uuid, p_proposal uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_p cloudops.alias_proposal;
    v_d cloudops.alias_decision;
    v_id uuid := gen_random_uuid();
BEGIN
    SELECT * INTO v_p FROM cloudops.alias_proposal WHERE id = p_proposal;
    IF NOT FOUND THEN
        PERFORM access.require_mapper(p_actor, NULL);
        RAISE EXCEPTION 'proposal not found' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM access.require_mapper(p_actor, v_p.tenant_id);
    PERFORM pg_advisory_xact_lock(hashtext('access.reprocess:' || p_proposal::text));
    SELECT * INTO v_d FROM cloudops.alias_decision WHERE proposal_id = p_proposal;
    IF NOT FOUND OR v_d.decision <> 'approve' THEN
        RAISE EXCEPTION 'only an approved mapping is re-processed' USING ERRCODE = 'CMNOT';
    END IF;
    IF v_d.held_occurrences IS NULL THEN
        RAISE EXCEPTION 'nothing was held under that name' USING ERRCODE = 'CMNIL';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM cloudops.tenant_alias a
                   WHERE a.source_system = v_p.source_system AND a.alias = v_p.alias
                     AND a.tenant_id = v_p.tenant_id) THEN
        RAISE EXCEPTION 'that name no longer maps to this tenant' USING ERRCODE = 'CMSTL';
    END IF;
    IF EXISTS (SELECT 1 FROM cloudops.alias_reprocess r
               WHERE r.proposal_id = p_proposal AND r.status IN ('requested', 'running')) THEN
        RAISE EXCEPTION 'a re-process of that name is already waiting or running'
            USING ERRCODE = 'CMDUP';
    END IF;
    INSERT INTO cloudops.alias_reprocess (id, tenant_id, proposal_id, source_system, alias,
                                          window_start, window_end, requested_by)
    VALUES (v_id, v_p.tenant_id, p_proposal, v_p.source_system, v_p.alias,
            (v_d.held_first_seen AT TIME ZONE 'UTC')::date - 1,
            (v_d.held_last_seen AT TIME ZONE 'UTC')::date, p_actor);
    PERFORM access.log_change(p_actor::text, 'request_reprocess', jsonb_build_object(
        'request_id', v_id, 'proposal_id', p_proposal, 'tenant_id', v_p.tenant_id));
    RETURN v_id;
END $$;

-- The connector worker's side, under the ingest login. Claim the oldest waiting
-- request for a source (none: no row); the worker then re-checks it under that
-- tenant's scope and reports how it ended with finish_reprocess.
CREATE FUNCTION cloudops.claim_reprocess(p_source text)
RETURNS TABLE (id uuid, tenant_id uuid, proposal_id uuid, alias text,
               window_start date, window_end date)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    UPDATE cloudops.alias_reprocess r SET status = 'running', started_at = now()
    WHERE r.id = (SELECT q.id FROM cloudops.alias_reprocess q
                  WHERE q.status = 'requested' AND q.source_system = p_source
                  ORDER BY q.requested_at LIMIT 1 FOR UPDATE SKIP LOCKED)
    RETURNING r.id, r.tenant_id, r.proposal_id, r.alias, r.window_start, r.window_end;
END $$;

-- Under the request's tenant scope: a request outside it is not found.
CREATE FUNCTION cloudops.finish_reprocess(
    p_id uuid, p_status text, p_moved integer, p_pages integer, p_outcome text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    UPDATE cloudops.alias_reprocess
    SET status = p_status, finished_at = now(), moved = p_moved, pages = p_pages,
        outcome = p_outcome
    WHERE id = p_id AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no running re-process with that id' USING ERRCODE = 'no_data_found';
    END IF;
END $$;
"""

READERS = ("mapping_approvers()",)
WRITERS = (
    "require_mapper(uuid, uuid)",
    "alias_quarantine(uuid)",
    "onboard_tenant(uuid, text, text)",
    "propose_alias(uuid, text, text, uuid, text)",
    "decide_alias(uuid, uuid, text, text)",
    "request_reprocess(uuid, uuid)",
)
# Owned by the access-admin role too, but in schema cloudops and for ingest only.
WORKER = ("claim_reprocess(text)", "finish_reprocess(uuid, text, integer, integer, text)")
APP_CALLABLE = tuple(f for f in (*READERS, *WRITERS) if not f.startswith("require"))

PRIVILEGES = """
REVOKE ALL ON FUNCTION access.require_mapper(uuid, uuid), access.mapping_approvers(),
    access.alias_quarantine(uuid), access.onboard_tenant(uuid, text, text),
    access.propose_alias(uuid, text, text, uuid, text),
    access.decide_alias(uuid, uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION cloudops.check_alias_decision(), access.reject_customer_mapping(),
    cloudops.check_reprocess_transition(), access.request_reprocess(uuid, uuid),
    cloudops.claim_reprocess(text),
    cloudops.finish_reprocess(uuid, text, integer, integer, text) FROM PUBLIC;

-- A tenant or an alias exists only through these functions now.
REVOKE INSERT, UPDATE ON cloudops.tenant, cloudops.tenant_alias FROM cloudops_app;
GRANT SELECT ON cloudops.alias_proposal, cloudops.alias_decision, cloudops.alias_reprocess
    TO cloudops_app;
-- The worker re-checks a request under its tenant's scope before running it.
GRANT SELECT ON cloudops.alias_decision, cloudops.alias_reprocess TO cloudops_ingest;
GRANT SELECT, INSERT, UPDATE ON cloudops.alias_reprocess TO cloudops_access_admin;

GRANT SELECT ON access.mapping_policy TO cloudops_resolver, cloudops_access_admin;
GRANT SELECT, INSERT ON cloudops.alias_proposal, cloudops.alias_decision
    TO cloudops_access_admin;
GRANT SELECT, INSERT ON cloudops.tenant, cloudops.tenant_alias TO cloudops_access_admin;
GRANT USAGE ON SEQUENCE cloudops.tenant_alias_id_seq TO cloudops_access_admin;
GRANT USAGE ON SCHEMA staging TO cloudops_access_admin;
GRANT SELECT, UPDATE, DELETE ON staging.tenant_alias_quarantine TO cloudops_access_admin;
GRANT SELECT ON staging.legal_suffix TO cloudops_access_admin;
-- Onboarding writes a tenant no scope holds yet. Insert only, and only for the
-- function owner, which no login can become.
CREATE POLICY tenant_onboarding ON cloudops.tenant FOR INSERT
    TO cloudops_access_admin WITH CHECK (true);
"""


def _ownership() -> str:
    lines = [f"GRANT EXECUTE ON FUNCTION access.{fn} TO cloudops_app;" for fn in APP_CALLABLE]
    lines += [f"GRANT EXECUTE ON FUNCTION cloudops.{fn} TO cloudops_ingest;" for fn in WORKER]
    lines.append("GRANT EXECUTE ON FUNCTION access.require_mapper(uuid, uuid)"
                 " TO cloudops_access_admin;")
    # Grants first: once ownership moves, the migrator can no longer grant.
    lines.append("GRANT CREATE ON SCHEMA access TO cloudops_resolver, cloudops_access_admin;")
    for fn in READERS:
        lines.append(f"ALTER FUNCTION access.{fn} OWNER TO cloudops_resolver;")
    for fn in WRITERS:
        lines.append(f"ALTER FUNCTION access.{fn} OWNER TO cloudops_access_admin;")
    lines.append("REVOKE CREATE ON SCHEMA access FROM cloudops_resolver, cloudops_access_admin;")
    lines.append("GRANT CREATE ON SCHEMA cloudops TO cloudops_access_admin;")
    for fn in WORKER:
        lines.append(f"ALTER FUNCTION cloudops.{fn} OWNER TO cloudops_access_admin;")
    lines.append("REVOKE CREATE ON SCHEMA cloudops FROM cloudops_access_admin;")
    return "\n".join(lines)


CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS alias_proposal_tenant_idx"
    " ON cloudops.alias_proposal (tenant_id, proposed_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS alias_proposal_source_alias_idx"
    " ON cloudops.alias_proposal (source_system, alias)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS alias_decision_tenant_idx"
    " ON cloudops.alias_decision (tenant_id, decided_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS alias_reprocess_proposal_idx"
    " ON cloudops.alias_reprocess (proposal_id, requested_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS alias_reprocess_queue_idx"
    " ON cloudops.alias_reprocess (source_system, requested_at) WHERE status = 'requested'",
)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(FUNCTIONS)
    op.execute(REPROCESS)
    op.execute(PRIVILEGES)
    op.execute(_ownership())
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
