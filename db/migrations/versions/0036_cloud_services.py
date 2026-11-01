"""Cloud Services: continuous read-only audit of customers' cloud accounts (expand).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-01

ADR 0038.

- ``cloudops.cloud_check``: the current state of one check on one thing in one
  account. ``kind`` is ``backup`` (a protected item), ``policy`` (a policy state),
  ``advisor`` (a recommendation), ``defender`` (a Defender for Cloud assessment) or
  ``config`` (a configuration rule, or a resource's ``config.settings`` snapshot).
  Unique on (account, kind, native id). Written by the customer-credential worker
  only (ingest); a check its snapshot no longer holds gets ``resolved_at``.
- ``cloudops.cloud_check_event``: the history. One row per change of a check:
  ``opened`` (first seen as an issue), ``changed`` (state, severity or a detail
  value; ``changed`` names which), ``resolved`` and ``reopened``. Insert-only.
- Both are tenant data: FORCE RLS with ``tenant_isolation``, and the restrictive
  ``account_scope`` by their account (D16), as ``security_finding`` (0028).
- Grants: ingest writes (``cloud_check`` insert and update, events insert only);
  the application reads; ``cloudops_customer`` reads, so a customer whose role
  holds the permission sees its own tenant's (ADR 0018). Readers get every column
  but ``raw_ref``, as the quarantine (0034). Free text in them is the
  provider's or the customer's (names, recommendation text): data, never
  instruction.
- ``cloud_services.read``: the console's Cloud Services area and its API. Granted
  to the system staff roles that read inventory, and to two customer roles,
  Architect and Security officer (ADR 0038). Not commercial.
- ``staging.connector_schedule``: the five audit entities are expected every six
  hours (ADR 0037), for connector health.
"""

from __future__ import annotations

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

KINDS = ("backup", "policy", "advisor", "defender", "config")
ENTITIES = ("backup_item", "policy_state", "advisor", "defender", "resource_config")
PERMISSION = "cloud_services.read"
STAFF_ROLES = ("CEO", "CFO", "FinOps", "SecOps", "Delivery lead", "Auditor",
               "Platform administrator", "Engineer")
CUSTOMER_ROLES = ("Architect", "Security officer")
TABLES = ("cloudops.cloud_check", "cloudops.cloud_check_event")
# Every column; readers get all but raw_ref (a blob path is the connector's business).
COLUMNS = {
    "cloudops.cloud_check": (
        "id", "tenant_id", "account_id", "kind", "native_id", "resource_id",
        "resource_native_id", "resource_type", "resource_group", "region", "category", "rule",
        "title", "state", "severity", "source_status", "observed_at", "detail", "fingerprint",
        "first_seen", "last_seen", "resolved_at", "source_system", "raw_ref",
    ),
    "cloudops.cloud_check_event": (
        "id", "tenant_id", "account_id", "check_id", "at", "change", "state_from", "state_to",
        "severity_from", "severity_to", "changed", "raw_ref",
    ),
}


def _list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


DDL = f"""
CREATE TABLE cloudops.cloud_check (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id         uuid NOT NULL REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    kind               text NOT NULL CHECK (kind IN ({_list(KINDS)})),
    native_id          text NOT NULL,
    resource_id        uuid REFERENCES cloudops.resource(id) ON DELETE SET NULL,
    resource_native_id text,
    resource_type      text,
    resource_group     text,
    region             text,
    category           text NOT NULL,
    rule               text NOT NULL,
    title              text NOT NULL,
    state              text NOT NULL CHECK (state IN ('ok', 'issue', 'not_applicable')),
    severity           text NOT NULL CHECK (severity IN ('high', 'medium', 'low', 'info')),
    source_status      text,
    observed_at        timestamptz,
    detail             jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    fingerprint        text NOT NULL,
    first_seen         timestamptz NOT NULL,
    last_seen          timestamptz NOT NULL,
    resolved_at        timestamptz,
    source_system      text NOT NULL,
    raw_ref            text NOT NULL,
    CHECK (last_seen >= first_seen),
    UNIQUE (account_id, kind, native_id)
);

CREATE TABLE cloudops.cloud_check_event (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id    uuid NOT NULL REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    check_id      uuid NOT NULL REFERENCES cloudops.cloud_check(id) ON DELETE CASCADE,
    at            timestamptz NOT NULL,
    change        text NOT NULL CHECK (change IN ('opened', 'changed', 'resolved', 'reopened')),
    state_from    text,
    state_to      text,
    severity_from text,
    severity_to   text,
    changed       text[] NOT NULL DEFAULT '{{}}',
    raw_ref       text NOT NULL
);
"""

SECURITY = "".join(f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t}
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY account_scope ON {t} AS RESTRICTIVE
  USING (cloudops.account_scope_is_all() OR account_id = ANY(cloudops.current_account_ids()));
GRANT SELECT ({", ".join(c for c in COLUMNS[t] if c != "raw_ref")}) ON {t}
    TO cloudops_app, cloudops_customer;
""" for t in TABLES) + """
GRANT SELECT, INSERT, UPDATE ON cloudops.cloud_check TO cloudops_ingest;
GRANT SELECT, INSERT ON cloudops.cloud_check_event TO cloudops_ingest;
"""

ACCESS = f"""
INSERT INTO access.permission (name, description, is_commercial)
VALUES ('{PERMISSION}', 'Cloud Services: backup, configuration, policy and recommendations',
        false);
INSERT INTO access.role_permission (role_id, permission)
SELECT r.id, '{PERMISSION}' FROM access.role_definition r
 WHERE r.is_system AND r.name IN ({_list(STAFF_ROLES)}) AND r.role_class <> 'customer';
INSERT INTO access.role_permission (role_id, permission)
SELECT r.id, '{PERMISSION}' FROM access.role_definition r
 WHERE r.is_system AND r.name IN ({_list(CUSTOMER_ROLES)}) AND r.role_class = 'customer';
"""

SCHEDULES = (
    "INSERT INTO staging.connector_schedule (source_system, entity, every) VALUES "
    + ", ".join(f"('azure', '{e}', interval '6 hours')" for e in ENTITIES)
    + " ON CONFLICT DO NOTHING;"
)

CONCURRENT = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cloud_check_open_idx"
    " ON cloudops.cloud_check (tenant_id, kind, severity) WHERE resolved_at IS NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cloud_check_resource_idx"
    " ON cloudops.cloud_check (account_id, resource_native_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cloud_check_resource_id_idx"
    " ON cloudops.cloud_check (resource_id) WHERE resource_id IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cloud_check_event_check_idx"
    " ON cloudops.cloud_check_event (check_id, at DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cloud_check_event_tenant_idx"
    " ON cloudops.cloud_check_event (tenant_id, at)",
)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(SECURITY)
    op.execute(ACCESS)
    op.execute(SCHEDULES)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
