"""A provider id is unique per tenant, not globally (contract).

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-31

ADR 0027 and ADR 0023 (amended). The platform's own dev subscription is also the
first onboarded customer's ("Azure Sponsorship"), so one subscription id is held
by two tenants. 0025 added the per-tenant unique index the connectors now upsert
on (``ON CONFLICT (tenant_id, provider, native_id)``) and the trigger that admits a
shared id only between the internal tenant and one customer. This drops the old
global ``UNIQUE (provider, native_id)``, which would refuse the second row.

**A recorded exception to "a contract ships a release after its expand"** (as
0015). The previous release's connectors upsert ``ON CONFLICT (provider,
native_id)``, which needs the constraint dropped here: while an old connector pod
runs after this migration, its account upsert fails and Temporal retries the page
until the new pod replaces it. Nothing is written wrongly; it waits. Holding the
contract back a release would hold back the onboarding it exists for.
"""

from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

CHANGE_KIND = "contract"

CONTRACT = """
ALTER TABLE cloudops.cloud_account DROP CONSTRAINT cloud_account_provider_native_id_key;
"""


def upgrade() -> None:
    op.execute(CONTRACT)


def downgrade() -> None:
    raise NotImplementedError("contract-only; roll forward, or restore with PITR")
