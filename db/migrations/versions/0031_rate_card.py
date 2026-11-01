"""The rate card moves into the database (expand).

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-01

ADR 0033, amending ADR 0019 decision 7. Finance and commercial publish a rate
card version from Settings instead of a code release. What does not change: a
version is immutable and effective-dated, a quote and a margin figure name the
version they used, and the workbook's version reproduces the reference engine.

- ``cloudops.rate_card``: one row per version, with the card's single values.
  ``effective_from`` is unique; there is no ``effective_to``. A version ends where
  the next begins, derived on read, so publishing never touches an earlier row.
- Eleven child tables, one per part of the card (complexity minutes, service
  multipliers, the catalogue, the platform-type map, base pack bands, coverage
  uplift, CI slabs, tools, people cost, the role mix, margin health bands). Each
  carries its position, because the engine reads bands, slabs, health bands and
  the team in order.
- Rates are ``cloudops.rate_input``: an unconstrained ``numeric`` of at most 16
  decimal places. Not ``numeric(18,6)``: these are the inputs the engine
  multiplies (the workbook's day rate is 266.9932626632895), and truncating them
  to six places changes the quote. Every figure computed from them is stored at
  ``numeric(18,6)`` as before. Amounts carry the version's ``currency``.
- Platform-wide reference data, so no ``tenant_id`` (like ``fx_rate``). Every
  table is FORCE RLS with the restrictive ``commercial_visibility``: an engineer
  or a customer scope reads no row, whatever the grants. ``cloudops_customer``
  holds no grant at all (the first lock, ADR 0018).
- History is never edited: a trigger refuses UPDATE and DELETE on every table,
  for the owner too.
- ``access.publish_rate_card(actor, document, reason)`` is the only write path:
  SECURITY DEFINER, owned by ``cloudops_access_admin``, executable by
  ``cloudops_app``. It requires ``ratecard.publish`` (a commercial permission, so
  0004's trigger refuses it to any role class outside the commercial list, and
  never to a customer role), and an ``effective_from`` after today and after the
  latest version: a version can only take effect in the future, so no closed or
  current month is repriced by publishing.
- ``cloudops.rate_card_documents()`` returns every version whole, as the JSON
  document ``cloudops_commercial.ratecard.from_document`` reads (SECURITY
  INVOKER: the caller's RLS applies).

Version ``2026-v2.14`` is seeded from ``db/migrations/data/rate_card_2026-v2.14.json``,
the workbook's constants exactly as ``ratecard.py`` held them, before RLS is
switched on. The pricing-regression job proves the file reproduces the reference
engine; ``db/tests/test_rate_card.py`` proves the database returns it unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

SEED = Path(__file__).resolve().parents[1] / "data" / "rate_card_2026-v2.14.json"

# Roles the commercial policy admits (LLD; 0001's list, unchanged).
COMMERCIAL_ROLES = ("ceo", "cfo", "finops", "delivery_lead", "commercial", "auditor",
                    "platform_admin")
# Default holders of ratecard.publish: who owns pricing (HLD, D6 and D7: commercial
# and Finance), and the platform administrator.
PUBLISHERS = ("CFO", "Commercial", "Platform administrator")

DDL = """
CREATE DOMAIN cloudops.rate_input AS numeric
    CHECK (VALUE >= 0 AND VALUE < 1000000000000 AND scale(VALUE) <= 16);

CREATE TABLE cloudops.rate_card (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version                    text NOT NULL UNIQUE
                               CHECK (version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$'),
    effective_from             date NOT NULL UNIQUE,
    source                     text NOT NULL CHECK (length(btrim(source)) BETWEEN 1 AND 200),
    currency                   char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    blended_day_rate           cloudops.rate_input NOT NULL CHECK (blended_day_rate > 0),
    minutes_per_day            cloudops.rate_input NOT NULL CHECK (minutes_per_day > 0),
    working_days_per_month     cloudops.rate_input NOT NULL CHECK (working_days_per_month > 0),
    hours_per_day              cloudops.rate_input NOT NULL CHECK (hours_per_day > 0),
    base_pack_floor            cloudops.rate_input NOT NULL,
    corestack_consumption_rate cloudops.rate_input NOT NULL,
    repricing_headroom_minimum integer NOT NULL CHECK (repricing_headroom_minimum >= 0),
    repricing_headroom_share   cloudops.rate_input NOT NULL,
    healthy_label              text NOT NULL CHECK (length(btrim(healthy_label)) BETWEEN 1 AND 40),
    published_by               text NOT NULL,
    published_at               timestamptz NOT NULL DEFAULT now(),
    reason                     text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500)
);

