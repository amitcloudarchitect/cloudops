"""The customer price, for a report pack (expand).

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-31

ADR 0021 (amended). A customer's monthly pack shows its cloud consumption at
the price Getronics charges, never Getronics' cost. Packs are generated under a
staff scope (ADR 0020), so they need the price as of generation. That way the
published pack is a fixed record, and the person who approves it sees the
figure the customer will see (user decision, 2026-10-31: computed at
generation, not when a customer opens the pack).

``cloudops.customer_price`` itself stays customer-only. Price over cost is the
uplift, so a staff role that reads cost must not reach the price unless it may
read the uplift anyway. ``cloudops.pack_customer_price(tenant, from, to)`` is a
narrow wrapper over it:

- one tenant, one price per usage date and transaction currency, unrounded.
  A day with any unpriced line (no uplift, no SOW, or two SOWs in force) is a
  NULL price for the whole day, never the priced part alone;
- executable by ``cloudops_app`` (staff), and it raises unless ``app.role`` is a
  commercial role: the roles ``commercial_visibility`` already lets read the
  SOW, so the uplift, and the only ones that hold ``reports.manage``. A
  customer scope runs as ``cloudops_customer``, which is not a member of
  ``cloudops_app`` and gets no grant;
- SECURITY DEFINER, owned by ``cloudops_resolver``, which owns
  ``customer_price`` and so may call it. Row-level security on ``cost_fact``
  still reads the caller's ``app.*`` settings, so the caller's tenants are all
  it can price.

**Where the price is stored: ``report_pack_price``, never ``report_pack``.**
``report_pack.sections`` is read by every ``reports.read`` holder, and
``reports.read`` and ``cost.read`` are both non-commercial permissions: an
administrator may grant both to an Engineer-class role, which would then
divide the price by the cost and read the uplift. So the executive summary as
the customer sees it (the price, no cost) goes to its own table, one row per
pack, written with it (no UPDATE grant):

- ``price_visibility`` (RESTRICTIVE, SELECT): a commercial role, or a customer
  scope once the pack is published. Any other role reads no row, and gets the
  pack's summary with neither price nor cost.
- ``price_written_by_commercial`` (RESTRICTIVE, INSERT): commercial roles only.
- ``cloudops_customer`` is granted nothing here, as with ``report_pack`` (0013):
  the portal's grant on report packs must include this table.
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

# 0001's list, unchanged: the roles commercial_visibility admits.
COMMERCIAL_ROLES = ("ceo", "cfo", "finops", "delivery_lead", "commercial", "auditor",
                    "platform_admin")

SIGNATURE = "cloudops.pack_customer_price(uuid, date, date)"


TABLE = """
CREATE TABLE cloudops.report_pack_price (
    pack_id   uuid PRIMARY KEY REFERENCES cloudops.report_pack(id) ON DELETE CASCADE,
    tenant_id uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    sections  jsonb NOT NULL CHECK (jsonb_typeof(sections) = 'object')
);
"""


def _security() -> str:
    roles = ",".join(f"'{r}'" for r in COMMERCIAL_ROLES)
    commercial = f"coalesce(current_setting('app.role', true), '') IN ({roles})"
    # FORCE, not just ENABLE: without it the owner bypasses every policy.
    return f"""
ALTER TABLE cloudops.report_pack_price ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloudops.report_pack_price FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cloudops.report_pack_price
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
CREATE POLICY account_scope ON cloudops.report_pack_price AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY price_visibility ON cloudops.report_pack_price AS RESTRICTIVE FOR SELECT
  USING ({commercial}
         OR (current_setting('app.role', true) = 'customer'
             AND EXISTS (SELECT 1 FROM cloudops.report_pack p
                         WHERE p.id = pack_id AND p.status = 'published')));
CREATE POLICY price_written_by_commercial ON cloudops.report_pack_price AS RESTRICTIVE
  FOR INSERT WITH CHECK ({commercial});
-- Written with its pack and never changed, as report_pack_commercial: no UPDATE.
GRANT SELECT, INSERT ON cloudops.report_pack_price TO cloudops_app;
"""


def _function() -> str:
    roles = ",".join(f"'{r}'" for r in COMMERCIAL_ROLES)
    return f"""
CREATE FUNCTION cloudops.pack_customer_price(p_tenant uuid, p_from date, p_to date)
RETURNS TABLE (usage_date date, currency char(3), price numeric)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF coalesce(current_setting('app.role', true), '') NOT IN ({roles}) THEN
        RAISE EXCEPTION 'permission denied for function pack_customer_price'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN QUERY
        SELECT p.usage_date, p.currency,
               CASE WHEN bool_and(p.price IS NOT NULL) THEN sum(p.price) END
        FROM cloudops.customer_price(p_from, p_to) p
        WHERE p.tenant_id = p_tenant
        GROUP BY p.usage_date, p.currency;
END
$$;
"""


GRANTS = f"""
REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {SIGNATURE} TO cloudops_app;
GRANT CREATE ON SCHEMA cloudops TO cloudops_resolver;
ALTER FUNCTION {SIGNATURE} OWNER TO cloudops_resolver;
REVOKE CREATE ON SCHEMA cloudops FROM cloudops_resolver;
"""


def upgrade() -> None:
    op.execute(TABLE)
    op.execute(_security())
    op.execute(_function())
    op.execute(GRANTS)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
