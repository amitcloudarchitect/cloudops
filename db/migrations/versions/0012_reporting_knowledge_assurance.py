"""Reporting, knowledge and assurance (expand).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-31

Step 17, ADR 0019. HLD modules M14, M15, M19 and M20.

**Knowledge (M20).** ``knowledge_chunk`` gains its citation: the ``document``
it came from, its ``ordinal`` in that document and the ``page`` it starts on,
and ``embedded_at``. SharePoint writes chunks without an embedding, as before;
the indexer embeds them under the chunk's own tenant scope, and only fills an
embedding that is still empty. ``document`` gains ``doc_kind`` (design,
handover, policy, runbook, report, other: from its name and folder, never from
what it says) and ``folder_path``. Staleness and gap analysis are queries over
these columns, not stored verdicts.

The indexer runs in the AI worker as the application login, one tenant's
scope at a time. ``cloudops.knowledge_tenants()`` gives it the tenant ids to
visit (ids only, through the resolver's existing registry policy), as
``access.resolve_principal`` does for an all-tenants principal.

**Assurance (M14, M15).**

- ``control``: the control library. Reference data with no tenant, like
  ``fx_rate``. The application reads it; only ``access.load_controls`` writes
  it, for a principal holding ``assurance.library``. The library itself (the
  98 controls) is supplied by Security; none is shipped here.
- ``assessment`` and ``control_response``: tenant tables. A response's
  ``result`` exists only with an approval (``approved_by`` and
  ``approved_at``), and a trigger refuses an approval unless the approver is
  an active staff principal holding ``assurance.approve``. A model's draft
  lives in ``draft_*`` columns, and nothing a draft says can set ``result``.
- ``evidence``: the evidence store. The query, its parameters, the raw value,
  the retrieval timestamp and a digest. Append-only for everyone, the owner
  included (a trigger refuses UPDATE and DELETE).
- ``evidence_pack``: each signed export, with its manifest. Append-only too.

**Reporting (M19).**

- ``report_pack``: one generated pack per customer and month (re-generation
  makes a new version and supersedes the old). The sections are the generated
  data snapshot, in the contract currency recorded on the pack.
- ``report_narrative``: the AI draft of a summary section, and the text a
  person wrote or accepted. A pack cannot be approved or published while any
  narrative lacks a person's text, and only an active staff principal holding
  ``reports.publish`` can approve or publish one: nothing is auto-published.
- A customer role sees published packs only (a restrictive policy), so the
  customer portal (step 18) cannot show a draft by mistake.

**The drafting queue.** ``cloudops.pending_drafts(limit)`` lists drafts that
have been requested, across tenants, for the drafting worker to claim: id and
tenant only, through a ``SECURITY DEFINER`` function owned by
``cloudops_resolver`` (the ADR 0005 pattern). The worker then claims and
drafts each one under that one tenant's scope.
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

PERMISSIONS = (
    ("reports.read", "Monthly report packs"),
    ("reports.manage", "Generate report packs and edit their narrative"),
    ("reports.publish", "Approve and publish a report pack"),
    ("assurance.read", "Assessments, control responses and evidence"),
    ("assurance.manage", "Run assessments, collect evidence, request and edit drafts"),
    ("assurance.approve", "Approve a control response so that it scores"),
    ("assurance.library", "Load the control library"),
)
# The default matrix (ADR 0019), by system role name: a role class is shared by
# several roles (Customer master is a delivery_lead class), and they differ.
# The product owner refines it in the console. No customer role gets any of
# these yet: the portal decides.
GRANTS = {
    "reports.read": ("CEO", "CFO", "Delivery lead", "Commercial", "Auditor",
                     "Platform administrator"),
    "reports.manage": ("Delivery lead", "Platform administrator"),
    "reports.publish": ("Delivery lead", "Platform administrator"),
    "assurance.read": ("CEO", "SecOps", "Delivery lead", "Auditor", "Platform administrator"),
    "assurance.manage": ("SecOps", "Delivery lead", "Platform administrator"),
    "assurance.approve": ("SecOps", "Delivery lead", "Platform administrator"),
    "assurance.library": ("Platform administrator",),
}

DDL = """
-- Knowledge: citations and embedding state ------------------------------------

