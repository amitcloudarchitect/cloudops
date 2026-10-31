"""Structural isolation checks, read from the catalog.

These catch the failure no behavioural test you did not write will catch: a
table added without FORCE, without a policy, or with a nullable tenant_id.
Every table in the platform schemas must be classified here; a new one that
is not fails until someone decides how it is protected.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from .conftest import Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

SCHEMAS = ("cloudops", "staging", "audit")

# Reference and ingest-state tables with no tenant. Protected by grants.
NON_TENANT = {
    "cloudops.fx_rate",
    "staging.ingest_watermark",
    "staging.tenant_alias_quarantine",  # unmatched names: no tenant by definition
    "cloudops.global_knowledge_chunk",  # Getronics-wide content, read-only (ADR 0001)
    # Connector state: a run spans tenants, so these carry none (ingest-only grants).
    "staging.legal_suffix",
    "staging.ingest_run",
    "staging.ingest_dead_letter",
    # Step 15: continuation state, CUR assembly progress, the field registry and
    # configuration. Field names and object keys, never a customer's values.
    "staging.ingest_cursor",
    "staging.cur_assembly",
    "staging.schema_field",
    "staging.metric_allowlist",
    "staging.connector_schedule",
    # Step 16: the platform's default anomaly thresholds, and the explicit entry
    # an FX correction creates (reference data, like fx_rate).
    "staging.anomaly_threshold_default",
    "cloudops.fx_restatement",
}
TENANT_KEYED_BY_ID = {"cloudops.tenant"}
COMMERCIAL = {"cloudops.sow", "cloudops.margin_snapshot", "cloudops.margin_variance"}
PARTITIONED = {
    "cloudops.cost_fact",
    "cloudops.work_item",
    "cloudops.vulnerability",
    "audit.action_log",
}
GROUP_ROLES = ("cloudops_app", "cloudops_ingest")

CATALOG = """
SELECT n.nspname || '.' || c.relname AS name,
       c.relkind,
       c.relrowsecurity,
       c.relforcerowsecurity,
       (SELECT NOT a.attnotnull FROM pg_attribute a
         WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped)
         AS tenant_nullable,
       (SELECT pg_get_expr(p.polqual, p.polrelid) || ' | '
               || pg_get_expr(p.polwithcheck, p.polrelid)
          FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = 'tenant_isolation')
         AS isolation_exprs,
       EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid
                 AND p.polname = 'commercial_visibility' AND NOT p.polpermissive)
         AS restrictive_commercial
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p') AND NOT c.relispartition
ORDER BY 1
"""


def _catalog(seeded: Seeded) -> dict[str, dict[str, Any]]:
    with psycopg.connect(seeded.admin_dsn) as cx:
        cur = cx.execute(CATALOG, (list(SCHEMAS),))
        cols = [d.name for d in cur.description or []]
        return {row[0]: dict(zip(cols, row, strict=True)) for row in cur.fetchall()}


def test_every_table_is_classified(seeded: Seeded) -> None:
    tables = set(_catalog(seeded))
    tenant_tables = tables - NON_TENANT
    assert tables >= NON_TENANT
    assert tenant_tables, "no tenant tables found"
    for name in tenant_tables:
        info = _catalog(seeded)[name]
        has_tenant = info["tenant_nullable"] is not None or name in TENANT_KEYED_BY_ID
        assert has_tenant, f"{name} has no tenant_id and is not classified as non-tenant"


@pytest.mark.parametrize(
    "required",
    [
        "cloudops.tenant",
        "cloudops.tenant_alias",
        "cloudops.sow",
        "cloudops.cloud_account",
        "cloudops.resource",
        "cloudops.work_item",
        "cloudops.cost_fact",
        "cloudops.vulnerability",
        "cloudops.knowledge_chunk",
        "cloudops.global_knowledge_chunk",
        "audit.action_log",
        "cloudops.fx_rate",
        "staging.ingest_watermark",
        "staging.tenant_alias_quarantine",
        "staging.resource_quarantine",
    ],
)
def test_phase1_entities_exist(seeded: Seeded, required: str) -> None:
    assert required in _catalog(seeded)


def test_tenant_tables_force_rls_with_the_isolation_policy(seeded: Seeded) -> None:
    problems = []
    for name, info in _catalog(seeded).items():
        if name in NON_TENANT:
            continue
        if not info["relrowsecurity"]:
            problems.append(f"{name}: RLS not enabled")
        if not info["relforcerowsecurity"]:
            problems.append(f"{name}: RLS not FORCEd (owner bypasses it)")
        exprs = info["isolation_exprs"]
        if exprs is None or exprs.count("current_tenant_ids()") != 2:
            problems.append(f"{name}: tenant_isolation must USE and CHECK current_tenant_ids()")
        if name not in TENANT_KEYED_BY_ID and info["tenant_nullable"]:
            problems.append(f"{name}: tenant_id is nullable")
    assert problems == []


def test_no_isolation_policy_has_a_null_tenant_escape(seeded: Seeded) -> None:
    """ADR 0001: no tenant table admits tenant-less rows, in either direction."""
    for name, info in _catalog(seeded).items():
        if name in NON_TENANT:
            continue
        assert "IS NULL" not in (info["isolation_exprs"] or ""), name


def test_non_tenant_tables_are_read_only_to_the_application(seeded: Seeded) -> None:
    """Reference data every scope may read must not be writable by any scope."""
    with psycopg.connect(seeded.admin_dsn) as cx:
        writable = cx.execute(
            "SELECT r, t FROM unnest(%s::text[]) r, unnest(%s::text[]) t"
            " WHERE has_table_privilege(r, t, 'INSERT,UPDATE,DELETE,TRUNCATE')",
            (list(GROUP_ROLES), ["cloudops.global_knowledge_chunk"]),
        ).fetchall()
        readable = cx.execute(
            "SELECT has_table_privilege('cloudops_app', 'cloudops.global_knowledge_chunk',"
            " 'SELECT')"
        ).fetchone()
    assert writable == []
    assert readable == (True,)


def test_commercial_tables_have_the_restrictive_policy(seeded: Seeded) -> None:
    catalog = _catalog(seeded)
    for name in COMMERCIAL:
        assert catalog[name]["restrictive_commercial"], name


def test_partitioned_tables_are_partitioned(seeded: Seeded) -> None:
    catalog = _catalog(seeded)
    assert {n for n, i in catalog.items() if i["relkind"] == "p"} == PARTITIONED


def test_partitions_cannot_be_reached_directly(seeded: Seeded) -> None:
    """A query against a partition skips the parent's policies, so the
    application roles must hold no privilege on any partition."""
    with psycopg.connect(seeded.admin_dsn) as cx:
        rows = cx.execute(
            """
            SELECT n.nspname || '.' || c.relname, r.rolname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN pg_roles r
            WHERE c.relispartition AND n.nspname = ANY(%s) AND r.rolname = ANY(%s)
              AND has_table_privilege(r.oid, c.oid, 'SELECT,INSERT,UPDATE,DELETE')
            """,
            (list(SCHEMAS), list(GROUP_ROLES)),
        ).fetchall()
        partitions = cx.execute(
            "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent"
            " WHERE p.relkind = 'p'"
        ).fetchone()
    assert rows == []
    assert partitions is not None
    assert partitions[0] >= len(PARTITIONED)


def test_application_role_cannot_read_unscoped_staging_tables(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        readable = cx.execute(
            "SELECT t FROM unnest(%s::text[]) t"
            " WHERE has_table_privilege('cloudops_app', t, 'SELECT')",
            (sorted(n for n in NON_TENANT if n.startswith("staging.")),),
        ).fetchall()
    assert readable == []


def test_no_platform_object_is_owned_by_an_application_role(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        owned = cx.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = ANY(%s) AND pg_get_userbyid(c.relowner) = ANY(%s)",
            (list(SCHEMAS), list(GROUP_ROLES)),
        ).fetchall()
    assert owned == []


def test_partman_manages_partitions_and_retention(seeded: Seeded) -> None:
    if not seeded.partman:
        pytest.skip("pg_partman not installed on this server; CI requires it")
    expected = {
        "cloudops.cost_fact": ("1 month", "7 years"),
        "cloudops.work_item": ("3 months", None),
        "cloudops.vulnerability": ("3 months", None),
        "audit.action_log": ("1 month", None),  # never dropped
    }
    with psycopg.connect(seeded.admin_dsn) as cx:
        actual = {
            row[0]: (row[1], row[2])
            for row in cx.execute(
                "SELECT parent_table, partition_interval::interval, retention::interval"
                " FROM partman.part_config"
            ).fetchall()
        }
        want = {
            table: cx.execute("SELECT %s::interval, %s::interval", (interval, retention)).fetchone()
            for table, (interval, retention) in expected.items()
        }
    assert actual == want