CREATE TABLE cloudops.rate_card_complexity (
    rate_card_id uuid NOT NULL REFERENCES cloudops.rate_card(id),
    position     integer NOT NULL,
    complexity   text NOT NULL CHECK (complexity ~ '^([0-9]{1,3}|Case)$'),
    minutes      cloudops.rate_input NOT NULL CHECK (minutes > 0),
    PRIMARY KEY (rate_card_id, complexity),
    UNIQUE (rate_card_id, position)
);

CREATE TABLE cloudops.rate_card_multiplier (
    rate_card_id uuid NOT NULL REFERENCES cloudops.rate_card(id),
    coverage     cloudops.coverage_model NOT NULL,
    support      cloudops.support_model NOT NULL,
    multiplier   cloudops.rate_input NOT NULL CHECK (multiplier > 0),
    PRIMARY KEY (rate_card_id, coverage, support)
);

CREATE TABLE cloudops.rate_card_catalogue (
    rate_card_id  uuid NOT NULL REFERENCES cloudops.rate_card(id),
    position      integer NOT NULL,
    resource_type text NOT NULL CHECK (length(btrim(resource_type)) BETWEEN 1 AND 200),
    complexity    text NOT NULL,
    PRIMARY KEY (rate_card_id, resource_type),
    UNIQUE (rate_card_id, position),
    FOREIGN KEY (rate_card_id, complexity)
        REFERENCES cloudops.rate_card_complexity (rate_card_id, complexity)
);

CREATE TABLE cloudops.rate_card_type_map (
    rate_card_id   uuid NOT NULL REFERENCES cloudops.rate_card(id),
    position       integer NOT NULL,
    type_canonical text NOT NULL CHECK (length(btrim(type_canonical)) BETWEEN 1 AND 200),
    resource_type  text NOT NULL,
    PRIMARY KEY (rate_card_id, type_canonical),
    UNIQUE (rate_card_id, position),
    FOREIGN KEY (rate_card_id, resource_type)
        REFERENCES cloudops.rate_card_catalogue (rate_card_id, resource_type)
);

CREATE TABLE cloudops.rate_card_base_band (
    rate_card_id uuid NOT NULL REFERENCES cloudops.rate_card(id),
    position     integer NOT NULL,
    lower_bound  cloudops.rate_input NOT NULL,
    upper_bound  cloudops.rate_input CHECK (upper_bound > lower_bound),
    rate         cloudops.rate_input NOT NULL,
    PRIMARY KEY (rate_card_id, position)
);

CREATE TABLE cloudops.rate_card_coverage_uplift (
    rate_card_id uuid NOT NULL REFERENCES cloudops.rate_card(id),
    coverage     cloudops.coverage_model NOT NULL,
    uplift       cloudops.rate_input NOT NULL CHECK (uplift > 0),
    PRIMARY KEY (rate_card_id, coverage)
);

CREATE TABLE cloudops.rate_card_slab (
    rate_card_id     uuid NOT NULL REFERENCES cloudops.rate_card(id),
    position         integer NOT NULL,
    label            text NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 200),
    lower_bound      integer NOT NULL CHECK (lower_bound >= 0),
    upper_bound      integer NOT NULL CHECK (upper_bound >= lower_bound),
    transition_weeks integer NOT NULL CHECK (transition_weeks >= 0),
    PRIMARY KEY (rate_card_id, position)
);

CREATE TABLE cloudops.rate_card_tool (
    rate_card_id uuid NOT NULL REFERENCES cloudops.rate_card(id),
    position     integer NOT NULL,
    tool         text NOT NULL CHECK (length(btrim(tool)) BETWEEN 1 AND 200),
    cost_per_ci  cloudops.rate_input NOT NULL,
    is_default   boolean NOT NULL,
    PRIMARY KEY (rate_card_id, tool),
    UNIQUE (rate_card_id, position)
);

