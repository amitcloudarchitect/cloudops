"""One tenant per provider account; many internal tenants (contract).

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-01

ADR 0031. 0029 folded every shared account into its single owner. This drops
what PR 62 added for sharing and what limited the platform to one internal tenant:

- the trigger and function that admitted the internal tenant and one customer on
  one id (``cloud_account_sharing``), and the one that kept the internal tenant
  internal while it shared (``tenant_internal_sharing``), with the policies that
  let their owner read and mark every account and read the internal tenant;
- 0019's ``tenant_one_internal_idx``: many tenants may now be internal.

And adds ``cloud_account_provider_native_key``, unique on (provider, native_id)
again: an account belongs to exactly one tenant. The connectors upsert on
(tenant_id, provider, native_id) (0025's index, kept); an id another tenant holds
now fails that insert with a unique violation, which the credential worker reports
as ``account_held_by_another_tenant`` and the alias path leaves failing loudly.

Dropping the trigger and the index breaks nothing the previous release does: it
reads neither. ``cloud_account.shared_with_platform`` (now always false) and
``cloudops.internal_tenant()`` (now the platform host) are left for the next
contract, a release after the code stopped reading them.
"""

from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

CHANGE_KIND = "contract"

# The functions belong to cloudops_access_admin (0025): only it may drop them.
CONTRACT = """
DROP TRIGGER cloud_account_sharing ON cloudops.cloud_account;
DROP TRIGGER tenant_internal_sharing ON cloudops.tenant;
DROP POLICY account_sharing_read ON cloudops.cloud_account;
DROP POLICY account_sharing_mark ON cloudops.cloud_account;
DROP POLICY internal_tenant_read ON cloudops.tenant;
REVOKE UPDATE (shared_with_platform) ON cloudops.cloud_account FROM cloudops_access_admin;
SET LOCAL ROLE cloudops_access_admin;
DROP FUNCTION cloudops.check_account_sharing();
DROP FUNCTION cloudops.check_internal_sharing();
RESET ROLE;
"""

CONCURRENT = (
    "DROP INDEX CONCURRENTLY IF EXISTS cloudops.tenant_one_internal_idx",
    # One owner per account (ADR 0031). 0029 left no pair for this to trip on.
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS cloud_account_provider_native_key"
    " ON cloudops.cloud_account (provider, native_id)",
)


def upgrade() -> None:
    op.execute(CONTRACT)
    with op.get_context().autocommit_block():
        for statement in CONCURRENT:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("contract-only; roll forward, or restore with PITR")
