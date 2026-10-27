"""Every case in the LLD's "Isolation cases that must exist" table.

Each case has its database-level half tested here against the real schema. The
half that belongs to a component not yet built (API, assistant, broker) is a
strict-xfail stub naming that component; it turns red the moment the
component exists, until a real test replaces it.

Run as a release gate:  uv run pytest -m isolation
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role

from .conftest import Seeded, embedding
from .support import has_customer_role, has_python, pending, raw_scope, scope, stub

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

COMMERCIAL_ROLES = {
    "ceo",
    "cfo",
    "finops",
    "delivery_lead",
    "commercial",
    "auditor",
    "platform_admin",
}

api_built = has_python("services/api/**/*.py")
assistant_built = has_python("services/**/*assistant*/**/*.py", "services/**/*gateway*/**/*.py")
broker_built = has_python("execution/**/*.py")


async def _rows(db: Database, s: Any, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with db.transaction(s) as conn:
        return [tuple(r) for r in await conn.execute(text(sql), params)]


# --- 1. API request for another tenant's resource by ID -> 404, not 403 -----------


@pytest.mark.parametrize(
    ("table", "attr"),
    [("cloudops.resource", "resource_id"), ("cloudops.cloud_account", "account_id")],
)
async def test_case1_foreign_id_is_indistinguishable_from_missing(
    app_db: Database, seeded: Seeded, table: str, attr: str
) -> None:
    """The database returns exactly what it returns for an id that never
    existed, so the API has nothing to turn into a 403."""
    sql = f"SELECT id FROM {table} WHERE id = :id"
    foreign = await _rows(app_db, scope(seeded.a), sql, id=getattr(seeded.b, attr))
    missing = await _rows(app_db, scope(seeded.a), sql, id=uuid4())
    own = await _rows(app_db, scope(seeded.a), sql, id=getattr(seeded.a, attr))
    assert foreign == missing == []
    assert own == [(getattr(seeded.a, attr),)]


@pending("Phase 3 API: GET of another tenant's resource returns 404, not 403")
def test_case1_api_returns_404() -> None:
    stub(api_built, "the API (services/api)")


# --- 2. Direct SQL with scope unset -> zero rows, not all rows --------------------

TENANT_TABLES = (
    "cloudops.tenant",
    "cloudops.tenant_alias",
    "cloudops.sow",
    "cloudops.cloud_account",
    "cloudops.resource",
    "cloudops.work_item",
    "cloudops.vulnerability",
    "cloudops.cost_fact",
    "audit.action_log",
    "staging.resource_quarantine",
)


@pytest.mark.parametrize("login", ["app_dsn", "ingest_dsn"])
@pytest.mark.parametrize("table", TENANT_TABLES)
def test_case2_unscoped_sql_sees_zero_rows(seeded: Seeded, login: str, table: str) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        seeded_rows = cx.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert seeded_rows is not None
    assert seeded_rows[0] > 0, "zero proves nothing on an empty table"
    with psycopg.connect(getattr(seeded, login)) as cx:
        if table.startswith("staging.") and login == "app_dsn":
            # Quarantine is ingest-only: the application role cannot even look.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cx.execute(f"SELECT count(*) FROM {table}")
            return
        visible = cx.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert visible == (0,)


def test_case2_unscoped_knowledge_shows_global_chunks_only(seeded: Seeded) -> None:
    with psycopg.connect(seeded.app_dsn) as cx:
        tenants = cx.execute("SELECT tenant_id FROM cloudops.knowledge_chunk").fetchall()
    assert tenants == [(None,)]


async def test_case2_explicitly_empty_scope_sees_zero_rows(
    app_db: Database, ingest_db: Database, seeded: Seeded
) -> None:
    for table in TENANT_TABLES:
        db = ingest_db if table.startswith("staging.") else app_db
        assert await _rows(db, scope(role=Role.PLATFORM_ADMIN), f"SELECT 1 FROM {table}") == []


# --- 3. Vector retrieval naming another customer -> no foreign chunks ---------------

RETRIEVAL = """
SELECT tenant_id, 1 - (embedding <=> {q}) AS score
FROM cloudops.knowledge_chunk
WHERE (tenant_id = ANY(cloudops.current_tenant_ids()) OR tenant_id IS NULL)
  AND source_kind = ANY({kinds})
