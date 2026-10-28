"""Connector ingest: raw_extra, alias resolution, run log and dead letters (expand).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28

LLD, Connector contract and Identity resolution; ADR 0004.

- ``work_item.raw_extra``: source fields the normaliser does not map are kept,
  never discarded.
- ``staging.legal_suffix``: the legal-suffix list identity resolution strips,
  held as configuration (a table), not code. Seeded with the LLD's list.
- ``cloudops.normalise_company(text)``: lowercase, strip punctuation, drop
  legal-suffix tokens, close up the spaces.
- ``cloudops.resolve_tenant_alias(source, alias)``: exact match on source +
  alias, then a normalised match across every registered alias, then nothing.
  Ambiguity (two tenants) is nothing too: never guess. SECURITY DEFINER, owned
  by cloudops_resolver (it already resolves identities before scope exists),
  executable by the ingest role only. It returns a tenant id and an outcome,
  never alias text, so a connector learns nothing about other tenants.
- ``staging.ingest_run``: one row per connector run, the health report.
- ``staging.ingest_dead_letter``: pages that failed after the retries, with the
  bronze reference that replays them.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

CHANGE_KIND = "expand"

# LLD, Identity resolution: "limited, ltd, bv, gmbh, sa, plc, inc, group,
# holdings, services, held as configuration not code".
LEGAL_SUFFIXES = (
    "limited", "ltd", "bv", "gmbh", "sa", "plc", "inc", "group", "holdings", "services",
)

DDL = """
ALTER TABLE cloudops.work_item ADD COLUMN raw_extra jsonb;

CREATE TABLE staging.legal_suffix (
    suffix text PRIMARY KEY CHECK (suffix ~ '^[a-z0-9]+$')
);

CREATE TABLE staging.ingest_run (
    id              uuid PRIMARY KEY,
    source_system   text NOT NULL,
    entity          text NOT NULL,
    mode            text NOT NULL CHECK (mode IN ('incremental', 'replay')),
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz,
    status          text NOT NULL
                    CHECK (status IN ('running', 'succeeded', 'dead_lettered', 'failed')),
    pages           integer NOT NULL DEFAULT 0,
    records         integer NOT NULL DEFAULT 0,
    written         integer NOT NULL DEFAULT 0,
    quarantined     integer NOT NULL DEFAULT 0,
    rejected        integer NOT NULL DEFAULT 0,  -- malformed: no key, kept in bronze
    ci_unlinked     integer NOT NULL DEFAULT 0,
    watermark       timestamptz,
    lag_seconds     bigint
);

CREATE TABLE staging.ingest_dead_letter (
    id             bigserial PRIMARY KEY,
    run_id         uuid NOT NULL REFERENCES staging.ingest_run(id),
    source_system  text NOT NULL,
    entity         text NOT NULL,
    raw_ref        text NOT NULL,
    error          text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    replayed_at    timestamptz
);

GRANT SELECT, INSERT, UPDATE ON staging.ingest_run, staging.ingest_dead_letter TO cloudops_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA staging TO cloudops_ingest;
GRANT SELECT ON staging.legal_suffix TO cloudops_ingest, cloudops_resolver;
GRANT USAGE ON SCHEMA staging TO cloudops_resolver;

-- Normalisation, LLD: lowercase, strip punctuation and legal suffixes. Tokens
-- are split on whitespace before punctuation goes, so "B.V." becomes "bv" (a
-- suffix) and "Tail Spin" closes up to match "Tailspin".
CREATE FUNCTION cloudops.normalise_company(p_name text) RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE SET search_path = pg_catalog, pg_temp AS $$
    SELECT coalesce(string_agg(tok, '' ORDER BY ord), '')
    FROM unnest(regexp_split_to_array(lower(btrim(coalesce(p_name, ''))), '\\s+'))
         WITH ORDINALITY AS t(raw, ord),
         LATERAL (SELECT regexp_replace(raw, '[^a-z0-9]', '', 'g') AS tok) c
    WHERE tok <> ''
      AND NOT EXISTS (SELECT 1 FROM staging.legal_suffix s WHERE s.suffix = tok)
$$;

-- The resolver reads every tenant's aliases; only through this function.
GRANT SELECT (tenant_id, source_system, alias) ON cloudops.tenant_alias TO cloudops_resolver;
CREATE POLICY alias_registry_read ON cloudops.tenant_alias FOR SELECT
    TO cloudops_resolver USING (true);

CREATE FUNCTION cloudops.resolve_tenant_alias(p_source text, p_alias text)
RETURNS TABLE (tenant_id uuid, outcome text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_norm text := cloudops.normalise_company(p_alias);
    v_ids uuid[];
BEGIN
    -- 1. Exact: this source, this spelling.
    SELECT array_agg(DISTINCT a.tenant_id) INTO v_ids
    FROM cloudops.tenant_alias a
    WHERE a.source_system = p_source AND a.alias = p_alias;
    IF cardinality(v_ids) = 1 THEN
        RETURN QUERY SELECT v_ids[1], 'exact'::text;
        RETURN;
    END IF;
    -- 2. Normalised, across every registered alias of every source.
    IF v_norm <> '' THEN
        SELECT array_agg(DISTINCT a.tenant_id) INTO v_ids
        FROM cloudops.tenant_alias a
        WHERE cloudops.normalise_company(a.alias) = v_norm;
        IF cardinality(v_ids) = 1 THEN
            RETURN QUERY SELECT v_ids[1], 'normalised'::text;
            RETURN;
        END IF;
        IF cardinality(v_ids) > 1 THEN
            RETURN QUERY SELECT NULL::uuid, 'ambiguous'::text;
            RETURN;
        END IF;
    END IF;
    -- 3. Nothing: the caller quarantines. Never a guess.
    RETURN QUERY SELECT NULL::uuid, 'unmatched'::text;
END $$;

REVOKE ALL ON FUNCTION cloudops.resolve_tenant_alias(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cloudops.resolve_tenant_alias(text, text) TO cloudops_ingest;
GRANT EXECUTE ON FUNCTION cloudops.normalise_company(text) TO cloudops_resolver, cloudops_ingest;

-- Grants first: once ownership moves, the migrator can no longer grant.
GRANT CREATE ON SCHEMA cloudops TO cloudops_resolver;
ALTER FUNCTION cloudops.resolve_tenant_alias(text, text) OWNER TO cloudops_resolver;
REVOKE CREATE ON SCHEMA cloudops FROM cloudops_resolver;
"""


def upgrade() -> None:
    op.execute(DDL)
    values = ", ".join(f"('{s}')" for s in LEGAL_SUFFIXES)
    op.execute(f"INSERT INTO staging.legal_suffix (suffix) VALUES {values}")


def downgrade() -> None:
    raise NotImplementedError("expand-only; roll forward, or restore with PITR")
