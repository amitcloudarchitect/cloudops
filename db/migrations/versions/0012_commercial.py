"""Commercial modules: FinOps, pricing and margin (expand).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-31

Step 16. HLD M10-M12 and Multi-currency; LLD, Row-level security and
Multi-currency; ADR 0019.

Commercial tables. Each is FORCE RLS with ``tenant_isolation`` *and* the
restrictive ``commercial_visibility`` (LLD), so an engineer or a customer inside
the tenant reads no row of them, whatever the API does:

- ``sow`` gains ``target_margin`` (a fraction; step 16 decision: the target is
  per SOW). Nullable: a SOW without one shows no gap, never a gap to zero.
- ``margin_snapshot``: one customer-month, in the contract currency. Recomputed
  daily while ``provisional``; ``closed`` at month end. A closed month that
  restates (billing, an FX correction) gets a new version that names the one
  it ``restates``: the explicit entry, never a silent overwrite. A customer with
  no SOW has a row whose ``not_captured`` says so, never a zero.
- ``margin_variance``: the snapshot's effort by workgroup and activity type,
  with the hours above the priced slab attributed to each.

FinOps tables, tenant-scoped and restricted by account like ``cost_fact``
(ADR 0002, D16):

- ``billing_total``: the providers' own figures for an account and period, the
  reference reconciliation compares with: ``provider_total`` (month to date,
  daily) and ``invoice`` (at close).
- ``cost_reconciliation``: each check, its drift and its verdict. Above 0.5% the
  figure is unverified and ``alerted_at`` is set.
- ``cost_anomaly``: day-over-day and month-over-month changes past threshold.
- ``anomaly_threshold``: a tenant's own thresholds, overriding the platform
  default in ``staging.anomaly_threshold_default``. Amounts carry a currency.

FX (LLD open item L4, step 16 decision: ECB reference rates, with a finance
correction path). ``fx_restatement`` is the explicit entry a correction creates
(HLD, Multi-currency: "a correction creates a new effective-dated rate and an
explicit entry"). ``access.correct_fx_rate`` is the only way the application
changes a rate: staff holding ``fx.manage``.

Margin and FinOps figures are written by the FinOps worker
(``cloudops_connectors.finops``) on the ingest login, under the ``finops`` role
class so the commercial policy admits it. The application only reads them.
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

# Roles the commercial policy admits (LLD; 0001's list, unchanged).
COMMERCIAL_ROLES = ("ceo", "cfo", "finops", "delivery_lead", "commercial", "auditor",
                    "platform_admin")

# Step 16 decision: the platform default anomaly thresholds.
# (granularity, min_ratio, min_amount, currency)
ANOMALY_DEFAULTS = (
    ("day", "0.30", "100", "EUR"),
    ("month", "0.20", "1000", "EUR"),
)

# Default holders of fx.manage: those who own the FX source (HLD: Finance).
FX_MANAGERS = ("CFO", "FinOps", "Platform administrator")

# Daily FinOps work and the ECB feed (HLD, Integration: margin daily, cost daily).
SCHEDULES = (
    ("ecb", "fx", "1 day"),
    ("azure", "cost_export", "1 day"),
    ("azure", "cost_total", "1 day"),
    ("azure", "invoice", "1 day"),
    ("aws", "cost_total", "1 day"),
    ("aws", "invoice", "1 day"),
    ("finops", "daily", "1 day"),
)

DDL = """
ALTER TABLE cloudops.sow ADD COLUMN target_margin numeric(7,6)
    CHECK (target_margin > -1 AND target_margin < 1);