ORDER BY embedding <=> {q} LIMIT 20
"""


async def test_case3_retrieval_returns_no_foreign_chunks(app_db: Database, seeded: Seeded) -> None:
    """The query vector is tenant B's own chunk, so B's chunk is the nearest
    neighbour in the table. Scoped to A it must not come back."""
    sql = RETRIEVAL.format(q="CAST(:q AS vector)", kinds="CAST(:kinds AS text[])")
    rows = await _rows(
        app_db, scope(seeded.a), sql, q=embedding(seeded.b.axis), kinds=["close_note", "runbook"]
    )
    tenants = {r[0] for r in rows}
    assert tenants == {seeded.a.id, None}


@pytest.mark.parametrize("force_hnsw", [False, True])
def test_case3_filter_holds_under_the_hnsw_index_plan(seeded: Seeded, force_hnsw: bool) -> None:
    """Same query, once as the planner likes and once with every alternative to
    an ordered HNSW scan disabled: the tenant predicate is inside the SQL, so
    the plan cannot change the answer."""
    sql = RETRIEVAL.format(q="%(q)s::vector", kinds="%(kinds)s")
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "engineer") as cx:
        if force_hnsw:
            for setting in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
                cx.execute("SELECT set_config(%s, 'off', true)", (setting,))
        plan = "\n".join(
            r[0]
            for r in cx.execute(
                "EXPLAIN " + sql,
                {"q": embedding(seeded.b.axis), "kinds": ["close_note", "runbook"]},
            ).fetchall()
        )
        rows = cx.execute(
            sql, {"q": embedding(seeded.b.axis), "kinds": ["close_note", "runbook"]}
        ).fetchall()
    if force_hnsw:
        assert "knowledge_chunk_embedding_idx" in plan
    assert {r[0] for r in rows} == {seeded.a.id, None}


@pending("Phase 5 retrieval interface: gateway retrieval for tenant A never returns B's chunks")
def test_case3_gateway_retrieval() -> None:
    stub(assistant_built, "the AI gateway retrieval interface")


# --- 4. Single-tenant user asks to compare two customers -> refusal ---------------


async def test_case4_cross_tenant_comparison_returns_only_own_tenant(
    app_db: Database, seeded: Seeded
) -> None:
    sql = (
        "SELECT tenant_id, sum(unblended_cost) FROM cloudops.cost_fact"
        " WHERE tenant_id = ANY(CAST(:ids AS uuid[])) GROUP BY tenant_id"
    )
    rows = await _rows(app_db, scope(seeded.a), sql, ids=[seeded.a.id, seeded.b.id])
    assert [r[0] for r in rows] == [seeded.a.id]


@pending("Phase 5/6 assistant: a single-tenant user's comparison request is refused")
def test_case4_assistant_refuses() -> None:
    stub(assistant_built, "the assistant")


# --- 5. Credential minted for A used against B -> broker rejection ----------------


async def test_case5_targets_outside_scope_do_not_resolve(app_db: Database, seeded: Seeded) -> None:
    """The broker resolves targets against tenant scope (LLD, minting step 5).
    B's account and resource do not resolve under A's scope."""
    accounts = await _rows(
        app_db,
        scope(seeded.a),
        "SELECT id FROM cloudops.cloud_account WHERE native_id = :n",
        n=seeded.b.account_native_id,
    )
    resources = await _rows(
        app_db,
        scope(seeded.a),
        "SELECT id FROM cloudops.resource WHERE account_id = :a",
        a=seeded.b.account_id,
    )
    assert accounts == resources == []


@pending("execution plane broker (after security review): rejects B targets before any cloud call")
def test_case5_broker_rejects() -> None:
    stub(broker_built, "the credential broker (execution/)")


# --- 6. Customer principal requests margin or rate-card field -> absent by RLS ------


@pytest.mark.parametrize("role", ["customer", "customer_admin", "customer_user", "", "CFO"])
def test_case6_unknown_and_customer_roles_see_no_commercial_rows(seeded: Seeded, role: str) -> None:
    """Customer roles are not defined yet; whatever they are called, a role
    outside the commercial list gets no rows. Fails closed by construction."""
    with raw_scope(seeded.app_dsn, str(seeded.a.id), role) as cx:
        rows = cx.execute("SELECT monthly_manage_revenue FROM cloudops.sow").fetchall()
        tenant_rows = cx.execute("SELECT count(*) FROM cloudops.resource").fetchone()
    assert rows == []
    assert tenant_rows == (1,), "the role, not the scope, is what hides the row"


@pytest.mark.parametrize("role", list(Role))
async def test_case6_commercial_visibility_matches_the_role_list(
    app_db: Database, seeded: Seeded, role: Role
) -> None:
    rows = await _rows(app_db, scope(seeded.a, role=role), "SELECT id FROM cloudops.sow")
    assert len(rows) == (1 if role.value in COMMERCIAL_ROLES else 0)


@pending("Phase 3: customer roles defined, and a customer principal sees no margin fields")
def test_case6_customer_principal() -> None:
    stub(has_customer_role, "customer roles in cloudops_tenancy.Role")


# --- 7. Customer principal with a tampered organisation claim -> claim ignored ------


@pytest.mark.parametrize(
    "tampered",
    ["*", "all", "' OR '1'='1", "{}", "00000000-0000-0000-0000-000000000000;", "NULL"],
)
def test_case7_malformed_scope_fails_closed(seeded: Seeded, tampered: str) -> None:
    """Whatever reaches app.tenant_ids, it can only name tenants or fail. It
    never widens to all rows."""
    with (
        pytest.raises(psycopg.errors.InvalidTextRepresentation),
        raw_scope(seeded.app_dsn, f"{seeded.a.id},{tampered}", "engineer") as cx,
    ):
        cx.execute("SELECT count(*) FROM cloudops.resource")


@pending("Phase 3 scope resolution: tampered organisation claim ignored, scope from the server")
def test_case7_claim_ignored() -> None:
    stub(api_built, "the API's scope resolution (services/api)")


# --- 8. Engineer in tenant scope requests commercial data -> restrictive policy -----


async def test_case8_engineer_cannot_read_commercial_data_in_own_tenant(
    app_db: Database, seeded: Seeded
) -> None:
    engineer = await _rows(
        app_db, scope(seeded.a, role=Role.ENGINEER), "SELECT id FROM cloudops.sow"
    )
    cfo = await _rows(app_db, scope(seeded.a, role=Role.CFO), "SELECT id FROM cloudops.sow")
    assert engineer == []
    assert len(cfo) == 1


async def test_case8_engineer_cannot_write_commercial_data(
    app_db: Database, seeded: Seeded
) -> None:
    insert = (
        "INSERT INTO cloudops.sow (tenant_id, reference, effective_from, currency,"
        " monthly_manage_revenue) VALUES (:t, 'SOW-X', '2026-02-01', 'EUR', 1.000000)"
    )

    async def write() -> None:
        await _rows(
            app_db, scope(seeded.a, role=Role.ENGINEER), insert + " RETURNING id", t=seeded.a.id
        )

    with pytest.raises(Exception, match="row-level security"):
        await write()


# --- Writes into another tenant, for every tenant table -----------------------------

WRITES = {
    "cloudops.tenant_alias": (
        "INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias)"
        " VALUES (:t, 'opsramp', :u)"
    ),
    "cloudops.cloud_account": (
        "INSERT INTO cloudops.cloud_account (tenant_id, provider, native_id, display_name)"
        " VALUES (:t, 'aws', :u, 'x')"
    ),
    "cloudops.work_item": (
        "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at)"
        " VALUES (:t, 'incident', :u, 'x', now())"
    ),
    "cloudops.vulnerability": (
        "INSERT INTO cloudops.vulnerability (tenant_id, qid, asset_name, scope, title, severity,"
        " first_detected) VALUES (:t, 1, :u, 'os', 'x', 1, now())"
    ),
    "cloudops.knowledge_chunk": (
        "INSERT INTO cloudops.knowledge_chunk (tenant_id, source_kind, body) VALUES (:t, 'kb', :u)"
    ),
    "audit.action_log": (
        "INSERT INTO audit.action_log (tenant_id, actor, action) VALUES (:t, 'user:x', :u)"
    ),
    "staging.resource_quarantine": (
        "INSERT INTO staging.resource_quarantine (tenant_id, source_system, external_key)"
        " VALUES (:t, 'qualys', :u)"
    ),
}


@pytest.mark.parametrize("table", sorted(WRITES))
async def test_write_into_another_tenant_is_rejected(
    app_db: Database, ingest_db: Database, seeded: Seeded, table: str
) -> None:
    db = ingest_db if table.startswith("staging.") else app_db

    async def write() -> None:
        await _rows(
            db, scope(seeded.a), WRITES[table] + " RETURNING 1", t=seeded.b.id, u=str(uuid4())
        )

    with pytest.raises(Exception, match="row-level security"):
        await write()
    # And the same statement inside the scope succeeds, so the rejection is RLS.
    assert await _rows(
        db, scope(seeded.a), WRITES[table] + " RETURNING 1", t=seeded.a.id, u=str(uuid4())
    )


async def test_no_scope_can_write_global_knowledge(app_db: Database, seeded: Seeded) -> None:
    async def write() -> None:
        await _rows(
            app_db,
            scope(seeded.a, role=Role.PLATFORM_ADMIN),
            "INSERT INTO cloudops.knowledge_chunk (tenant_id, source_kind, body)"
            " VALUES (NULL, 'runbook', 'x') RETURNING 1",
        )

    with pytest.raises(Exception, match="row-level security"):
        await write()
