"""Remaining connectors, telemetry configuration and connector health (expand).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-31

Step 15. LLD, Connector contract and Identity resolution; HLD, Integration.

Canonical tables gain ``raw_extra`` (every unmapped source field is kept) and,
where several sources write one table, ``source_system``:

- ``resource``: Azure Resource Graph and AWS Config.
- ``vulnerability``: Qualys, plus the detection ``status``.
- ``cost_fact``: AWS CUR, plus ``source_batch`` (the CUR assembly a row came from).

New tenant tables, each FORCE RLS with ``tenant_isolation`` and an account
restriction (ADR 0002, D16):

- ``cost_finding``: CoreStack's findings, kept *beside* the raw billing exports,
  never instead of them (HLD). Restricted like cost: by account.
- ``directory_principal`` and ``directory_role_assignment``: Entra ID, read for
  access control and audit evidence. A directory is tenant-wide, so an
  account-restricted scope sees none of it.
- ``sharepoint_site`` and ``document``: per-site selected consent, the delta
  sync's documents. Chunks go to ``knowledge_chunk`` (no embedding yet; step 17)
  and are deleted when the document is deleted or the site's consent revoked.
  The ingest role gains DELETE on ``knowledge_chunk`` for that, and only that.
- ``staging.cost_fact_load``: one CUR assembly, loaded file by file, swapped into
  ``cost_fact`` in one transaction when the last file lands. CUR settles late
  and restates: a new assembly replaces its accounts' rows for the period.

Ingest state with no tenant, protected by grants (ingest only):

- ``staging.ingest_cursor``: opaque continuation state (Graph delta links)
  beside the time watermark, advanced in the committing transaction.
- ``staging.cur_assembly``: which files a CUR assembly has and which have loaded.
- ``staging.schema_field``: every field a source has sent per entity, and drift:
  a new field (it lands in ``raw_extra``) or an expected field missing from a
  whole page. A person acknowledges drift in the console.

Configuration, held as data not code (like ``legal_suffix``):

- ``staging.metric_allowlist``: LLD open item L3, a starter allowlist per
  resource type. Telemetry normalisation stores only these; everything else is
  counted and left in the bronze capture.
- ``staging.connector_schedule``: each connector's expected interval (HLD,
  Integration). Health bands on it: the LLD pages after three missed intervals.

Health: ``access.connector_health(actor)`` and ``access.schema_drift(actor)``
read the run log, quarantine depth and drift for staff holding
``connectors.read``; ``access.acknowledge_drift`` needs ``connectors.manage``.
Quarantined resources are counted under the caller's own scope (the table's
RLS applies to the function too), alias quarantine in full: it has no tenant.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

# HLD, Integration: each source's cadence. (source, entity, interval)
SCHEDULES = (
    ("servicenow", "incident", "5 minutes"),
    ("servicenow", "change_task", "5 minutes"),
    ("qualys", "host_detection", "1 day"),
    ("corestack", "finding", "1 day"),
    ("azure", "resource", "15 minutes"),
    ("aws", "config", "15 minutes"),
    ("aws", "cur", "1 day"),
    ("sharepoint", "drive_item", "1 day"),
    ("entra", "user", "1 day"),
    ("entra", "role_assignment", "1 day"),
    ("telemetry", "metrics", "5 minutes"),
)

# LLD open item L3: the starter allowlist (resource type, metric, unit). The
# metrics that drive a decision: capacity, saturation and availability.
METRIC_ALLOWLIST = (
    ("compute.vm", "cpu.utilisation", "percent"),
    ("compute.vm", "memory.available", "bytes"),
    ("compute.vm", "disk.read_ops", "count_per_second"),
    ("compute.vm", "disk.write_ops", "count_per_second"),
    ("compute.vm", "network.in", "bytes"),
    ("compute.vm", "network.out", "bytes"),
    ("compute.vm", "availability", "percent"),
    ("database.sql", "cpu.utilisation", "percent"),
    ("database.sql", "storage.used", "percent"),
    ("database.sql", "connections.failed", "count"),
    ("database.sql", "availability", "percent"),
    ("storage.account", "availability", "percent"),
    ("storage.account", "latency.e2e", "milliseconds"),
    ("storage.account", "capacity.used", "bytes"),
    ("network.load_balancer", "availability", "percent"),
    ("network.load_balancer", "health_probe", "percent"),
)

CONNECTOR_PERMISSIONS = (
    ("connectors.read", "Connector health: runs, quarantine and schema drift"),
    ("connectors.manage", "Acknowledge connector schema drift"),
)
# Staff roles that watch ingest; customers never see it.
CONNECTOR_READERS = ("Platform administrator", "Delivery lead", "Engineer", "Customer master")

DDL = """
ALTER TABLE cloudops.resource ADD COLUMN raw_extra jsonb;
ALTER TABLE cloudops.resource ADD COLUMN source_system text;
ALTER TABLE cloudops.vulnerability ADD COLUMN raw_extra jsonb;
ALTER TABLE cloudops.vulnerability ADD COLUMN status text;
ALTER TABLE cloudops.cost_fact ADD COLUMN raw_extra jsonb;
ALTER TABLE cloudops.cost_fact ADD COLUMN source_system text;
ALTER TABLE cloudops.cost_fact ADD COLUMN source_batch text;

