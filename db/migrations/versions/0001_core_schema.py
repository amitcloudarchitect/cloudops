"""Core schema: tenancy, commercial, inventory, tickets, cost, vulnerabilities,
knowledge, audit, FX, ingest state and quarantine.

Revision ID: 0001
Revises:
Create Date: 2026-10-27

Sources: reference/schema.sql for shape, docs/LLD.md (Database design) where the
two differ, CLAUDE.md for money. Departures from schema.sql, all deliberate:

- RLS policies are the LLD form: no `tenant_id IS NULL` escape hatch.
- Money is numeric(18,6) with a sibling currency column and no default currency;
  cost_fact.fx_to_eur is dropped (silver tables never store converted values).
- commercial_visibility includes 'commercial' (LLD role list).
- vulnerability is partitioned quarterly, as the LLD specifies.
- Partitioned tables key on (id, partition key); work_item is unique on
  (number, opened_at) since PostgreSQL cannot enforce `number` alone.
- cost_fact's natural key is NULLS NOT DISTINCT, so rows with no resource or
  service cannot be inserted twice.
- knowledge_chunk keeps a nullable tenant_id: NULL is Getronics-wide content,
  readable by every scope (the LLD retrieval query), writable by none of them.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

# Roles allowed to read commercial rows (LLD, Row-level security).
COMMERCIAL_ROLES = (
    "ceo",
    "cfo",
    "finops",
    "delivery_lead",
    "commercial",
    "auditor",
    "platform_admin",
)

# (table, control column, interval, retention) — LLD, Partitioning and indexes.
PARTITIONED = (
    ("cloudops.cost_fact", "usage_date", "1 month", "7 years"),
    ("cloudops.work_item", "opened_at", "3 months", None),
    ("cloudops.vulnerability", "first_detected", "3 months", None),
    ("audit.action_log", "created_at", "1 month", None),  # 7 years, never dropped
)

FOUNDATION = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS cloudops;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS audit;

-- Group roles that privileges are granted to. Login identities (one per
-- workload) are created by infrastructure and made members of these.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cloudops_app') THEN
        CREATE ROLE cloudops_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cloudops_ingest') THEN
        CREATE ROLE cloudops_ingest NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT;
    END IF;
END $$;

REVOKE ALL ON SCHEMA cloudops, staging, audit FROM PUBLIC;

-- Scope as set by packages/tenancy. Unset or empty means no tenants, never
-- all of them. A malformed value raises, so a bad scope fails closed.
CREATE FUNCTION cloudops.current_tenant_ids() RETURNS uuid[]
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN coalesce(pg_catalog.current_setting('app.tenant_ids', true), '') = ''
            THEN ARRAY[]::uuid[]
        ELSE pg_catalog.string_to_array(
                 pg_catalog.current_setting('app.tenant_ids', true), ',')::uuid[]
    END
$$;

CREATE TYPE cloudops.delivery_location AS ENUM ('India', 'Budapest', 'Mixed');
CREATE TYPE cloudops.coverage_model    AS ENUM ('8x5', '16x5', '24x7');
CREATE TYPE cloudops.support_model     AS ENUM ('Bronze', 'Silver', 'Gold');
CREATE TYPE cloudops.charge_class AS ENUM
    ('consumption','licence','marketplace','support','reservation',
     'savings_plan','tax','credit','other');
CREATE TYPE audit.autonomy_level AS ENUM
    ('L0_explain','L1_recommend','L2_draft','L3_approved_execute','L4_auto');
"""