CREATE TABLE cloudops.margin_snapshot (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                  uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    sow_id                     uuid REFERENCES cloudops.sow(id),
    period                     date NOT NULL CHECK (extract(day FROM period) = 1),
    -- The last day the figures cover: the month's last day once closed, the day
    -- before the run while it is still provisional (month to date).
    through                    date NOT NULL,
    version                    integer NOT NULL DEFAULT 1 CHECK (version > 0),
    status                     text NOT NULL CHECK (status IN ('provisional', 'closed')),
    restates                   uuid REFERENCES cloudops.margin_snapshot(id),
    computed_at                timestamptz NOT NULL DEFAULT now(),
    rate_card_version          text NOT NULL,
    not_captured               text CHECK (not_captured IN
                                           ('no_sow', 'no_people_cost', 'no_fx_rate',
                                            'no_cost_data')),
    contract_currency          char(3) CHECK (contract_currency ~ '^[A-Z]{3}$'),
    revenue                    numeric(18,6),
    modelled_cost              numeric(18,6),
    actual_cost                numeric(18,6),
    tooling_cost               numeric(18,6),
    modelled_margin            numeric(18,6),
    actual_margin              numeric(18,6),
    margin_at_prior_rates      numeric(18,6),
    margin_pct                 numeric(14,8),
    band_basis                 text CHECK (band_basis IN ('actual', 'modelled')),
    band                       text,
    target_margin              numeric(7,6),
    gap_to_target              numeric(14,8),
    fte                        numeric(14,6),
    modelled_hours             numeric(14,4),
    effort_hours               numeric(14,4) NOT NULL DEFAULT 0,
    effort_items               integer NOT NULL DEFAULT 0,
    effort_items_without_hours integer NOT NULL DEFAULT 0,
    effort_complete            boolean NOT NULL DEFAULT false,
    unpriced_cis               integer NOT NULL DEFAULT 0,
    exceeds_slab               boolean NOT NULL DEFAULT false,
    -- The month's consumption, in the rate card's currency (it prices on it).
    consumption                numeric(18,6),
    consumption_currency       char(3) CHECK (consumption_currency ~ '^[A-Z]{3}$'),
    -- Rate card currency -> contract currency: the month's average and last month's.
    fx_rate                    numeric(18,8),
    fx_rate_from               date,
    fx_rate_to                 date,
    prior_fx_rate              numeric(18,8),
    prior_fx_rate_from         date,
    prior_fx_rate_to           date,
    UNIQUE (tenant_id, period, version),
    -- Money never travels without its currency.
    CHECK (revenue IS NULL OR contract_currency IS NOT NULL),
    CHECK (consumption IS NULL OR consumption_currency IS NOT NULL),
    -- Captured or saying why not: never a figure and a reason, never neither.
    CHECK ((not_captured IS NULL) = (actual_margin IS NOT NULL))
);

CREATE TABLE cloudops.margin_variance (
    id                  bigserial PRIMARY KEY,
    tenant_id           uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    snapshot_id         uuid NOT NULL REFERENCES cloudops.margin_snapshot(id) ON DELETE CASCADE,
    workgroup           text NOT NULL,
    activity            text NOT NULL,
    hours               numeric(14,4) NOT NULL,
    items               integer NOT NULL,
    items_without_hours integer NOT NULL,
    share               numeric(14,8) NOT NULL,
    excess_hours        numeric(14,4) NOT NULL,
    excess_cost         numeric(18,6) NOT NULL,
    currency            char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    UNIQUE (snapshot_id, workgroup, activity)
);

CREATE TABLE cloudops.billing_total (
    id             bigserial PRIMARY KEY,
    tenant_id      uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id     uuid NOT NULL REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    source_system  text NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('provider_total', 'invoice')),
    invoice_period text NOT NULL,
    as_of          date NOT NULL,
    amount         numeric(18,6) NOT NULL,
    currency       char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    document_ref   text,
    raw_extra      jsonb,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, kind, invoice_period, as_of)
);