ALTER TABLE cloudops.document ADD COLUMN doc_kind text NOT NULL DEFAULT 'other'
    CHECK (doc_kind IN ('design', 'handover', 'policy', 'runbook', 'report', 'other'));
ALTER TABLE cloudops.document ADD COLUMN folder_path text;

ALTER TABLE cloudops.knowledge_chunk
    ADD COLUMN document_id uuid REFERENCES cloudops.document(id) ON DELETE CASCADE;
ALTER TABLE cloudops.knowledge_chunk ADD COLUMN ordinal integer CHECK (ordinal >= 0);
ALTER TABLE cloudops.knowledge_chunk ADD COLUMN page integer CHECK (page >= 1);
ALTER TABLE cloudops.knowledge_chunk ADD COLUMN embedded_at timestamptz;

-- Assurance --------------------------------------------------------------------

-- The library: reference data, no tenant. Loaded through access.load_controls.
CREATE TABLE cloudops.control (
    code            text PRIMARY KEY CHECK (code ~ '^[A-Z0-9][A-Z0-9._-]{1,39}$'),
    framework       text NOT NULL CHECK (framework ~ '^[a-z0-9_]{1,40}$'),
    domain          text NOT NULL CHECK (length(domain) BETWEEN 1 AND 120),
    title           text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    requirement     text NOT NULL CHECK (length(requirement) BETWEEN 1 AND 4000),
    assessment      text NOT NULL CHECK (assessment IN ('automated', 'narrative', 'manual')),
    evidence_source text NOT NULL CHECK (evidence_source IN
                        ('azure', 'aws', 'servicenow', 'qualys', 'corestack', 'entra',
                         'sharepoint', 'policy_library', 'human')),
    evidence_query  text CHECK (evidence_query ~ '^[a-z][a-z0-9_.]{1,80}$'),
    parameters      jsonb NOT NULL DEFAULT '{}'::jsonb,
    library_version text NOT NULL CHECK (length(library_version) BETWEEN 1 AND 40),
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    retired_at      timestamptz,
    -- Evidence routing: an automated control names the query that answers it;
    -- a manual one is answered by a person and nothing else.
    CHECK ((assessment = 'automated') = (evidence_query IS NOT NULL)),
    CHECK (assessment <> 'manual' OR evidence_source = 'human'),
    CHECK (jsonb_typeof(parameters) = 'object')
);

CREATE TABLE cloudops.assessment (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    framework       text NOT NULL,
    library_version text NOT NULL,
    title           text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_by      uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    CHECK ((status = 'closed') = (closed_at IS NOT NULL))
);

CREATE TABLE cloudops.control_response (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    assessment_id     uuid NOT NULL REFERENCES cloudops.assessment(id) ON DELETE CASCADE,
    control_code      text NOT NULL REFERENCES cloudops.control(code),
    -- From an automated control's evidence, by a fixed rule: a proposal only.
    proposed_result   text CHECK (proposed_result IN
                          ('compliant', 'partially_compliant', 'non_compliant')),
    proposed_basis    text,
    -- The model's draft for a narrative control. It has no result to propose.
    draft_state       text NOT NULL DEFAULT 'none' CHECK (draft_state IN
                          ('none', 'requested', 'drafting', 'drafted', 'declined', 'failed')),
    draft_narrative   text,
    draft_citations   jsonb,
    draft_sufficiency text CHECK (draft_sufficiency IN ('sufficient', 'partial', 'insufficient')),
    draft_run_id      uuid,
    drafted_at        timestamptz,
    draft_note        text CHECK (length(draft_note) <= 200),  -- why a draft was declined
    -- What a person wrote or accepted, and the result they approved.
    narrative         text CHECK (length(narrative) <= 8000),
    citations         jsonb NOT NULL DEFAULT '[]'::jsonb,
    result            text CHECK (result IN ('compliant', 'partially_compliant', 'non_compliant',
                                             'not_applicable', 'manual')),
    approved_by       uuid,
    approved_at       timestamptz,
    approval_note     text CHECK (length(approval_note) <= 2000),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (assessment_id, control_code),
    -- No result without an approval, no approval without an approver.
    CHECK ((result IS NULL) = (approved_at IS NULL)),
    CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CHECK (jsonb_typeof(citations) = 'array')
);