TABLES = """
-- 1. Tenancy -----------------------------------------------------------------

CREATE TABLE cloudops.tenant (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name    text NOT NULL UNIQUE,
    short_code        text NOT NULL UNIQUE,
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN
                             ('prospect','transitioning','active','offboarding','exited')),
    delivery_location cloudops.delivery_location NOT NULL DEFAULT 'India',
    coverage          cloudops.coverage_model    NOT NULL DEFAULT '24x7',
    support           cloudops.support_model     NOT NULL DEFAULT 'Gold',
    data_residency    text NOT NULL DEFAULT 'EU',
    contract_start    date,
    contract_end      date,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cloudops.tenant_alias (
    id             bigserial PRIMARY KEY,
    tenant_id      uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    source_system  text NOT NULL,
    alias          text NOT NULL,
    alias_norm     text GENERATED ALWAYS AS
                   (lower(regexp_replace(alias, '[^a-zA-Z0-9]', '', 'g'))) STORED,
    confidence     numeric(3,2) NOT NULL DEFAULT 1.00,
    confirmed_by   text,
    confirmed_at   timestamptz,
    UNIQUE (source_system, alias)
);

-- 2. Commercial --------------------------------------------------------------

CREATE TABLE cloudops.sow (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    reference              text NOT NULL,
    effective_from         date NOT NULL,
    effective_to           date,
    currency               char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    monthly_manage_revenue numeric(18,6) NOT NULL,
    hosting_acv            numeric(18,6),
    manage_acv             numeric(18,6),
    professional_services  numeric(18,6) NOT NULL DEFAULT 0,
    contracted_ci_count    integer,
    contracted_slab        text,
    coverage               cloudops.coverage_model,
    support                cloudops.support_model,
    excludes_licence_spend boolean NOT NULL DEFAULT true,
    document_uri           text,
    created_at             timestamptz NOT NULL DEFAULT now()
);

-- 3. Inventory ---------------------------------------------------------------

CREATE TABLE cloudops.cloud_account (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    provider         text NOT NULL CHECK (provider IN ('azure','aws')),
    native_id        text NOT NULL,
    display_name     text NOT NULL,
    environment      text,
    region_default   text,
    onboarded_at     timestamptz,
    connection_state text NOT NULL DEFAULT 'pending'
                     CHECK (connection_state IN
                            ('pending','validating','healthy','degraded','revoked')),
    last_discovery   timestamptz,
    UNIQUE (provider, native_id)
);

CREATE TABLE cloudops.resource (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id           uuid NOT NULL REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    native_id            text NOT NULL,
    name                 text NOT NULL,
    name_norm            text GENERATED ALWAYS AS (lower(split_part(name, '.', 1))) STORED,
    fqdn                 text,
    ip_addresses         inet[],
    type_native          text NOT NULL,
    type_canonical       text NOT NULL,
    catalog_type         text,
    region               text,
    operating_system     text,
    is_critical_ci       boolean NOT NULL DEFAULT true,
    tags                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    state                text,
    first_seen           timestamptz NOT NULL DEFAULT now(),
    last_seen            timestamptz NOT NULL DEFAULT now(),
    deleted_at           timestamptz,
    opsramp_resource_id  text,
    servicenow_ci_sys_id text,
    qualys_asset_id      text,
    UNIQUE (account_id, native_id)
);

-- 4. ServiceNow mirror (partitioned quarterly on opened_at) -------------------

CREATE TABLE cloudops.work_item (
    id                    uuid NOT NULL DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    kind                  text NOT NULL
                          CHECK (kind IN
                                 ('incident','change_task','service_request','project_task')),
    number                text NOT NULL,
    change_request        text,
    company_raw           text NOT NULL,
    opened_at             timestamptz NOT NULL,
    closed_at             timestamptz,
    priority              text,
    state                 text,
    assigned_to           text,
    assignment_workgroup  text,
    short_description     text,
    configuration_item    text,
    resource_id           uuid REFERENCES cloudops.resource(id),
    change_type           text,
    resolve_sla           boolean,
    response_sla          boolean,
    made_sla              boolean,
    sla_breached          boolean,
    duration_seconds      bigint,
    resolve_time_seconds  bigint,
    pending_total_seconds bigint,
    planned_hours         numeric(8,2),
    actual_hours          numeric(8,2),
    closure_code          text,
    close_notes           text,
    is_workaround         boolean GENERATED ALWAYS AS
                          (coalesce(closure_code ILIKE '%workaround%', false)) STORED,
    ingested_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, opened_at),
    -- A ticket's opened_at never changes, so this is the connector's upsert key.
    UNIQUE (number, opened_at)
) PARTITION BY RANGE (opened_at);

-- 6. Qualys mirror (partitioned quarterly on first_detected) ------------------

CREATE TABLE cloudops.vulnerability (
    id                  uuid NOT NULL DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    qid                 integer NOT NULL,
    asset_name          text NOT NULL,
    asset_ipv4          text,
    resource_id         uuid REFERENCES cloudops.resource(id),
    scope               text NOT NULL CHECK (scope IN ('os','application')),
    title               text NOT NULL,
    severity            smallint NOT NULL CHECK (severity BETWEEN 1 AND 5),
    operating_system    text,
    solution            text,
    threat              text,
    detection_type      text,
    asset_tags          text[],
    first_detected      timestamptz NOT NULL,
    last_detected       timestamptz,
    remediated_at       timestamptz,
    detection_span_days numeric(8,3),
    fix_days            numeric(8,3) GENERATED ALWAYS AS (
                            CASE WHEN remediated_at IS NOT NULL
                            THEN EXTRACT(epoch FROM remediated_at - first_detected) / 86400.0 END
                        ) STORED,
    PRIMARY KEY (id, first_detected),
    UNIQUE (tenant_id, qid, asset_name, first_detected)
) PARTITION BY RANGE (first_detected);

-- 7. Cost (partitioned monthly on usage_date) ----------------------------------

CREATE TABLE cloudops.cost_fact (
    id                bigserial,
    tenant_id         uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id        uuid NOT NULL REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    resource_id       uuid REFERENCES cloudops.resource(id),
    usage_date        date NOT NULL,
    invoice_period    text NOT NULL,
    service_native    text,
    service_canonical text,
    charge_class      cloudops.charge_class NOT NULL DEFAULT 'consumption',
    unblended_cost    numeric(18,6) NOT NULL,
    amortized_cost    numeric(18,6) NOT NULL,
    currency          char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    tags_snapshot     jsonb,
    PRIMARY KEY (id, usage_date),
    UNIQUE NULLS NOT DISTINCT (account_id, usage_date, resource_id, service_native, charge_class)
) PARTITION BY RANGE (usage_date);

-- 9. Knowledge ----------------------------------------------------------------

CREATE TABLE cloudops.knowledge_chunk (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid REFERENCES cloudops.tenant(id) ON DELETE CASCADE,  -- NULL = Getronics-wide
    source_kind text NOT NULL,
    source_ref  text,
    title       text,
    body        text NOT NULL,
    embedding   vector(1536),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- 10. Audit (partitioned monthly, append-only) ----------------------------------

CREATE TABLE audit.action_log (
    id                 bigserial,
    tenant_id          uuid NOT NULL REFERENCES cloudops.tenant(id),
    actor              text NOT NULL,
    actor_is_agent     boolean NOT NULL DEFAULT false,
    autonomy           audit.autonomy_level,
    action             text NOT NULL,
    target_kind        text,
    target_ref         text,
    approval_user      text,
    approval_at        timestamptz,
    change_request_ref text,
    blast_radius       jsonb,
    before_state       jsonb,
    after_state        jsonb,
    outcome            text NOT NULL DEFAULT 'pending'
                       CHECK (outcome IN ('pending','succeeded','failed','rolled_back','blocked')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Reference data: FX (LLD, Multi-currency) --------------------------------------

CREATE TABLE cloudops.fx_rate (
    id              bigserial PRIMARY KEY,
    from_currency   char(3) NOT NULL CHECK (from_currency ~ '^[A-Z]{3}$'),
    to_currency     char(3) NOT NULL CHECK (to_currency ~ '^[A-Z]{3}$'),
    rate            numeric(18,8) NOT NULL CHECK (rate > 0),
    rate_type       text NOT NULL CHECK (rate_type IN ('closing','average')),
    effective_date  date NOT NULL,
    source          text NOT NULL,
    UNIQUE (from_currency, to_currency, rate_type, effective_date)
);

-- Ingest state and quarantine ----------------------------------------------------

-- One watermark per source, entity and scope (an instance, account or tenant
-- key), advanced only in the transaction that commits the batch.
CREATE TABLE staging.ingest_watermark (
    source_system text NOT NULL,
    entity        text NOT NULL,
    scope_key     text NOT NULL,
    watermark     timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, entity, scope_key)
);

-- Customer names that matched no tenant. No tenant_id by definition, so it is
-- protected by grants instead: ingest only, never the application role.
CREATE TABLE staging.tenant_alias_quarantine (
    id                  bigserial PRIMARY KEY,
    source_system       text NOT NULL,
    alias               text NOT NULL,
    first_seen          timestamptz NOT NULL DEFAULT now(),
    last_seen           timestamptz NOT NULL DEFAULT now(),
    occurrence_count    bigint NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    suggested_tenant_id uuid REFERENCES cloudops.tenant(id),
    raw_ref             text,
    UNIQUE (source_system, alias)
);

-- Resources whose tenant is known (via the account) but which matched no
-- canonical resource.
CREATE TABLE staging.resource_quarantine (
    id               bigserial PRIMARY KEY,
    tenant_id        uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id       uuid REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    source_system    text NOT NULL,
    external_key     text NOT NULL,
    name             text,
    ip_address       inet,
    first_seen       timestamptz NOT NULL DEFAULT now(),
    last_seen        timestamptz NOT NULL DEFAULT now(),
    occurrence_count bigint NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    raw_ref          text,
    UNIQUE (tenant_id, source_system, external_key)
);
"""