-- CoreStack: findings beside the billing exports, never instead of them.
CREATE TABLE cloudops.cost_finding (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id             uuid REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    resource_id            uuid REFERENCES cloudops.resource(id),
    source_system          text NOT NULL,
    finding_id             text NOT NULL,
    category               text NOT NULL,
    title                  text NOT NULL,
    recommendation         text,
    status                 text NOT NULL CHECK (status IN ('open', 'resolved', 'dismissed')),
    estimated_saving       numeric(18,6),
    saving_currency        char(3) CHECK (saving_currency ~ '^[A-Z]{3}$'),
    customer_raw           text NOT NULL,
    account_native_id      text,
    resource_native_id     text,
    detected_at            timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,
    raw_extra              jsonb,
    ingested_at            timestamptz NOT NULL DEFAULT now(),
    -- Money with its currency, or neither.
    CHECK ((estimated_saving IS NULL) = (saving_currency IS NULL)),
    UNIQUE (source_system, finding_id)
);

-- Entra ID, per customer directory.
CREATE TABLE cloudops.directory_principal (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    directory_id         text NOT NULL,
    object_id            text NOT NULL,
    kind                 text NOT NULL CHECK (kind IN ('user', 'group', 'service_principal')),
    display_name         text,
    user_principal_name  text,
    account_enabled      boolean,
    user_type            text,
    created_at_source    timestamptz,
    deleted_at           timestamptz,
    raw_extra            jsonb,
    ingested_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (directory_id, object_id)
);

CREATE TABLE cloudops.directory_role_assignment (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    directory_id         text NOT NULL,
    assignment_id        text NOT NULL,
    principal_object_id  text NOT NULL,
    role_definition_id   text NOT NULL,
    role_name            text,
    directory_scope_id   text NOT NULL,
    first_seen           timestamptz NOT NULL DEFAULT now(),
    last_seen            timestamptz NOT NULL DEFAULT now(),
    removed_at           timestamptz,
    raw_extra            jsonb,
    UNIQUE (directory_id, assignment_id)
);

-- SharePoint: a site is read only with its own consent (Sites.Selected).
CREATE TABLE cloudops.sharepoint_site (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    site_id       text NOT NULL UNIQUE,
    site_url      text NOT NULL,
    customer_raw  text NOT NULL,
    consent       text NOT NULL DEFAULT 'granted' CHECK (consent IN ('granted', 'revoked')),
    registered_at timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz,
    CHECK ((consent = 'revoked') = (revoked_at IS NOT NULL))
);

CREATE TABLE cloudops.document (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    source_system  text NOT NULL,
    site_id        text NOT NULL,
    item_id        text NOT NULL,
    name           text NOT NULL,
    web_url        text,
    mime_type      text,
    etag           text,
    size_bytes     bigint,
    modified_at    timestamptz,
    extraction     text NOT NULL CHECK (extraction IN ('text', 'unsupported', 'folder')),
    chunk_count    integer NOT NULL DEFAULT 0,
    deleted_at     timestamptz,
    raw_extra      jsonb,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_system, site_id, item_id)
);