CREATE TABLE cloudops.cost_reconciliation (
    id                 bigserial PRIMARY KEY,
    tenant_id          uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id         uuid NOT NULL REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    invoice_period     text NOT NULL,
    kind               text NOT NULL CHECK (kind IN ('provider_total', 'invoice')),
    checked_on         date NOT NULL,
    reference_as_of    date,
    ledger_amount      numeric(18,6) NOT NULL,
    ledger_currency    char(3) CHECK (ledger_currency ~ '^[A-Z]{3}$'),
    reference_amount   numeric(18,6),
    reference_currency char(3) CHECK (reference_currency ~ '^[A-Z]{3}$'),
    drift              numeric(14,8),
    status             text NOT NULL CHECK (status IN ('verified', 'unverified')),
    reason             text CHECK (reason IN
                                   ('drift', 'currency_mismatch', 'reference_zero',
                                    'no_reference', 'mixed_currency')),
    alerted_at         timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, invoice_period, kind, checked_on),
    CHECK ((status = 'verified') = (reason IS NULL))
);

CREATE TABLE cloudops.cost_anomaly (
    id               bigserial PRIMARY KEY,
    tenant_id        uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    account_id       uuid NOT NULL REFERENCES cloudops.cloud_account(id) ON DELETE CASCADE,
    granularity      text NOT NULL CHECK (granularity IN ('day', 'month')),
    observed_on      date NOT NULL,
    observed         numeric(18,6) NOT NULL,
    baseline         numeric(18,6) NOT NULL,
    change           numeric(18,6) NOT NULL,
    ratio            numeric(14,8),
    currency         char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    direction        text NOT NULL CHECK (direction IN ('spike', 'drop')),
    threshold_ratio  numeric(9,6) NOT NULL,
    threshold_amount numeric(18,6) NOT NULL,
    detected_at      timestamptz NOT NULL DEFAULT now(),
    alerted_at       timestamptz,
    UNIQUE (account_id, granularity, observed_on, currency)
);

CREATE TABLE cloudops.anomaly_threshold (
    tenant_id   uuid NOT NULL REFERENCES cloudops.tenant(id) ON DELETE CASCADE,
    granularity text NOT NULL CHECK (granularity IN ('day', 'month')),
    min_ratio   numeric(9,6) NOT NULL CHECK (min_ratio >= 0),
    min_amount  numeric(18,6) NOT NULL CHECK (min_amount >= 0),
    currency    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    PRIMARY KEY (tenant_id, granularity)
);

CREATE TABLE staging.anomaly_threshold_default (
    granularity text PRIMARY KEY CHECK (granularity IN ('day', 'month')),
    min_ratio   numeric(9,6) NOT NULL CHECK (min_ratio >= 0),
    min_amount  numeric(18,6) NOT NULL CHECK (min_amount >= 0),
    currency    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$')
);

-- Reference data like fx_rate: no tenant, protected by grants.
CREATE TABLE cloudops.fx_restatement (
    id             bigserial PRIMARY KEY,
    from_currency  char(3) NOT NULL CHECK (from_currency ~ '^[A-Z]{3}$'),
    to_currency    char(3) NOT NULL CHECK (to_currency ~ '^[A-Z]{3}$'),
    rate_type      text NOT NULL CHECK (rate_type IN ('closing', 'average')),
    effective_date date NOT NULL,
    previous_rate  numeric(18,8),
    new_rate       numeric(18,8) NOT NULL CHECK (new_rate > 0),
    source         text NOT NULL,
    reason         text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
    entered_by     text NOT NULL,
    entered_at     timestamptz NOT NULL DEFAULT now()
);
"""

TENANT_TABLES = (
    "cloudops.margin_snapshot",
    "cloudops.margin_variance",
    "cloudops.billing_total",
    "cloudops.cost_reconciliation",
    "cloudops.cost_anomaly",
    "cloudops.anomaly_threshold",
)
COMMERCIAL_TABLES = ("cloudops.margin_snapshot", "cloudops.margin_variance")
ACCOUNT_TABLES = ("cloudops.billing_total", "cloudops.cost_reconciliation",
                  "cloudops.cost_anomaly")


def _rls(table: str) -> str:
    # FORCE, not just ENABLE: without it the owner bypasses every policy.
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table}
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
"""


def _commercial(table: str) -> str:
    roles = ",".join(f"'{r}'" for r in COMMERCIAL_ROLES)
    return f"""
CREATE POLICY commercial_visibility ON {table}
  AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') IN ({roles}));
"""