# Indexes on partitioned parents cannot be built CONCURRENTLY; PostgreSQL
# builds them on each partition. These tables are empty at creation.
PARENT_INDEXES = """
CREATE INDEX ON cloudops.work_item (tenant_id, kind, opened_at DESC);  -- partitioned parent
CREATE INDEX ON cloudops.work_item (resource_id)  -- partitioned parent
    WHERE resource_id IS NOT NULL;
CREATE INDEX ON cloudops.work_item (tenant_id) WHERE is_workaround;  -- partitioned parent
CREATE INDEX ON cloudops.cost_fact (tenant_id, usage_date);  -- partitioned parent
CREATE INDEX ON cloudops.cost_fact (tenant_id, charge_class, usage_date);  -- partitioned parent
CREATE INDEX ON cloudops.vulnerability (tenant_id, severity, remediated_at);  -- partitioned parent
CREATE INDEX ON cloudops.vulnerability (tenant_id)  -- partitioned parent
    WHERE remediated_at IS NULL;
CREATE INDEX ON audit.action_log (tenant_id, created_at DESC);  -- partitioned parent
CREATE INDEX ON audit.action_log (actor, created_at DESC);  -- partitioned parent
"""

CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS tenant_alias_alias_norm_idx"
    " ON cloudops.tenant_alias (alias_norm)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS tenant_alias_tenant_id_idx"
    " ON cloudops.tenant_alias (tenant_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS sow_tenant_effective_idx"
    " ON cloudops.sow (tenant_id, effective_from DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cloud_account_tenant_id_idx"
    " ON cloudops.cloud_account (tenant_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS resource_live_tenant_idx"
    " ON cloudops.resource (tenant_id) WHERE deleted_at IS NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS resource_name_norm_idx"
    " ON cloudops.resource (name_norm)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS resource_tags_idx"
    " ON cloudops.resource USING gin (tags)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS knowledge_chunk_embedding_idx"
    " ON cloudops.knowledge_chunk USING hnsw (embedding vector_cosine_ops)"
    " WITH (m = 16, ef_construction = 64)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS knowledge_chunk_tenant_kind_idx"
    " ON cloudops.knowledge_chunk (tenant_id, source_kind)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS resource_quarantine_tenant_idx"
    " ON staging.resource_quarantine (tenant_id)",
)

