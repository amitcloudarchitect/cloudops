"""Connector sync: every six hours, and once as soon as a customer is onboarded (expand).

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-01

ADR 0037.

- ``staging.connector_schedule``: SharePoint (``drive_item``, ``folder_item``),
  Qualys (``host_detection``) and CoreStack (``finding``) are expected every six
  hours, not daily. Connector health reads this as each entity's expected interval;
  the Temporal schedules themselves are each connector's (``cadence.py``).
- ``staging.sync_request``: a request that one source sync now, outside its
  interval. Rows are queued by the database, never by a client:

    a tenant onboarded (``cloudops.tenant`` insert)          every source below
    an alias approved (``cloudops.tenant_alias`` insert)     that alias's source
    an account added (``cloudops.cloud_account`` insert)     corestack, qualys

  An approved alias and a passed credential check (which creates the credential's
  accounts) both arrive as these inserts, through the definer functions that make
  them (0009, 0025, 0029), so no function changes and no new path writes.
  ``SYNC_SOURCES`` are the sources that resolve a customer by name and run
  platform-wide: the customer's own cloud credential already starts its own first
  sync (customer worker). At most one waiting request per source: more coalesce.
  The source's worker claims its own (``claimed_at``) and triggers its schedules.
- The table holds a source name, a reason code and two times: no tenant, no name,
  nothing a customer sent. Ingest state like ``connector_schedule``, so no tenant
  column and no row-level security; protected by grants. The queuing trigger
  function is SECURITY DEFINER (owned by the migration owner, as the table), so the
  roles that onboard (``cloudops_access_admin``, ``cloudops_ingest``) need no grant
  on the table to queue, and the application gets none at all.
"""

from __future__ import annotations

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

EVERY = "6 hours"
SIX_HOURLY = (
    ("sharepoint", "drive_item"),
    ("sharepoint", "folder_item"),
    ("qualys", "host_detection"),
    ("corestack", "finding"),
)
SYNC_SOURCES = ("sharepoint", "qualys", "corestack")
ACCOUNT_SOURCES = ("corestack", "qualys")  # their findings link to accounts and resources


def _sources(names: tuple[str, ...]) -> str:
    return "ARRAY[" + ", ".join(f"'{n}'" for n in names) + "]::text[]"


DDL = f"""
UPDATE staging.connector_schedule SET every = interval '{EVERY}'
 WHERE (source_system, entity) IN ({", ".join(f"('{s}', '{e}')" for s, e in SIX_HOURLY)});

CREATE TABLE staging.sync_request (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_system text NOT NULL CHECK (source_system = ANY({_sources(SYNC_SOURCES)})),
    reason        text NOT NULL
                  CHECK (reason IN ('tenant_onboarded', 'alias_approved', 'account_added')),
    requested_at  timestamptz NOT NULL DEFAULT now(),
    claimed_at    timestamptz,
    CHECK (claimed_at IS NULL OR claimed_at >= requested_at)
);
CREATE FUNCTION staging.queue_sync() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_sources text[];
    v_reason  text;
BEGIN
    IF TG_TABLE_NAME = 'tenant' THEN
        v_sources := {_sources(SYNC_SOURCES)};
        v_reason := 'tenant_onboarded';
    ELSIF TG_TABLE_NAME = 'tenant_alias' THEN
        v_sources := ARRAY[NEW.source_system]::text[];
        v_reason := 'alias_approved';
    ELSE
        v_sources := {_sources(ACCOUNT_SOURCES)};
        v_reason := 'account_added';
    END IF;
    INSERT INTO staging.sync_request (source_system, reason)
    SELECT s, v_reason FROM unnest(v_sources) s
     WHERE s = ANY({_sources(SYNC_SOURCES)})
    ON CONFLICT (source_system) WHERE claimed_at IS NULL DO NOTHING;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION staging.queue_sync() FROM PUBLIC;

-- The connector workers claim their own source's requests and clear old ones.
GRANT SELECT, UPDATE (claimed_at), DELETE ON staging.sync_request TO cloudops_ingest;
"""

# One waiting request per source: a second, while one waits, adds nothing. The
# triggers' ON CONFLICT needs it, so they are created after it.
CONCURRENT = (
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS sync_request_waiting_idx"
    " ON staging.sync_request (source_system) WHERE claimed_at IS NULL",
)

TRIGGERS = """
CREATE TRIGGER queue_sync AFTER INSERT ON cloudops.tenant
    FOR EACH ROW EXECUTE FUNCTION staging.queue_sync();
CREATE TRIGGER queue_sync AFTER INSERT ON cloudops.tenant_alias
    FOR EACH ROW EXECUTE FUNCTION staging.queue_sync();
CREATE TRIGGER queue_sync AFTER INSERT ON cloudops.cloud_account
    FOR EACH ROW EXECUTE FUNCTION staging.queue_sync();
"""


def upgrade() -> None:
    op.execute(DDL)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT:
            op.execute(statement)
    op.execute(TRIGGERS)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
