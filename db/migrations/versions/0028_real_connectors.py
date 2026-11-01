"""SharePoint folders, CoreStack security findings, snapshot scopes (expand).

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-01

ADR 0028: SharePoint, Qualys and CoreStack read for real, read-only, in dev.

- ``sharepoint_folder``: a customer's folder, granted to ``app-cloudops-sharepoint``
  under ListItems.SelectedOperations.Selected (one grant per folder, never the
  site). The folder's name is the customer alias (``sharepoint``); its tenant is
  the one that alias resolved to when it was first read. Tenant data, FORCE RLS,
  and tenant-wide like a site: an account-restricted scope sees none of it.
- ``document.folder_id``: the folder a document was read under. A folder is read
  as a whole snapshot each run (Graph's delta does not work below a drive's root),
  so a document missing from the snapshot was deleted; this is how it is found.
- ``security_finding``: CoreStack's security and compliance recommendations (and
  later other sources'), per resource. Shaped like ``cost_finding`` without money,
  restricted the same way: by tenant, then by account.
- ``source_scope`` on both finding tables: the source's own scope a row was read
  under (a CoreStack tenant). CoreStack has no change feed, so each run reads the
  scope whole and an open finding missing from it is resolved.
- The schedule for ``sharepoint``/``folder_item``, so connector health shows it.
"""

from __future__ import annotations

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

DDL = """
CREATE TABLE cloudops.sharepoint_folder (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    drive_id       text NOT NULL,
    item_id        text NOT NULL,
    site_id        text,
    name           text NOT NULL,
    web_url        text,
    consent        text NOT NULL DEFAULT 'granted' CHECK (consent IN ('granted', 'revoked')),
    first_seen     timestamptz NOT NULL DEFAULT now(),
    last_synced_at timestamptz,
    revoked_at     timestamptz,
    CHECK ((consent = 'revoked') = (revoked_at IS NOT NULL)),
    UNIQUE (drive_id, item_id)
);

ALTER TABLE cloudops.document ADD COLUMN folder_id text;

CREATE TABLE cloudops.security_finding (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id             uuid REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    resource_id            uuid REFERENCES cloudops.resource(id),
    source_system          text NOT NULL,
    source_scope           text NOT NULL,
    finding_id             text NOT NULL,
    category               text NOT NULL CHECK (category IN ('security', 'compliance')),
    subcategory            text,
    severity               text NOT NULL CHECK (severity IN ('high', 'medium', 'low')),
    title                  text NOT NULL,
    policy_name            text,
    detector               text,
    status                 text NOT NULL CHECK (status IN ('open', 'resolved', 'dismissed')),
    customer_raw           text NOT NULL,
    account_native_id      text,
    resource_native_id     text,
    detected_at            timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,
    resolved_at            timestamptz,
    raw_extra              jsonb,
    ingested_at            timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
    UNIQUE (source_system, finding_id)
);

ALTER TABLE cloudops.cost_finding ADD COLUMN source_scope text;
"""

TENANT_TABLES = ("cloudops.sharepoint_folder", "cloudops.security_finding")

SECURITY = "".join(f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t}
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
""" for t in TENANT_TABLES) + """
-- D16, as 0010: a folder is tenant-wide like a site; a finding follows its account.
CREATE POLICY account_scope ON cloudops.sharepoint_folder AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.security_finding AS RESTRICTIVE
  USING (cloudops.account_scope_is_all() OR account_id = ANY(cloudops.current_account_ids()));

GRANT SELECT ON cloudops.sharepoint_folder, cloudops.security_finding TO cloudops_app;
GRANT SELECT, INSERT, UPDATE ON cloudops.sharepoint_folder, cloudops.security_finding
    TO cloudops_ingest;
"""

SCHEDULES = """
INSERT INTO staging.connector_schedule (source_system, entity, every)
VALUES ('sharepoint', 'folder_item', interval '1 day')
ON CONFLICT DO NOTHING;
"""

CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS document_folder_idx"
    " ON cloudops.document (source_system, folder_id) WHERE folder_id IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS security_finding_scope_idx"
    " ON cloudops.security_finding (source_system, source_scope, status)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cost_finding_scope_idx"
    " ON cloudops.cost_finding (source_system, source_scope, status)"
    " WHERE source_scope IS NOT NULL",
)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(SECURITY)
    op.execute(SCHEDULES)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