# Every table carrying customer data, and the column that names its tenant.
TENANT_TABLES = (
    ("cloudops.tenant", "id"),
    ("cloudops.tenant_alias", "tenant_id"),
    ("cloudops.sow", "tenant_id"),
    ("cloudops.cloud_account", "tenant_id"),
    ("cloudops.resource", "tenant_id"),
    ("cloudops.work_item", "tenant_id"),
    ("cloudops.vulnerability", "tenant_id"),
    ("cloudops.cost_fact", "tenant_id"),
    ("audit.action_log", "tenant_id"),
    ("staging.resource_quarantine", "tenant_id"),
)


def _rls(table: str, column: str) -> str:
    # FORCE, not just ENABLE: without it the table owner — the role
    # migrations run as — bypasses every policy.
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table}
  USING      ({column} = ANY(cloudops.current_tenant_ids()))
  WITH CHECK ({column} = ANY(cloudops.current_tenant_ids()));
"""


def _commercial(table: str) -> str:
    roles = ",".join(f"'{r}'" for r in COMMERCIAL_ROLES)
    return f"""
CREATE POLICY commercial_visibility ON {table}
  AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') IN ({roles}));
"""


SECURITY = (
    "".join(_rls(t, c) for t, c in TENANT_TABLES)
    + _commercial("cloudops.sow")
    + """
-- Global knowledge (tenant_id NULL) is readable by every scope; no scope can
-- write it. Writes of global content are a separate, later decision.
ALTER TABLE cloudops.knowledge_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.knowledge_chunk FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.knowledge_chunk
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()) OR tenant_id IS NULL)
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));

-- The action log is append-only, for everyone. Privileges stop the
-- application; the trigger stops the owner too.
CREATE FUNCTION audit.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.action_log is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER action_log_append_only
    BEFORE UPDATE OR DELETE ON audit.action_log
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
CREATE TRIGGER action_log_no_truncate
    BEFORE TRUNCATE ON audit.action_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON audit.action_log FROM PUBLIC;

-- Privileges. Partitions get none: reaching a partition directly would skip
-- the parent's policies, so it must be impossible rather than merely unused.
GRANT USAGE ON SCHEMA cloudops, audit TO cloudops_app, cloudops_ingest;
GRANT USAGE ON SCHEMA staging TO cloudops_ingest;