def _account(table: str) -> str:
    return f"""
CREATE POLICY account_scope ON {table} AS RESTRICTIVE
  USING (cloudops.account_scope_is_all() OR account_id = ANY(cloudops.current_account_ids()));
"""


SECURITY = (
    "".join(_rls(t) for t in TENANT_TABLES)
    + "".join(_commercial(t) for t in COMMERCIAL_TABLES)
    + "".join(_account(t) for t in ACCOUNT_TABLES)
    + """
-- Margin is per customer, never per subscription: a subscription-limited scope
-- sees none of it (D16), like a directory.
CREATE POLICY account_scope ON cloudops.margin_snapshot AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.margin_variance AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());
CREATE POLICY account_scope ON cloudops.anomaly_threshold AS RESTRICTIVE
  USING (cloudops.account_scope_is_all());

GRANT SELECT ON cloudops.margin_snapshot, cloudops.margin_variance, cloudops.billing_total,
    cloudops.cost_reconciliation, cloudops.cost_anomaly, cloudops.anomaly_threshold,
    cloudops.fx_restatement
    TO cloudops_app;
GRANT SELECT, INSERT, UPDATE ON cloudops.margin_snapshot, cloudops.billing_total,
    cloudops.cost_reconciliation
    TO cloudops_ingest;
-- A recomputed day that no longer crosses its threshold (billing restated) is removed.
GRANT SELECT, INSERT, UPDATE, DELETE ON cloudops.cost_anomaly TO cloudops_ingest;
-- A provisional snapshot's variance is replaced on each daily recompute.
GRANT SELECT, INSERT, UPDATE, DELETE ON cloudops.margin_variance TO cloudops_ingest;
GRANT SELECT ON cloudops.anomaly_threshold, staging.anomaly_threshold_default,
    cloudops.fx_restatement
    TO cloudops_ingest;
-- The ECB feed records a revision of a published rate like a correction.
GRANT INSERT ON cloudops.fx_restatement TO cloudops_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA cloudops TO cloudops_app, cloudops_ingest;
"""
)

FUNCTIONS = """
-- Finance corrects a rate: the rate in force changes and the explicit entry
-- records what it was, who changed it and why, in one transaction. A rate that
-- did not exist is added the same way (previous_rate NULL).
CREATE FUNCTION access.correct_fx_rate(
    p_actor uuid, p_from char(3), p_to char(3), p_rate_type text, p_effective date,
    p_rate numeric, p_reason text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_previous numeric(18,8);
    v_id bigint;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class <> 'customer' AND rp.permission = 'fx.manage'
    ) THEN
        RAISE EXCEPTION 'not permitted to correct FX rates'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_from = p_to THEN
        RAISE EXCEPTION 'a rate needs two currencies' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_rate IS NULL OR p_rate <= 0 THEN
        RAISE EXCEPTION 'a rate is positive' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('fx:' || p_from || p_to || p_rate_type
                                           || p_effective::text));
    SELECT f.rate INTO v_previous FROM cloudops.fx_rate f
    WHERE f.from_currency = p_from AND f.to_currency = p_to AND f.rate_type = p_rate_type
      AND f.effective_date = p_effective;
    IF v_previous = p_rate THEN
        RAISE EXCEPTION 'that is already the rate' USING ERRCODE = 'FXSAM';
    END IF;
    INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate, rate_type,
                                  effective_date, source)
    VALUES (p_from, p_to, p_rate, p_rate_type, p_effective, 'finance')
    ON CONFLICT (from_currency, to_currency, rate_type, effective_date)
    DO UPDATE SET rate = EXCLUDED.rate, source = EXCLUDED.source;
    INSERT INTO cloudops.fx_restatement (from_currency, to_currency, rate_type,
        effective_date, previous_rate, new_rate, source, reason, entered_by)
    VALUES (p_from, p_to, p_rate_type, p_effective, v_previous, p_rate, 'finance',
            btrim(p_reason), p_actor::text)
    RETURNING id INTO v_id;
    RETURN v_id;
END $$;
"""

