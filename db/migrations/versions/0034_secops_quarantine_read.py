"""Staff read the resource quarantine for scanner coverage (expand).

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-01

ADR 0036. The SecOps workspace (HLD M13) lists the Qualys hosts that matched no
resource: the resource quarantine the connectors write (LLD, Identity
resolution). Until now only the ingest role and the access administrator's
health function read it.

- ``USAGE`` on schema ``staging`` and column ``SELECT`` on
  ``staging.resource_quarantine`` to ``cloudops_app``. Not ``raw_ref``: a blob
  path is the connector's business, not a screen's.
- Nothing to ``cloudops_customer``. A customer scope runs as that role
  (``SET LOCAL ROLE``, ADR 0018), and ``cloudops_app`` does not inherit it
  either way, so the quarantine stays out of a customer's reach whatever the
  matrix says. The API refuses ``/v1/secops`` to customers as well.

Rows are already held by the table's FORCEd ``tenant_isolation`` policy (0001)
and the restrictive ``account_scope`` policy (0004): a scope sees its own
tenants' quarantine, and a subscription-limited scope sees only rows filed
under its accounts (a Qualys host has none, so none).
"""

from __future__ import annotations

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

QUARANTINE_COLUMNS = (
    "id", "tenant_id", "account_id", "source_system", "external_key", "name", "ip_address",
    "first_seen", "last_seen", "occurrence_count",
)


def upgrade() -> None:
    op.execute(
        "GRANT USAGE ON SCHEMA staging TO cloudops_app;\n"
        f"GRANT SELECT ({', '.join(QUARANTINE_COLUMNS)}) ON staging.resource_quarantine"
        " TO cloudops_app;"
    )


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