-- One CUR assembly while it loads; swapped into cost_fact when complete.
CREATE TABLE staging.cost_fact_load (
    id                bigserial PRIMARY KEY,
    tenant_id         uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id        uuid NOT NULL REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    assembly_id       text NOT NULL,
    object_key        text NOT NULL,
    resource_id       uuid REFERENCES cloudops.resource(id),
    usage_date        date NOT NULL,
    invoice_period    text NOT NULL,
    service_native    text,
    charge_class      cloudops.charge_class NOT NULL,
    unblended_cost    numeric(18,6) NOT NULL,
    amortized_cost    numeric(18,6) NOT NULL,
    currency          char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    raw_extra         jsonb
);

CREATE TABLE staging.cur_assembly (
    assembly_id     text PRIMARY KEY,
    scope_key       text NOT NULL,
    invoice_period  text NOT NULL,
    manifest_ref    text NOT NULL,
    report_keys     text[] NOT NULL,
    loaded_keys     text[] NOT NULL DEFAULT '{}',
    tenant_ids      uuid[] NOT NULL DEFAULT '{}',
    account_ids     uuid[] NOT NULL DEFAULT '{}',
    registered_at   timestamptz NOT NULL DEFAULT now(),
    swapped_at      timestamptz
);

CREATE TABLE staging.ingest_cursor (
    source_system text NOT NULL,
    entity        text NOT NULL,
    scope_key     text NOT NULL,
    cursor        text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, entity, scope_key)
);

CREATE TABLE staging.schema_field (
    source_system   text NOT NULL,
    entity          text NOT NULL,
    field           text NOT NULL CHECK (length(field) <= 200),
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    drift           text CHECK (drift IN ('added', 'missing')),
    drift_at        timestamptz,
    drift_count     bigint NOT NULL DEFAULT 0,
    raw_ref         text,
    acknowledged_at timestamptz,
    acknowledged_by uuid,
    PRIMARY KEY (source_system, entity, field)
);

CREATE TABLE staging.metric_allowlist (
    type_canonical text NOT NULL,
    metric         text NOT NULL CHECK (metric ~ '^[a-z0-9_.]+$'),
    unit           text NOT NULL,
    PRIMARY KEY (type_canonical, metric)
);

CREATE TABLE staging.connector_schedule (
    source_system text NOT NULL,
    entity        text NOT NULL,
    every         interval NOT NULL CHECK (every > interval '0'),
    PRIMARY KEY (source_system, entity)
);
"""

TENANT_TABLES = (
    "cloudops.cost_finding",
    "cloudops.directory_principal",
    "cloudops.directory_role_assignment",
    "cloudops.sharepoint_site",
    "cloudops.document",
    "staging.cost_fact_load",
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
-- D16. Findings and loaded cost follow their account; a finding with no account
-- is hidden from a restricted scope. A directory and a SharePoint site are
-- tenant-wide: a restricted scope sees none of them.
CREATE POLICY account_scope ON cloudops.cost_finding AS RESTRICTIVE
  USING (cloudops.account_scope_is_all() OR account_id = ANY(cloudops.current_account_ids()));
CREATE POLICY account_scope ON staging.cost_fact_load AS RESTRICTIVE
  USING (cloudops.account_scope_is_all() OR account_id = ANY(cloudops.current_account_ids()));
CREATE POLICY account_scope ON cloudops.directory_principal AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.directory_role_assignment AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.sharepoint_site AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.document AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());

GRANT SELECT ON cloudops.cost_finding, cloudops.directory_principal,
    cloudops.directory_role_assignment, cloudops.sharepoint_site, cloudops.document
    TO cloudops_app;
GRANT SELECT, INSERT, UPDATE ON cloudops.cost_finding, cloudops.directory_principal,
    cloudops.directory_role_assignment, cloudops.sharepoint_site, cloudops.document
    TO cloudops_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON staging.cost_fact_load TO cloudops_ingest;
GRANT SELECT, INSERT, UPDATE ON staging.cur_assembly, staging.ingest_cursor,
    staging.schema_field TO cloudops_ingest;
GRANT SELECT ON staging.metric_allowlist, staging.connector_schedule TO cloudops_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA staging TO cloudops_ingest;
-- CUR restatement replaces a period's rows; SharePoint deletion removes chunks.
GRANT DELETE ON cloudops.cost_fact TO cloudops_ingest;
GRANT DELETE ON cloudops.knowledge_chunk TO cloudops_ingest;
"""