-- 'Mixed' has no people cost (ADR 0019): its margin is not captured.
CREATE TABLE cloudops.rate_card_people_cost (
    rate_card_id uuid NOT NULL REFERENCES cloudops.rate_card(id),
    position     integer NOT NULL,
    location     cloudops.delivery_location NOT NULL CHECK (location <> 'Mixed'),
    monthly_cost cloudops.rate_input NOT NULL CHECK (monthly_cost > 0),
    PRIMARY KEY (rate_card_id, location),
    UNIQUE (rate_card_id, position)
);

CREATE TABLE cloudops.rate_card_role (
    rate_card_id uuid NOT NULL REFERENCES cloudops.rate_card(id),
    position     integer NOT NULL,
    role         text NOT NULL CHECK (length(btrim(role)) BETWEEN 1 AND 200),
    share        cloudops.rate_input NOT NULL CHECK (share <= 1),
    PRIMARY KEY (rate_card_id, role),
    UNIQUE (rate_card_id, position)
);

CREATE TABLE cloudops.rate_card_health_band (
    rate_card_id uuid NOT NULL REFERENCES cloudops.rate_card(id),
    position     integer NOT NULL,
    below        numeric(9,6) NOT NULL CHECK (below > -1 AND below < 1),
    label        text NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 40),
    PRIMARY KEY (rate_card_id, position)
);
"""

TABLES = (
    "cloudops.rate_card",
    "cloudops.rate_card_complexity",
    "cloudops.rate_card_multiplier",
    "cloudops.rate_card_catalogue",
    "cloudops.rate_card_type_map",
    "cloudops.rate_card_base_band",
    "cloudops.rate_card_coverage_uplift",
    "cloudops.rate_card_slab",
    "cloudops.rate_card_tool",
    "cloudops.rate_card_people_cost",
    "cloudops.rate_card_role",
    "cloudops.rate_card_health_band",
)

# Writes one version from its document. Not callable by any login: the seed runs
# it as the migrator, and access.publish_rate_card as its definer. Amounts arrive
# as strings and are cast here, so no float is ever involved.
INSERT = """
CREATE FUNCTION cloudops.rate_card_insert(p_doc jsonb, p_by text, p_reason text)
RETURNS uuid
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO cloudops.rate_card (version, effective_from, source, currency,
        blended_day_rate, minutes_per_day, working_days_per_month, hours_per_day,
        base_pack_floor, corestack_consumption_rate, repricing_headroom_minimum,
        repricing_headroom_share, healthy_label, published_by, reason)
    VALUES (p_doc->>'version', (p_doc->>'effective_from')::date, p_doc->>'source',
        p_doc->>'currency', (p_doc->>'blended_day_rate')::numeric,
        (p_doc->>'minutes_per_day')::numeric, (p_doc->>'working_days_per_month')::numeric,
        (p_doc->>'hours_per_day')::numeric, (p_doc->>'base_pack_floor')::numeric,
        (p_doc->>'corestack_consumption_rate')::numeric,
        (p_doc->>'repricing_headroom_minimum')::integer,
        (p_doc->>'repricing_headroom_share')::numeric, p_doc->>'healthy_label',
        p_by, btrim(p_reason))
    RETURNING id INTO v_id;

    INSERT INTO cloudops.rate_card_complexity (rate_card_id, position, complexity, minutes)
    SELECT v_id, n, e->>'complexity', (e->>'minutes')::numeric
    FROM jsonb_array_elements(p_doc->'complexity_minutes') WITH ORDINALITY AS a(e, n);

    INSERT INTO cloudops.rate_card_multiplier (rate_card_id, coverage, support, multiplier)
    SELECT v_id, (e->>'coverage')::cloudops.coverage_model,
           (e->>'support')::cloudops.support_model, (e->>'multiplier')::numeric
    FROM jsonb_array_elements(p_doc->'service_multipliers') AS a(e);

    INSERT INTO cloudops.rate_card_catalogue (rate_card_id, position, resource_type, complexity)
    SELECT v_id, n, e->>'resource_type', e->>'complexity'
    FROM jsonb_array_elements(p_doc->'catalogue') WITH ORDINALITY AS a(e, n);

    INSERT INTO cloudops.rate_card_type_map (rate_card_id, position, type_canonical,
                                             resource_type)
    SELECT v_id, n, e->>'type_canonical', e->>'resource_type'
    FROM jsonb_array_elements(coalesce(p_doc->'type_map', '[]')) WITH ORDINALITY AS a(e, n);

    INSERT INTO cloudops.rate_card_base_band (rate_card_id, position, lower_bound,
                                              upper_bound, rate)
    SELECT v_id, n, (e->>'lower')::numeric, (e->>'upper')::numeric, (e->>'rate')::numeric
    FROM jsonb_array_elements(p_doc->'base_pack_bands') WITH ORDINALITY AS a(e, n);

    INSERT INTO cloudops.rate_card_coverage_uplift (rate_card_id, coverage, uplift)
    SELECT v_id, (e->>'coverage')::cloudops.coverage_model, (e->>'uplift')::numeric
    FROM jsonb_array_elements(p_doc->'base_pack_coverage_uplift') AS a(e);

    INSERT INTO cloudops.rate_card_slab (rate_card_id, position, label, lower_bound,
                                         upper_bound, transition_weeks)
    SELECT v_id, n, e->>'label', (e->>'lower')::integer, (e->>'upper')::integer,
           (e->>'transition_weeks')::integer
    FROM jsonb_array_elements(p_doc->'ci_slabs') WITH ORDINALITY AS a(e, n);

    INSERT INTO cloudops.rate_card_tool (rate_card_id, position, tool, cost_per_ci, is_default)
    SELECT v_id, n, e->>'tool', (e->>'cost_per_ci')::numeric, (e->>'default')::boolean
    FROM jsonb_array_elements(p_doc->'tools') WITH ORDINALITY AS a(e, n);

    INSERT INTO cloudops.rate_card_people_cost (rate_card_id, position, location, monthly_cost)
    SELECT v_id, n, (e->>'location')::cloudops.delivery_location,
           (e->>'monthly_cost')::numeric
    FROM jsonb_array_elements(p_doc->'people_cost') WITH ORDINALITY AS a(e, n);

    INSERT INTO cloudops.rate_card_role (rate_card_id, position, role, share)
    SELECT v_id, n, e->>'role', (e->>'share')::numeric
    FROM jsonb_array_elements(p_doc->'roles') WITH ORDINALITY AS a(e, n);

    INSERT INTO cloudops.rate_card_health_band (rate_card_id, position, below, label)
    SELECT v_id, n, (e->>'below')::numeric, e->>'label'
    FROM jsonb_array_elements(p_doc->'health_bands') WITH ORDINALITY AS a(e, n);

    -- What the engine would refuse, refused here too: a card that cannot price.
    IF (SELECT count(*) FROM cloudops.rate_card_multiplier WHERE rate_card_id = v_id) <> 9
       OR (SELECT count(*) FROM cloudops.rate_card_coverage_uplift
           WHERE rate_card_id = v_id) <> 3
       OR (SELECT coalesce(sum(share), 0) FROM cloudops.rate_card_role
           WHERE rate_card_id = v_id) <> 1
       OR NOT EXISTS (SELECT 1 FROM cloudops.rate_card_slab WHERE rate_card_id = v_id)
       OR NOT EXISTS (SELECT 1 FROM cloudops.rate_card_people_cost WHERE rate_card_id = v_id)
       OR (SELECT count(*) FILTER (WHERE upper_bound IS NULL)
           FROM cloudops.rate_card_base_band WHERE rate_card_id = v_id) <> 1 THEN
        RAISE EXCEPTION 'the rate card cannot price every case'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN v_id;
