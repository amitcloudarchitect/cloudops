"""Customer scopes stop reading Getronics' cloud cost (contract).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-31

ADR 0021. 0011 granted ``cloudops_customer`` SELECT on
``cost_fact.unblended_cost`` and ``amortized_cost`` on the assumption that they
were the customer's bill. They are what the provider charges Getronics (user,
2026-10-31): Getronics' cost base, from which margin can be inferred, so they are
commercial (ADR 0002, D10). This migration revokes them, and
``cost_finding.estimated_saving``, which CoreStack states in the same
provider-cost terms. A customer reads its
consumption as a price through ``cloudops.customer_price`` and a finding's
saving through ``cloudops.customer_saving`` (0014); a query naming any of the
three columns fails with ``permission denied``.

**A recorded exception to "a contract ships a release after its expand"
(0003, LLD).** That rule exists so the running release keeps working while the
schema changes. The previous release's use of these columns for customers *is*
the defect: while it runs, the grant exposes cost. Revoking in the same release
fails closed. A customer cost card on an old pod errors during the rollout
instead of leaking. No customer is onboarded on any environment yet (ADR 0021),
so nothing is lost.
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

CHANGE_KIND = "contract"

CONTRACT = """
REVOKE SELECT (unblended_cost, amortized_cost) ON cloudops.cost_fact FROM cloudops_customer;
REVOKE SELECT (estimated_saving) ON cloudops.cost_finding FROM cloudops_customer;
"""


def upgrade() -> None:
    op.execute(CONTRACT)


def downgrade() -> None:
    raise NotImplementedError("contract; roll forward, or restore with PITR")