FUNCTIONS = """
-- The actor holds the permission as staff (never a customer role).
CREATE FUNCTION access.require_connector_permission(p_actor uuid, p_permission text)
RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class <> 'customer' AND rp.permission = p_permission
    ) THEN
        RAISE EXCEPTION 'not permitted to read connector health'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
END $$;

-- One row per scheduled source and entity. Run figures cover the last 24 hours;
-- last success is all-time. Resource quarantine is counted under the caller's
-- scope (its RLS applies here); alias quarantine has no tenant.
CREATE FUNCTION access.connector_health(p_actor uuid)
RETURNS TABLE (source_system text, entity text, expected_every_seconds bigint,
               last_success_at timestamptz, last_run_at timestamptz, last_status text,
               last_lag_seconds bigint, runs_24h bigint, failed_runs_24h bigint,
               records_24h bigint, rejected_24h bigint, quarantined_24h bigint,
               dead_letters_open bigint, alias_quarantine_depth bigint,
               resource_quarantine_depth bigint, drift_open bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    PERFORM access.require_connector_permission(p_actor, 'connectors.read');
    RETURN QUERY
    SELECT s.source_system, s.entity,
           extract(epoch FROM s.every)::bigint,
           (SELECT max(r.finished_at) FROM staging.ingest_run r
             WHERE r.source_system = s.source_system AND r.entity = s.entity
               AND r.status = 'succeeded' AND r.mode = 'incremental'),
           last.started_at, last.status, last.lag_seconds,
           coalesce(day.runs, 0), coalesce(day.failed, 0), coalesce(day.records, 0),
           coalesce(day.rejected, 0), coalesce(day.quarantined, 0),
           (SELECT count(*) FROM staging.ingest_dead_letter d
             WHERE d.source_system = s.source_system AND d.entity = s.entity
               AND d.replayed_at IS NULL),
           (SELECT count(*) FROM staging.tenant_alias_quarantine q
             WHERE q.source_system = s.source_system),
           (SELECT count(*) FROM staging.resource_quarantine q
             WHERE q.source_system = s.source_system),
           (SELECT count(*) FROM staging.schema_field f
             WHERE f.source_system = s.source_system AND f.entity = s.entity
               AND f.drift IS NOT NULL AND f.acknowledged_at IS NULL)
    FROM staging.connector_schedule s
    LEFT JOIN LATERAL (
        SELECT r.started_at, r.status, r.lag_seconds FROM staging.ingest_run r
        WHERE r.source_system = s.source_system AND r.entity = s.entity
          AND r.mode = 'incremental'
        ORDER BY r.started_at DESC LIMIT 1) last ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS runs,
               count(*) FILTER (WHERE r.status IN ('failed', 'dead_lettered')) AS failed,
               sum(r.records) AS records, sum(r.rejected) AS rejected,
               sum(r.quarantined) AS quarantined
        FROM staging.ingest_run r
        WHERE r.source_system = s.source_system AND r.entity = s.entity
          AND r.mode = 'incremental' AND r.started_at > now() - interval '24 hours'
          AND r.status <> 'running') day ON true
    ORDER BY s.source_system, s.entity;
END $$;

-- Drift waiting for a person. Field names only: never a value.
CREATE FUNCTION access.schema_drift(p_actor uuid)
RETURNS TABLE (source_system text, entity text, field text, drift text,
               drift_at timestamptz, drift_count bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
BEGIN
    PERFORM access.require_connector_permission(p_actor, 'connectors.read');
    RETURN QUERY
    SELECT f.source_system, f.entity, f.field, f.drift, f.drift_at, f.drift_count
    FROM staging.schema_field f
    WHERE f.drift IS NOT NULL AND f.acknowledged_at IS NULL
    ORDER BY f.drift_at DESC, f.source_system, f.entity, f.field;
END $$;

-- A person has seen the drift (and changed the mapping, or decided not to).
-- The next drift on the same field raises it again.
CREATE FUNCTION access.acknowledge_drift(
    p_actor uuid, p_source text, p_entity text, p_field text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_found boolean;
BEGIN
    PERFORM access.require_connector_permission(p_actor, 'connectors.manage');
    UPDATE staging.schema_field f
       SET acknowledged_at = now(), acknowledged_by = p_actor
     WHERE f.source_system = p_source AND f.entity = p_entity AND f.field = p_field
       AND f.drift IS NOT NULL AND f.acknowledged_at IS NULL;
    v_found := FOUND;
    IF v_found THEN
        PERFORM access.log_change(p_actor::text, 'acknowledge_drift',
            jsonb_build_object('source', p_source, 'entity', p_entity, 'field', p_field));
    END IF;
    RETURN v_found;
END $$;
"""