END $$;
"""

# Every version, whole, oldest first: the document ratecard.from_document reads.
# Rates are cast to text, so the digits round-trip exactly.
DOCUMENTS = """
CREATE FUNCTION cloudops.rate_card_documents()
RETURNS TABLE (id uuid, document jsonb, published_by text, published_at timestamptz,
               reason text)
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT c.id, jsonb_build_object(
        'version', c.version, 'effective_from', c.effective_from::text,
        'source', c.source, 'currency', c.currency,
        'blended_day_rate', c.blended_day_rate::text,
        'minutes_per_day', c.minutes_per_day::text,
        'working_days_per_month', c.working_days_per_month::text,
        'hours_per_day', c.hours_per_day::text,
        'base_pack_floor', c.base_pack_floor::text,
        'corestack_consumption_rate', c.corestack_consumption_rate::text,
        'repricing_headroom_minimum', c.repricing_headroom_minimum::text,
        'repricing_headroom_share', c.repricing_headroom_share::text,
        'healthy_label', c.healthy_label,
        'complexity_minutes', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'complexity', x.complexity, 'minutes', x.minutes::text) ORDER BY x.position), '[]')
            FROM cloudops.rate_card_complexity x WHERE x.rate_card_id = c.id),
        'service_multipliers', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'coverage', x.coverage::text, 'support', x.support::text,
            'multiplier', x.multiplier::text) ORDER BY x.support DESC, x.coverage DESC), '[]')
            FROM cloudops.rate_card_multiplier x WHERE x.rate_card_id = c.id),
        'catalogue', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'resource_type', x.resource_type, 'complexity', x.complexity)
            ORDER BY x.position), '[]')
            FROM cloudops.rate_card_catalogue x WHERE x.rate_card_id = c.id),
        'type_map', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'type_canonical', x.type_canonical, 'resource_type', x.resource_type)
            ORDER BY x.position), '[]')
            FROM cloudops.rate_card_type_map x WHERE x.rate_card_id = c.id),
        'base_pack_bands', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'lower', x.lower_bound::text, 'upper', x.upper_bound::text, 'rate', x.rate::text)
            ORDER BY x.position), '[]')
            FROM cloudops.rate_card_base_band x WHERE x.rate_card_id = c.id),
        'base_pack_coverage_uplift', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'coverage', x.coverage::text, 'uplift', x.uplift::text) ORDER BY x.coverage), '[]')
            FROM cloudops.rate_card_coverage_uplift x WHERE x.rate_card_id = c.id),
        'ci_slabs', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'label', x.label, 'lower', x.lower_bound::text, 'upper', x.upper_bound::text,
            'transition_weeks', x.transition_weeks::text) ORDER BY x.position), '[]')
            FROM cloudops.rate_card_slab x WHERE x.rate_card_id = c.id),
        'tools', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'tool', x.tool, 'cost_per_ci', x.cost_per_ci::text, 'default', x.is_default)
            ORDER BY x.position), '[]')
            FROM cloudops.rate_card_tool x WHERE x.rate_card_id = c.id),
        'people_cost', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'location', x.location::text, 'monthly_cost', x.monthly_cost::text)
            ORDER BY x.position), '[]')
            FROM cloudops.rate_card_people_cost x WHERE x.rate_card_id = c.id),
        'roles', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'role', x.role, 'share', x.share::text) ORDER BY x.position), '[]')
            FROM cloudops.rate_card_role x WHERE x.rate_card_id = c.id),
        'health_bands', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'below', trim_scale(x.below)::text, 'label', x.label) ORDER BY x.position), '[]')
            FROM cloudops.rate_card_health_band x WHERE x.rate_card_id = c.id)
    ), c.published_by, c.published_at, c.reason
    FROM cloudops.rate_card c
    ORDER BY c.effective_from