GRANT SELECT, INSERT, UPDATE ON
    cloudops.tenant, cloudops.tenant_alias, cloudops.sow, cloudops.cloud_account,
    cloudops.resource, cloudops.work_item, cloudops.vulnerability, cloudops.cost_fact,
    cloudops.knowledge_chunk
    TO cloudops_app, cloudops_ingest;
GRANT SELECT ON cloudops.fx_rate TO cloudops_app, cloudops_ingest;
GRANT INSERT, UPDATE ON cloudops.fx_rate TO cloudops_ingest;
GRANT SELECT, INSERT ON audit.action_log TO cloudops_app, cloudops_ingest;
GRANT SELECT, INSERT, UPDATE ON
    staging.ingest_watermark, staging.tenant_alias_quarantine, staging.resource_quarantine
    TO cloudops_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA cloudops, audit TO cloudops_app, cloudops_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA staging TO cloudops_ingest;
"""
)

# LLD, Multi-currency: the rate in force on the transaction date; rate_type is a
# required argument (flows use average, balances use closing); a missing rate
# raises rather than returning zero. No rounding — that happens once, at the
# presentation boundary. No inverse-pair fallback.
FX_FUNCTIONS = """
CREATE FUNCTION cloudops.fx_rate_on(
    p_from_currency char(3),
    p_to_currency   char(3),
    p_on_date       date,
    p_rate_type     text
) RETURNS TABLE (rate numeric(18,8), effective_date date)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF p_rate_type IS NULL OR p_rate_type NOT IN ('closing', 'average') THEN
        RAISE EXCEPTION 'rate_type must be closing or average, got %', p_rate_type
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_from_currency IS NULL OR p_to_currency IS NULL OR p_on_date IS NULL THEN
        RAISE EXCEPTION 'currencies and date are required'
            USING ERRCODE = 'null_value_not_allowed';
    END IF;
    IF p_from_currency = p_to_currency THEN
        RETURN QUERY SELECT 1::numeric(18,8), p_on_date;
        RETURN;
    END IF;
    RETURN QUERY
        SELECT f.rate, f.effective_date
        FROM cloudops.fx_rate f
        WHERE f.from_currency = p_from_currency
          AND f.to_currency = p_to_currency
          AND f.rate_type = p_rate_type
          AND f.effective_date <= p_on_date
        ORDER BY f.effective_date DESC
        LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no % rate % -> % in force on %',
            p_rate_type, p_from_currency, p_to_currency, p_on_date
            USING ERRCODE = 'no_data_found';
    END IF;
END $$;

CREATE FUNCTION cloudops.to_currency(
    p_amount        numeric,
    p_from_currency char(3),
    p_to_currency   char(3),
    p_on_date       date,
    p_rate_type     text
) RETURNS numeric
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN p_amount IS NULL THEN NULL ELSE p_amount * r.rate END
    FROM cloudops.fx_rate_on(p_from_currency, p_to_currency, p_on_date, p_rate_type) r
$$;

GRANT EXECUTE ON FUNCTION
    cloudops.fx_rate_on(char, char, date, text),
    cloudops.to_currency(numeric, char, char, date, text)
    TO cloudops_app, cloudops_ingest;
"""


def _partman_available() -> bool:
    found = (
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_partman'"))
        .scalar()
    )
    return found is not None


def _partition() -> None:
    """Register partitioned tables with pg_partman (LLD: pg_partman manages
    creation and retention; a missing future partition is an outage).

    Only a test harness can opt out, through ``config.attributes`` which the
    alembic CLI cannot set; it then gets plain default partitions.
    """
    if _partman_available():
        for table, control, interval, retention in PARTITIONED:
            op.execute(
                text(
                    "SELECT partman.create_parent("
                    " p_parent_table := :t, p_control := :c, p_interval := :i, p_premake := 4)"
                ).bindparams(t=table, c=control, i=interval)
            )
            op.execute(
                text(
                    "UPDATE partman.part_config"
                    " SET retention = :r, retention_keep_table = false,"
                    "     infinite_time_partitions = true"
                    " WHERE parent_table = :t"
                ).bindparams(r=retention, t=table)
            )
        return
    if not op.get_context().config.attributes.get("allow_missing_partman"):  # type: ignore[union-attr]
        raise RuntimeError("pg_partman is not installed in this database")
    for table, *_ in PARTITIONED:
        op.execute(f"CREATE TABLE {table}_default PARTITION OF {table} DEFAULT")


def upgrade() -> None:
    op.execute(FOUNDATION)
    op.execute(TABLES)
    op.execute(PARENT_INDEXES)
    op.execute(SECURITY)
    op.execute(FX_FUNCTIONS)
    _partition()
    # CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
