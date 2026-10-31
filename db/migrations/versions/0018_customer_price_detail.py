"""The customer price, without the detail that reveals the uplift (contract).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-31

ADR 0021, decision 9. Review found that the uplift can be inferred. One uplift
multiplies every cost line, and ``customer_price`` returned one row per
account, day, charge class and currency. A customer who knows the provider's
list price for a single line (a support plan, or a marketplace item on a day it
is the only charge) divides to get 1 + uplift, and from that every cost. An
uplift of 0 shows the cost itself. The user chose "uplift above 0, less
detail" (2026-10-31):

- ``sow.consumption_uplift`` must be greater than 0
  (``sow_consumption_uplift_positive``). Added NOT VALID and then validated. If
  any SOW holds 0 the migration stops and rolls back, rather than this migration
  choosing a value: Commercial sets the real uplift, or NULL for not captured.
  Nothing writes the uplift except migrations and the seed, so there is no API
  path to validate.
- ``cloudops.customer_monthly_price(from, to, currency)`` is what a customer may
  call now. It returns one row per tenant, **closed calendar month**, provider and
  transaction currency, with no account, charge class, service or day. Only
  months before the current UTC month are returned, so changing the dates, or
  watching a month to date grow, cannot isolate a day. Given ``currency``, each
  line is converted at the daily average rate in force on its own usage date
  before it is summed (CLAUDE.md rule 5), so the per-day figures never leave the
  function. A month with any unpriced line, or any day without a rate, has a
  NULL figure as a whole.
- ``cloudops.customer_saving_total()`` replaces the per-finding saving: the open
  findings' priced saving per tenant and currency, with how many findings it
  covers.
- EXECUTE on ``customer_price`` and ``customer_saving`` is revoked from
  ``cloudops_customer``. They stay as the resolver's internal building blocks,
  and ``pack_customer_price`` (0017, commercial roles only, who may read the
  uplift anyway) still uses ``customer_price``.

A contract, shipped in the same release as the API change that stops calling the
two functions for customers: the exception 0015 records. Revoking now fails
closed, since an old pod's customer cost card errors during the rollout instead
of returning per-line prices.
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

CHANGE_KIND = "contract"

UPLIFT = """
ALTER TABLE cloudops.sow ADD CONSTRAINT sow_consumption_uplift_positive
    CHECK (consumption_uplift > 0) NOT VALID;
ALTER TABLE cloudops.sow VALIDATE CONSTRAINT sow_consumption_uplift_positive;
"""

# As the resolver, which owns the per-line functions: a SQL function's body is
# checked at creation, and the migration role may not execute them, nor revoke
# what their owner granted.
FUNCTIONS = """
GRANT CREATE ON SCHEMA cloudops TO cloudops_resolver;
SET LOCAL ROLE cloudops_resolver;

CREATE FUNCTION cloudops.customer_monthly_price(p_from date, p_to date, p_currency char(3))
RETURNS TABLE (tenant_id uuid, period date, provider text, currency char(3), price numeric,
               converted numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH bounds AS (
        SELECT date_trunc('month', p_from)::date AS first_day,
               least((date_trunc('month', p_to) + interval '1 month - 1 day')::date,
                     (date_trunc('month', (now() AT TIME ZONE 'UTC')::date)
                      - interval '1 day')::date) AS last_day
    )
    SELECT p.tenant_id, date_trunc('month', p.usage_date)::date, a.provider, p.currency,
           CASE WHEN bool_and(p.price IS NOT NULL) THEN sum(p.price) END,
           CASE WHEN bool_and(p.price IS NOT NULL AND r.rate IS NOT NULL)
                THEN sum(p.price * r.rate) END
    FROM bounds b
    CROSS JOIN LATERAL cloudops.customer_price(b.first_day, b.last_day) p
    JOIN cloudops.cloud_account a ON a.id = p.account_id
    LEFT JOIN LATERAL (
        SELECT CASE WHEN p_currency IS NULL THEN NULL
                    WHEN p.currency = p_currency THEN 1::numeric
                    ELSE (SELECT f.rate FROM cloudops.fx_rate f
                          WHERE f.from_currency = p.currency AND f.to_currency = p_currency
                            AND f.rate_type = 'average' AND f.effective_date <= p.usage_date
                          ORDER BY f.effective_date DESC LIMIT 1) END AS rate
    ) r ON true
    GROUP BY 1, 2, 3, 4
$$;

CREATE FUNCTION cloudops.customer_saving_total()
RETURNS TABLE (tenant_id uuid, currency char(3), findings bigint, saving numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT s.tenant_id, s.currency, count(*),
           CASE WHEN bool_and(s.saving IS NOT NULL) THEN sum(s.saving) END
    FROM cloudops.customer_saving() s
    JOIN cloudops.cost_finding f ON f.id = s.id
    WHERE f.status = 'open' AND s.currency IS NOT NULL
    GROUP BY 1, 2
$$;

-- As the owner: only it may take the per-line functions away from customers.
REVOKE EXECUTE ON FUNCTION cloudops.customer_price(date, date), cloudops.customer_saving()
    FROM cloudops_customer;
REVOKE ALL ON FUNCTION cloudops.customer_monthly_price(date, date, char),
    cloudops.customer_saving_total() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.customer_monthly_price(date, date, char),
    cloudops.customer_saving_total() TO cloudops_customer;

RESET ROLE;
REVOKE CREATE ON SCHEMA cloudops FROM cloudops_resolver;
"""




# What the new functions read, granted before they are created.
READS = """
GRANT SELECT (id, provider) ON cloudops.cloud_account TO cloudops_resolver;
GRANT SELECT (status) ON cloudops.cost_finding TO cloudops_resolver;
GRANT SELECT (from_currency, to_currency, rate, rate_type, effective_date)
    ON cloudops.fx_rate TO cloudops_resolver;
"""


def upgrade() -> None:
    op.execute(UPLIFT)
    op.execute(READS)
    op.execute(FUNCTIONS)


def downgrade() -> None:
    raise NotImplementedError("contract; roll forward, or restore with PITR")
