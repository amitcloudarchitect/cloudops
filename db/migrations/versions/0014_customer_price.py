"""Customer price for cloud consumption (expand).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-31

ADR 0021. ``cost_fact`` holds what the cloud provider charges Getronics, which is
Getronics' cost base and commercial (ADR 0002, D10). A customer sees its
consumption at the price Getronics charges instead, computed in the database
from that cost and a per-SOW uplift, and never sees the cost or the uplift.

- ``sow.consumption_uplift``: a fraction (0.08 is 8%) applied to the amortized
  cost of every charge class (user decision, 2026-10-31). Nullable: a SOW
  without one leaves the price *not captured*, never the cost and never a zero
  uplift.
- ``cloudops.customer_price(from, to)``: the price per tenant, account, usage
  date, charge class and transaction currency, that is the amortized cost times
  (1 + the uplift of the one SOW in force on the usage date). No SOW, two
  overlapping SOWs or no uplift on the SOW gives a NULL price: never guess.
  Unrounded. Rounding happens once, at presentation.

**The function is the only way a customer reaches the figure.** It is SECURITY
DEFINER, owned by ``cloudops_resolver``, and only ``cloudops_customer`` may
execute it. Staff never get it, because an engineer who holds ``cost.read``
could divide the price by the cost and read the uplift, which is commercial.
Row-level security still decides which rows the definer sees. ``tenant_isolation``
and ``account_scope`` on ``cost_fact`` apply to every role and read the caller's
``app.*`` settings, so the function returns the caller's tenants and
subscriptions and nothing else. ``commercial_visibility`` on ``sow`` now also
admits ``cloudops_resolver`` (as ``current_user``, which is the definer only
inside its functions). The resolver holds SELECT on four SOW columns: the tenant,
the dates and the uplift. It gets no revenue column.

Reading the raw cost columns is revoked from ``cloudops_customer`` in 0015
(contract), once the API reads this figure instead.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

# 0001's list, unchanged: the roles commercial_visibility admits.
COMMERCIAL_ROLES = ("ceo", "cfo", "finops", "delivery_lead", "commercial", "auditor",
                    "platform_admin")

DDL = """
ALTER TABLE cloudops.sow ADD COLUMN consumption_uplift numeric(7,6)
    CHECK (consumption_uplift >= 0 AND consumption_uplift < 10);
"""


def _sow_policy() -> str:
    roles = ",".join(f"'{r}'" for r in COMMERCIAL_ROLES)
    return f"""
ALTER POLICY commercial_visibility ON cloudops.sow
  USING (coalesce(current_setting('app.role', true), '') IN ({roles})
         OR current_user = 'cloudops_resolver');
"""


FUNCTION = """
CREATE FUNCTION cloudops.customer_price(p_from date, p_to date)
RETURNS TABLE (tenant_id uuid, account_id uuid, usage_date date, charge_class text,
               currency char(3), price numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH day AS (
        SELECT c.tenant_id, c.account_id, c.usage_date, c.charge_class::text AS charge_class,
               c.currency, sum(c.amortized_cost) AS amount
        FROM cloudops.cost_fact c
        WHERE c.usage_date BETWEEN p_from AND p_to
        GROUP BY 1, 2, 3, 4, 5
    )
    SELECT d.tenant_id, d.account_id, d.usage_date, d.charge_class, d.currency,
           d.amount * (1 + u.uplift)
    FROM day d
    LEFT JOIN LATERAL (
        -- Exactly one SOW in force, or no price.
        SELECT CASE WHEN count(*) = 1 THEN max(s.consumption_uplift) END AS uplift
        FROM cloudops.sow s
        WHERE s.tenant_id = d.tenant_id AND s.effective_from <= d.usage_date
          AND (s.effective_to IS NULL OR s.effective_to >= d.usage_date)
    ) u ON true
$$;
"""

SIGNATURE = "cloudops.customer_price(date, date)"

GRANTS = f"""
GRANT SELECT (tenant_id, account_id, usage_date, charge_class, amortized_cost, currency)
    ON cloudops.cost_fact TO cloudops_resolver;
GRANT SELECT (tenant_id, effective_from, effective_to, consumption_uplift)
    ON cloudops.sow TO cloudops_resolver;
GRANT EXECUTE ON FUNCTION cloudops.current_tenant_ids(), cloudops.account_scope_is_all(),
    cloudops.current_account_ids() TO cloudops_resolver;
REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {SIGNATURE} TO cloudops_customer;
GRANT CREATE ON SCHEMA cloudops TO cloudops_resolver;
ALTER FUNCTION {SIGNATURE} OWNER TO cloudops_resolver;
REVOKE CREATE ON SCHEMA cloudops FROM cloudops_resolver;
"""


def upgrade() -> None:
    op.execute(DDL)
    op.execute(_sow_policy())
    op.execute(FUNCTION)
    op.execute(GRANTS)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
