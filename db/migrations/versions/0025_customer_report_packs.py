"""Customers read their published report packs (expand).

Revision ID: 0025
Revises: 0020
Create Date: 2026-11-01

ADR 0030 (user decision, 2026-11-01). The portal's home shows the customer its
latest report pack, so the portal now decides what ADR 0020 left to it: which
report columns a customer reads. Numbered 0025 because 0021-0024 are taken by
open pull requests; whichever merges after them rebases ``down_revision``.

- ``reports.read`` for the default customer roles CXO and VP or director (0011).
  No other customer role gains it; administrators may still edit the matrix.
- Column grants to ``cloudops_customer``, the role a customer scope runs as
  (ADR 0018, D1). The pack's snapshot and dates; the customer's price (0017);
  a section's published text. Not who generated, approved, published or edited
  it (Getronics principals), and not the AI drafts or their state.
- Nothing on ``report_pack_commercial``: margin, cost base and SOW revenue stay
  out of reach of a customer whatever ``commercial_visibility`` says.

Rows are already limited by the policies 0013 and 0017 wrote with this grant in
mind: ``published_only_for_customers`` (packs and narratives) and
``price_visibility`` (the price, once published), plus tenant isolation and the
account-scope restriction (a subscription-limited customer sees no pack).
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0020"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

CUSTOMER_COLUMNS: dict[str, tuple[str, ...]] = {
    "cloudops.report_pack": (
        "id", "tenant_id", "period", "version", "status", "contract_currency",
        "generated_at", "approved_at", "published_at", "data_as_of", "sections",
    ),
    "cloudops.report_pack_price": ("pack_id", "tenant_id", "sections"),
    "cloudops.report_narrative": ("pack_id", "tenant_id", "section", "text", "edited_at"),
}
REPORT_ROLES = ("CXO", "VP or director")


def _grants() -> str:
    return "\n".join(
        f"GRANT SELECT ({', '.join(columns)}) ON {table} TO cloudops_customer;"
        for table, columns in CUSTOMER_COLUMNS.items()
    )


def _roles() -> str:
    names = ", ".join(f"'{n}'" for n in REPORT_ROLES)
    return f"""
INSERT INTO access.role_permission (role_id, permission)
SELECT r.id, 'reports.read' FROM access.role_definition r
WHERE r.is_system AND r.role_class = 'customer' AND r.name IN ({names})
ON CONFLICT DO NOTHING;
"""


def upgrade() -> None:
    op.execute(_grants())
    op.execute(_roles())


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