READERS = ("connector_health(uuid)", "schema_drift(uuid)")
WRITERS = ("acknowledge_drift(uuid, text, text, text)",)

PRIVILEGES = """
GRANT USAGE ON SCHEMA staging TO cloudops_access_admin;
GRANT SELECT ON staging.connector_schedule, staging.ingest_run, staging.ingest_dead_letter,
    staging.resource_quarantine TO cloudops_access_admin;
GRANT SELECT, UPDATE ON staging.schema_field TO cloudops_access_admin;
GRANT EXECUTE ON FUNCTION access.require_connector_permission(uuid, text)
    TO cloudops_access_admin;
"""


def _seed() -> str:
    rows = [
        "INSERT INTO staging.connector_schedule (source_system, entity, every) VALUES "
        + ", ".join(f"('{s}', '{e}', interval '{i}')" for s, e, i in SCHEDULES) + ";",
        "INSERT INTO staging.metric_allowlist (type_canonical, metric, unit) VALUES "
        + ", ".join(f"('{t}', '{m}', '{u}')" for t, m, u in METRIC_ALLOWLIST) + ";",
        "INSERT INTO access.permission (name, description, is_commercial) VALUES "
        + ", ".join(f"('{n}', '{d}', false)" for n, d in CONNECTOR_PERMISSIONS) + ";",
    ]
    readers = ", ".join(f"'{r}'" for r in CONNECTOR_READERS)
    rows.append(
        "INSERT INTO access.role_permission (role_id, permission)"
        f" SELECT r.id, 'connectors.read' FROM access.role_definition r"
        f" WHERE r.is_system AND r.name IN ({readers});"
    )
    rows.append(
        "INSERT INTO access.role_permission (role_id, permission)"
        " SELECT r.id, 'connectors.manage' FROM access.role_definition r"
        " WHERE r.is_system AND r.name = 'Platform administrator';"
    )
    return "\n".join(rows)


def _ownership() -> str:
    # Functions are executable by PUBLIC by default: take that away first.
    lines = [f"REVOKE ALL ON FUNCTION access.{fn} FROM PUBLIC;"
             for fn in ("require_connector_permission(uuid, text)", *READERS, *WRITERS)]
    lines += [f"GRANT EXECUTE ON FUNCTION access.{fn} TO cloudops_app;"
              for fn in (*READERS, *WRITERS)]
    # Grants first: once ownership moves, the migrator can no longer grant.
    lines.append("GRANT CREATE ON SCHEMA access TO cloudops_access_admin;")
    lines.append("ALTER FUNCTION access.require_connector_permission(uuid, text)"
                 " OWNER TO cloudops_access_admin;")
    for fn in (*READERS, *WRITERS):
        lines.append(f"ALTER FUNCTION access.{fn} OWNER TO cloudops_access_admin;")
    lines.append("REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;")
    return "\n".join(lines)


# Indexes on partitioned parents cannot be built CONCURRENTLY; they are built
# on each partition. Adding a column touched no index, so these are new.
PARENT_INDEXES = """
CREATE INDEX ON cloudops.cost_fact (account_id, invoice_period);  -- partitioned parent
"""

CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cost_finding_tenant_status_idx"
    " ON cloudops.cost_finding (tenant_id, status, updated_at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS directory_principal_tenant_idx"
    " ON cloudops.directory_principal (tenant_id, kind) WHERE deleted_at IS NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS directory_role_assignment_tenant_idx"
    " ON cloudops.directory_role_assignment (tenant_id) WHERE removed_at IS NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS document_tenant_site_idx"
    " ON cloudops.document (tenant_id, site_id) WHERE deleted_at IS NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS knowledge_chunk_source_ref_idx"
    " ON cloudops.knowledge_chunk (source_ref) WHERE source_ref IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cost_fact_load_assembly_idx"
    " ON staging.cost_fact_load (assembly_id, account_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS resource_account_last_seen_idx"
    " ON cloudops.resource (account_id, last_seen) WHERE deleted_at IS NULL",
)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(SECURITY)
    op.execute(FUNCTIONS)
    op.execute(PRIVILEGES)
    op.execute(_seed())
    op.execute(_ownership())
    op.execute(PARENT_INDEXES)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