$$;
"""

PUBLISH = """
-- Finance or commercial publishes a version. Who: ratecard.publish, held as
-- staff by a role whose class the commercial policy admits (0004's trigger keeps
-- it off any other class). When: after today and after the latest version, so
-- nothing already priced is repriced. What: a whole card; the published row is
-- immutable. The request itself is in audit.action_log (the API's audit stage).
CREATE FUNCTION access.publish_rate_card(p_actor uuid, p_doc jsonb, p_reason text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_from date;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM access.principal p
        JOIN access.role_definition r ON r.id = p.role_id
        JOIN access.role_permission rp ON rp.role_id = p.role_id
        WHERE p.oid = p_actor AND p.disabled_at IS NULL AND p.kind = 'staff'
          AND r.role_class <> 'customer' AND rp.permission = 'ratecard.publish'
    ) THEN
        RAISE EXCEPTION 'not permitted to publish a rate card'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    BEGIN
        v_from := (p_doc->>'effective_from')::date;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'effective_from is not a date' USING ERRCODE = 'invalid_parameter_value';
    END;
    -- One publisher at a time decides what "the latest version" is.
    PERFORM pg_advisory_xact_lock(hashtext('rate_card:publish'));
    IF v_from IS NULL OR v_from <= current_date
       OR v_from <= (SELECT max(effective_from) FROM cloudops.rate_card) THEN
        RAISE EXCEPTION 'a new version takes effect after today and after the latest one'
            USING ERRCODE = 'RCDAT';
    END IF;
    IF EXISTS (SELECT 1 FROM cloudops.rate_card WHERE version = p_doc->>'version') THEN
        RAISE EXCEPTION 'that version is already published' USING ERRCODE = 'RCVER';
    END IF;
    RETURN cloudops.rate_card_insert(p_doc, p_actor::text, p_reason);
END $$;
"""


def _immutable(table: str) -> str:
    short = table.split(".")[1]
    return f"""
CREATE TRIGGER {short}_immutable
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION cloudops.reject_rate_card_change();
CREATE TRIGGER {short}_no_truncate
    BEFORE TRUNCATE ON {table}
    FOR EACH STATEMENT EXECUTE FUNCTION cloudops.reject_rate_card_change();
"""


def _rls(table: str) -> str:
    roles = ",".join(f"'{r}'" for r in COMMERCIAL_ROLES)
    # FORCE, not just ENABLE: without it the owner bypasses every policy.
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY rate_card_read ON {table} FOR SELECT USING (true);
CREATE POLICY rate_card_publish ON {table} FOR INSERT TO cloudops_access_admin
  WITH CHECK (true);
CREATE POLICY commercial_visibility ON {table}
  AS RESTRICTIVE
  USING (coalesce(current_setting('app.role', true), '') IN ({roles}));
"""


SECURITY = (
    """
-- A published version is history: nobody edits or removes it, the owner included.
CREATE FUNCTION cloudops.reject_rate_card_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'a published rate card version is immutable (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;
"""
    + "".join(_immutable(t) for t in TABLES)
    + "".join(_rls(t) for t in TABLES)
    + f"""
GRANT SELECT ON {", ".join(TABLES)} TO cloudops_app, cloudops_ingest;
GRANT SELECT, INSERT ON {", ".join(TABLES)} TO cloudops_access_admin;
REVOKE ALL ON FUNCTION cloudops.rate_card_insert(jsonb, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.rate_card_insert(jsonb, text, text)
    TO cloudops_access_admin;
REVOKE ALL ON FUNCTION cloudops.rate_card_documents() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.rate_card_documents() TO cloudops_app, cloudops_ingest;
"""
)

PUBLISH_SIGNATURE = "access.publish_rate_card(uuid, jsonb, text)"


def _ownership() -> str:
    return "\n".join([
        f"REVOKE ALL ON FUNCTION {PUBLISH_SIGNATURE} FROM PUBLIC;",
        f"GRANT EXECUTE ON FUNCTION {PUBLISH_SIGNATURE} TO cloudops_app;",
        "GRANT CREATE ON SCHEMA access TO cloudops_access_admin;",
        f"ALTER FUNCTION {PUBLISH_SIGNATURE} OWNER TO cloudops_access_admin;",
        "REVOKE CREATE ON SCHEMA access FROM cloudops_access_admin;",
    ])


def _permission() -> str:
    publishers = ", ".join(f"'{r}'" for r in PUBLISHERS)
    return (
        "INSERT INTO access.permission (name, description, is_commercial) VALUES"
        " ('ratecard.publish', 'Publish a new rate card version (immutable, effective-dated)',"
        " true);\n"
        "INSERT INTO access.role_permission (role_id, permission)"
        " SELECT r.id, 'ratecard.publish' FROM access.role_definition r"
        f" WHERE r.is_system AND r.name IN ({publishers});\n"
    )


def _seed() -> str:
    doc = json.dumps(json.loads(SEED.read_text(encoding="utf-8")))
    reason = "The workbook constants, moved from ratecard.py (ADR 0033)"
    return (f"SELECT cloudops.rate_card_insert($seed${doc}$seed$::jsonb, 'migration:0031',"
            f" '{reason}');")


def upgrade() -> None:
    op.execute(DDL)
    op.execute(INSERT)
    op.execute(DOCUMENTS)
    # Seeded before row-level security is on: the migrator has no app.role.
    op.execute(_seed())
    op.execute(SECURITY)
    op.execute(PUBLISH)
    op.execute(_ownership())
    op.execute(_permission())


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