-- The evidence store: what was asked, of which system, what came back, when.
CREATE TABLE cloudops.evidence (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES cloudops.tenant(id),
    assessment_id  uuid NOT NULL REFERENCES cloudops.assessment(id),
    control_code   text NOT NULL REFERENCES cloudops.control(code),
    source_system  text NOT NULL,
    query_name     text NOT NULL,
    query_ref      text NOT NULL,
    query_text     text NOT NULL,
    parameters     jsonb NOT NULL,
    raw_value      jsonb NOT NULL,
    retrieved_at   timestamptz NOT NULL,
    retrieved_by   text NOT NULL,
    sha256         char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE cloudops.evidence_pack (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES cloudops.tenant(id),
    assessment_id   uuid NOT NULL REFERENCES cloudops.assessment(id),
    framework       text NOT NULL,
    manifest        jsonb NOT NULL,
    manifest_sha256 char(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    signature       text NOT NULL,
    signature_alg   text NOT NULL,
    key_id          text NOT NULL,
    created_by      uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Reporting ----------------------------------------------------------------------

CREATE TABLE cloudops.report_pack (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    period            date NOT NULL CHECK (extract(day FROM period) = 1),
    version           integer NOT NULL CHECK (version >= 1),
    contract_currency char(3) NOT NULL CHECK (contract_currency ~ '^[A-Z]{3}$'),
    status            text NOT NULL DEFAULT 'draft' CHECK (status IN
                          ('draft', 'approved', 'published', 'superseded')),
    sections          jsonb NOT NULL CHECK (jsonb_typeof(sections) = 'object'),
    data_as_of        timestamptz NOT NULL,
    generated_by      uuid NOT NULL,
    generated_at      timestamptz NOT NULL DEFAULT now(),
    approved_by       uuid,
    approved_at       timestamptz,
    published_by      uuid,
    published_at      timestamptz,
    UNIQUE (tenant_id, period, version),
    CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CHECK ((published_by IS NULL) = (published_at IS NULL)),
    CHECK (status NOT IN ('approved', 'published') OR approved_at IS NOT NULL),
    -- Published means a publisher; a superseded pack keeps the record that it was.
    CHECK (status <> 'published' OR published_at IS NOT NULL),
    CHECK (published_at IS NULL OR status IN ('published', 'superseded'))
);

CREATE TABLE cloudops.report_narrative (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    pack_id      uuid NOT NULL REFERENCES cloudops.report_pack(id) ON DELETE CASCADE,
    section      text NOT NULL CHECK (section ~ '^[a-z_]{1,40}$'),
    draft_state  text NOT NULL DEFAULT 'requested' CHECK (draft_state IN
                     ('none', 'requested', 'drafting', 'drafted', 'declined', 'failed')),
    draft_text   text,
    draft_run_id uuid,
    drafted_at   timestamptz,
    draft_note   text CHECK (length(draft_note) <= 200),
    text         text CHECK (length(text) <= 6000),
    edited_by    uuid,
    edited_at    timestamptz,
    UNIQUE (pack_id, section),
    CHECK ((text IS NULL) = (edited_by IS NULL)),
    CHECK ((edited_by IS NULL) = (edited_at IS NULL))
);
"""

TENANT_TABLES = (
    "cloudops.assessment",
    "cloudops.control_response",
    "cloudops.evidence",
    "cloudops.evidence_pack",
    "cloudops.report_pack",
    "cloudops.report_narrative",
)


def _rls(table: str) -> str:
    # FORCE, not just ENABLE: without it the owner bypasses every policy.
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table}
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
"""


SECURITY = "".join(_rls(t) for t in TENANT_TABLES) + """
-- Assessments, evidence and packs are tenant-wide: a subscription-limited
-- scope (ADR 0002, D16) sees none of them, because an assessment answers for
-- the whole estate. Report packs likewise.
CREATE POLICY account_scope ON cloudops.assessment AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.control_response AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.evidence AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.evidence_pack AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.report_pack AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.report_narrative AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());

-- A customer sees a pack only once a person has published it, and never a
-- narrative of an unpublished pack. Enforced here, not by serialisation. It
-- binds the application login and cloudops_customer (what a customer scope runs
-- as, migration 0011). cloudops_customer is granted nothing here: customers fail
-- closed until the portal grants report columns, and then see published packs only.
CREATE POLICY published_only_for_customers ON cloudops.report_pack AS RESTRICTIVE
  TO cloudops_app, cloudops_customer
  USING (current_setting('app.role', true) IS DISTINCT FROM 'customer'
         OR status = 'published');
CREATE POLICY published_only_for_customers ON cloudops.report_narrative AS RESTRICTIVE
  TO cloudops_app, cloudops_customer
  USING (current_setting('app.role', true) IS DISTINCT FROM 'customer'
         OR EXISTS (SELECT 1 FROM cloudops.report_pack p
                    WHERE p.id = pack_id AND p.status = 'published'));

GRANT SELECT ON cloudops.control TO cloudops_app;
GRANT SELECT, INSERT, UPDATE ON cloudops.assessment, cloudops.control_response,
    cloudops.report_pack, cloudops.report_narrative TO cloudops_app;
-- Evidence and packs are append-only (the trigger below holds even for the owner).
GRANT SELECT, INSERT ON cloudops.evidence, cloudops.evidence_pack TO cloudops_app;
"""

TRIGGERS = """
CREATE FUNCTION cloudops.refuse_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER evidence_append_only BEFORE UPDATE OR DELETE ON cloudops.evidence
    FOR EACH ROW EXECUTE FUNCTION cloudops.refuse_mutation();
CREATE TRIGGER evidence_pack_append_only BEFORE UPDATE OR DELETE ON cloudops.evidence_pack
    FOR EACH ROW EXECUTE FUNCTION cloudops.refuse_mutation();
CREATE TRIGGER evidence_no_truncate BEFORE TRUNCATE ON cloudops.evidence
    FOR EACH STATEMENT EXECUTE FUNCTION cloudops.refuse_mutation();
CREATE TRIGGER evidence_pack_no_truncate BEFORE TRUNCATE ON cloudops.evidence_pack
    FOR EACH STATEMENT EXECUTE FUNCTION cloudops.refuse_mutation();

-- A result scores only with a person's approval: an active staff principal
-- holding assurance.approve. Nothing a model or a service writes can pass.
CREATE FUNCTION cloudops.check_control_approval() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.approved_by IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.approved_by IS DISTINCT FROM OLD.approved_by
            OR NEW.result IS DISTINCT FROM OLD.result
            OR NEW.approved_at IS DISTINCT FROM OLD.approved_at)
       AND NOT access.holds_staff_permission(NEW.approved_by, 'assurance.approve') THEN
        RAISE EXCEPTION 'approver does not hold assurance.approve'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER control_response_approval BEFORE INSERT OR UPDATE ON cloudops.control_response
    FOR EACH ROW EXECUTE FUNCTION cloudops.check_control_approval();

-- Never auto-published: approval and publication need a staff principal
-- holding reports.publish, and every narrative needs a person's text first.
CREATE FUNCTION cloudops.check_pack_release() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.approved_by IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.approved_by IS DISTINCT FROM OLD.approved_by)
       AND NOT access.holds_staff_permission(NEW.approved_by, 'reports.publish') THEN
        RAISE EXCEPTION 'approver does not hold reports.publish'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.published_by IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.published_by IS DISTINCT FROM OLD.published_by)
       AND NOT access.holds_staff_permission(NEW.published_by, 'reports.publish') THEN
        RAISE EXCEPTION 'publisher does not hold reports.publish'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status IN ('approved', 'published') AND EXISTS (
        SELECT 1 FROM cloudops.report_narrative n
        WHERE n.pack_id = NEW.id AND n.edited_by IS NULL) THEN
        RAISE EXCEPTION 'every narrative needs a person''s text before release'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER report_pack_release BEFORE INSERT OR UPDATE ON cloudops.report_pack
    FOR EACH ROW EXECUTE FUNCTION cloudops.check_pack_release();
"""

PERMISSION_CHECK = """
-- Whether a principal is active staff holding a permission. A boolean only:
-- triggers ask it, and it reveals nothing else about the matrix.
CREATE FUNCTION access.holds_staff_permission(p_oid uuid, p_permission text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_oid AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class <> 'customer' AND rp.permission = p_permission)
$$;
"""

FUNCTIONS = """
-- Load (or reload) one framework's control library. The payload is validated
-- by the caller (cloudops_domain.assurance.library) and again by the table's
-- constraints. Controls of the framework absent from the payload are retired,
-- never deleted: responses and evidence keep pointing at them.
CREATE FUNCTION access.load_controls(p_actor uuid, p_framework text, p_version text,
                                     p_controls jsonb)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_count integer;
BEGIN
    IF NOT access.holds_staff_permission(p_actor, 'assurance.library') THEN
        RAISE EXCEPTION 'not permitted to load the control library'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF jsonb_typeof(p_controls) <> 'array' OR jsonb_array_length(p_controls) = 0 THEN
        RAISE EXCEPTION 'controls must be a non-empty array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    INSERT INTO cloudops.control (code, framework, domain, title, requirement, assessment,
                                  evidence_source, evidence_query, parameters,
                                  library_version)
    SELECT c->>'code', p_framework, c->>'domain', c->>'title', c->>'requirement',
           c->>'assessment', c->>'evidence_source', c->>'evidence_query',
           coalesce(c->'parameters', '{}'::jsonb), p_version
    FROM jsonb_array_elements(p_controls) c
    ON CONFLICT (code) DO UPDATE SET
        framework = EXCLUDED.framework, domain = EXCLUDED.domain, title = EXCLUDED.title,
        requirement = EXCLUDED.requirement, assessment = EXCLUDED.assessment,
        evidence_source = EXCLUDED.evidence_source, evidence_query = EXCLUDED.evidence_query,
        parameters = EXCLUDED.parameters, library_version = EXCLUDED.library_version,
        loaded_at = now(), retired_at = NULL;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    UPDATE cloudops.control SET retired_at = now()
     WHERE framework = p_framework AND retired_at IS NULL
       AND code NOT IN (SELECT c->>'code' FROM jsonb_array_elements(p_controls) c);
    PERFORM access.log_change(p_actor::text, 'load_controls',
        jsonb_build_object('framework', p_framework, 'version', p_version,
                           'controls', v_count));
    RETURN v_count;
END $$;

-- The drafting queue across tenants: requested drafts, id and tenant only.
CREATE POLICY draft_queue_read ON cloudops.control_response FOR SELECT
    TO cloudops_resolver USING (draft_state = 'requested');
CREATE POLICY draft_queue_read ON cloudops.report_narrative FOR SELECT
    TO cloudops_resolver USING (draft_state = 'requested');
GRANT SELECT (id, tenant_id, draft_state) ON cloudops.control_response,
    cloudops.report_narrative TO cloudops_resolver;

-- The tenants the knowledge indexer visits, one scope at a time. Ids only.
CREATE FUNCTION cloudops.knowledge_tenants()
RETURNS TABLE (tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT t.id FROM cloudops.tenant t ORDER BY t.id
$$;

CREATE FUNCTION cloudops.pending_drafts(p_limit integer)
RETURNS TABLE (kind text, id uuid, tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT q.kind, q.id, q.tenant_id FROM (
        SELECT 'control_response'::text AS kind, r.id, r.tenant_id
        FROM cloudops.control_response r WHERE r.draft_state = 'requested'
        UNION ALL
        SELECT 'report_narrative'::text, n.id, n.tenant_id
        FROM cloudops.report_narrative n WHERE n.draft_state = 'requested') q
    ORDER BY q.kind, q.id
    LIMIT least(greatest(p_limit, 1), 500)
$$;
"""


def _seed() -> str:
    rows = [
        "INSERT INTO access.permission (name, description, is_commercial) VALUES "
        + ", ".join(f"('{n}', '{d}', false)" for n, d in PERMISSIONS) + ";",
    ]
    for permission, roles in GRANTS.items():
        listed = ", ".join(f"'{r}'" for r in roles)
        rows.append(
            "INSERT INTO access.role_permission (role_id, permission)"
            f" SELECT r.id, '{permission}' FROM access.role_definition r"
            f" WHERE r.is_system AND r.name IN ({listed});"
        )
    return "\n".join(rows)


def _ownership() -> str:
    lines = [
        "REVOKE ALL ON FUNCTION access.holds_staff_permission(uuid, text) FROM PUBLIC;",
        "REVOKE ALL ON FUNCTION access.load_controls(uuid, text, text, jsonb) FROM PUBLIC;",
        "REVOKE ALL ON FUNCTION cloudops.pending_drafts(integer) FROM PUBLIC;",
        "REVOKE ALL ON FUNCTION cloudops.knowledge_tenants() FROM PUBLIC;",
        # The approval triggers run as the writer (the application login).
        "GRANT EXECUTE ON FUNCTION access.holds_staff_permission(uuid, text) TO cloudops_app;",
        "GRANT EXECUTE ON FUNCTION access.load_controls(uuid, text, text, jsonb)"
        " TO cloudops_app;",
        "GRANT EXECUTE ON FUNCTION cloudops.pending_drafts(integer) TO cloudops_app;",
        "GRANT EXECUTE ON FUNCTION cloudops.knowledge_tenants() TO cloudops_app;",
        "GRANT SELECT, INSERT, UPDATE ON cloudops.control TO cloudops_access_admin;",
        # Grants first: once ownership moves, the migrator can no longer grant.
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
        "ALTER FUNCTION access.holds_staff_permission(uuid, text) OWNER TO cloudops_access_admin;",
        "ALTER FUNCTION access.load_controls(uuid, text, text, jsonb)"
        " OWNER TO cloudops_access_admin;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;",
        "GRANT CREATE ON SCHEMA cloudops TO cloudops_resolver;",
        "ALTER FUNCTION cloudops.pending_drafts(integer) OWNER TO cloudops_resolver;",
        "ALTER FUNCTION cloudops.knowledge_tenants() OWNER TO cloudops_resolver;",
        "REVOKE CREATE ON SCHEMA cloudops FROM cloudops_resolver;",
    ]
    return "\n".join(lines)


CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS knowledge_chunk_document_idx"
    " ON cloudops.knowledge_chunk (document_id, ordinal) WHERE document_id IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS knowledge_chunk_unembedded_idx"
    " ON cloudops.knowledge_chunk (tenant_id) WHERE embedding IS NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS document_tenant_kind_idx"
    " ON cloudops.document (tenant_id, doc_kind) WHERE deleted_at IS NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS control_framework_idx"
    " ON cloudops.control (framework) WHERE retired_at IS NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS assessment_tenant_idx"
    " ON cloudops.assessment (tenant_id, created_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS control_response_tenant_idx"
    " ON cloudops.control_response (tenant_id, control_code)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS control_response_draft_idx"
    " ON cloudops.control_response (draft_state) WHERE draft_state = 'requested'",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS evidence_assessment_idx"
    " ON cloudops.evidence (tenant_id, assessment_id, control_code, retrieved_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS evidence_pack_assessment_idx"
    " ON cloudops.evidence_pack (tenant_id, assessment_id, created_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS report_pack_tenant_period_idx"
    " ON cloudops.report_pack (tenant_id, period DESC, version DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS report_narrative_draft_idx"
    " ON cloudops.report_narrative (draft_state) WHERE draft_state = 'requested'",
)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(PERMISSION_CHECK)  # the triggers call it
    op.execute(SECURITY)
    op.execute(TRIGGERS)
    op.execute(FUNCTIONS)
    op.execute(_seed())
    op.execute(_ownership())
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