FUNCTION_SIGNATURE = "correct_fx_rate(uuid, char, char, text, date, numeric, text)"

# The FinOps worker computes for every customer, so it needs the list of them:
# ids only, owned by the resolver (the one role that may list tenants, 0004),
# callable by the ingest login alone. In the cloudops schema beside
# resolve_tenant_alias (0005): the access schema stays closed to the ingest login.
TENANT_LIST = """
CREATE FUNCTION cloudops.finops_tenants() RETURNS uuid[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT coalesce(array_agg(t.id ORDER BY t.id), '{}') FROM cloudops.tenant t
$$;
"""


def _seed() -> str:
    rows = [
        "INSERT INTO staging.anomaly_threshold_default"
        " (granularity, min_ratio, min_amount, currency) VALUES "
        + ", ".join(f"('{g}', {r}, {a}, '{c}')" for g, r, a, c in ANOMALY_DEFAULTS) + ";",
        "INSERT INTO staging.connector_schedule (source_system, entity, every) VALUES "
        + ", ".join(f"('{s}', '{e}', interval '{i}')" for s, e, i in SCHEDULES) + ";",
        "INSERT INTO access.permission (name, description, is_commercial) VALUES"
        " ('fx.manage', 'Correct FX rates (each correction is recorded)', false);",
    ]
    managers = ", ".join(f"'{r}'" for r in FX_MANAGERS)
    rows.append(
        "INSERT INTO access.role_permission (role_id, permission)"
        " SELECT r.id, 'fx.manage' FROM access.role_definition r"
        f" WHERE r.is_system AND r.name IN ({managers});"
    )
    return "\n".join(rows)


def _ownership() -> str:
    fn = f"access.{FUNCTION_SIGNATURE}"
    return "\n".join([
        "REVOKE ALL ON FUNCTION cloudops.finops_tenants() FROM PUBLIC;",
        "GRANT EXECUTE ON FUNCTION cloudops.finops_tenants() TO cloudops_ingest;",
        "GRANT CREATE ON SCHEMA cloudops TO cloudops_resolver;",
        "ALTER FUNCTION cloudops.finops_tenants() OWNER TO cloudops_resolver;",
        "REVOKE CREATE ON SCHEMA cloudops FROM cloudops_resolver;",
        f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC;",
        f"GRANT EXECUTE ON FUNCTION {fn} TO cloudops_app;",
        # The definer writes the rate and its entry, nothing else.
        "GRANT SELECT, INSERT, UPDATE ON cloudops.fx_rate TO cloudops_access_admin;",
        "GRANT SELECT, INSERT ON cloudops.fx_restatement TO cloudops_access_admin;",
        "GRANT USAGE ON SEQUENCE cloudops.fx_rate_id_seq, cloudops.fx_restatement_id_seq"
        " TO cloudops_access_admin;",
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
        f"ALTER FUNCTION {fn} OWNER TO cloudops_access_admin;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;",
    ])


CONCURRENT_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS margin_snapshot_period_idx"
    " ON cloudops.margin_snapshot (tenant_id, period DESC, version DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS margin_variance_snapshot_idx"
    " ON cloudops.margin_variance (tenant_id, snapshot_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS billing_total_period_idx"
    " ON cloudops.billing_total (tenant_id, invoice_period, kind)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cost_reconciliation_period_idx"
    " ON cloudops.cost_reconciliation (tenant_id, invoice_period, checked_on DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cost_anomaly_observed_idx"
    " ON cloudops.cost_anomaly (tenant_id, observed_on DESC)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS fx_restatement_entered_idx"
    " ON cloudops.fx_restatement (entered_at DESC)",
)


def upgrade() -> None:
    op.execute(DDL)
    op.execute(SECURITY)
    op.execute(FUNCTIONS)
    op.execute(TENANT_LIST)
    op.execute(_seed())
    op.execute(_ownership())
    with op.get_context().autocommit_block():
        for statement in CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
