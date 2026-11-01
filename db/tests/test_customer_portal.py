"""Step 18: the customer portal (HLD M2 and M22, ADR 0018, migration 0011).

From the customer principal's side, the three cases the brief names:

- a tampered claim, header or parameter changes nothing;
- a commercial, effort or internal field is refused by the database, not
  hidden by the API;
- another tenant's resource, ticket or user by id is 404, never 403.

Then the onboarding gate (a customer resolves only while its tenant is live and
its role is agreed), the tenant administrator's reach, and the default roles'
landing views.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import AGREED_ROLES, Api, Principals, Seeded
from .support import scope


async def _rows(db: Database, s: TenantScope, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with db.transaction(s) as conn:
        return [tuple(r) for r in await conn.execute(text(sql), params)]


async def _call(db: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    return await _rows(db, TenantScope.for_resolution("test:portal"), sql, **params)


async def _post(api: Api, path: str, oid: UUID, body: Any = None, **mint: Any) -> Any:
    token = api.token(oid, **mint)
    return await api.client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture(scope="module")
def changes(seeded: Seeded) -> dict[UUID, str]:
    """One open change per tenant, for the approver's view."""
    numbers = {t.id: f"CTASK-{t.name}-{uuid4().hex[:6]}" for t in seeded.tenants}
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for t in seeded.tenants:
            cx.execute(
                # No resource: other suites count each tenant's resource's tickets.
                "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
                " assigned_to, assignment_workgroup, planned_hours, actual_hours)"
                " VALUES (%s, 'change_task', %s, %s, %s, 'A. Getronics', 'GTN Azure L2',"
                " 4.00, 6.50)",
                (t.id, numbers[t.id], t.name.upper(), datetime.now(UTC) - timedelta(hours=3)),
            )
    return numbers


@dataclass(frozen=True)
class RoleUsers:
    """One user of tenant a per default customer role, added by a's administrator."""

    by_role: dict[str, UUID]


@pytest.fixture(scope="module")
async def role_users(api: Api, principals: Principals) -> RoleUsers:
    users: dict[str, UUID] = {}
    for role in AGREED_ROLES:
        oid = uuid4()
        response = await _post(api, "/v1/tenant/users", principals.tenant_admin_a,
                               {"oid": str(oid), "display_name": f"{role} user", "role": role})
        assert response.status_code == 200, response.text
        users[role] = oid
    return RoleUsers(users)


# --- 1. A tampered claim, header or parameter changes nothing ------------------------


TAMPERED_CLAIMS: dict[str, Any] = {
    "tenant_id": "", "tid_override": "", "organisation": "bravo",
    "roles": ["platform_admin", "cfo"], "is_tenant_admin": True, "kind": "staff",
}


@pytest.mark.parametrize("path", ["/v1/me", "/v1/performance", "/v1/approvals",
                                  "/v1/work-items?status=all", "/v1/customers"])
async def test_tampered_claims_change_nothing(
    api: Api, principals: Principals, seeded: Seeded, role_users: RoleUsers,
    changes: dict[UUID, str], path: str,
) -> None:
    for oid in (principals.customer_a, *role_users.by_role.values()):
        honest = await api.get(path, oid)
        claims = {**TAMPERED_CLAIMS, "tenant_id": str(seeded.b.id),
                  "tid_override": str(seeded.b.id)}
        joiner = "&" if "?" in path else "?"
        tampered = await api.get(f"{path}{joiner}tenant_id={seeded.b.id}", oid, claims=claims,
                                 headers={"X-Tenant-Id": str(seeded.b.id)})
        assert tampered.status_code == honest.status_code
        if honest.status_code == 200:
            assert _strip_volatile(tampered.json()) == _strip_volatile(honest.json())
            assert str(seeded.b.id) not in tampered.text


def _strip_volatile(body: Any) -> Any:
    """as_of is the response time; everything else must be identical."""
    if isinstance(body, dict):
        return {k: _strip_volatile(v) for k, v in body.items() if k != "as_of"}
    if isinstance(body, list):
        return [_strip_volatile(v) for v in body]
    return body


async def test_a_tampered_admin_claim_does_not_make_an_administrator(
    api: Api, principals: Principals, role_users: RoleUsers,
) -> None:
    for oid in (principals.customer_a, role_users.by_role["CXO"]):
        me = (await api.get("/v1/me", oid, claims=TAMPERED_CLAIMS)).json()
        assert me["tenant_admin"] is False
        assert me["kind"] == "customer"
        refused = await api.get("/v1/tenant/users", oid, claims=TAMPERED_CLAIMS)
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "forbidden"


# --- 2. A commercial, effort or internal field is refused by the database -----------


@pytest.mark.parametrize("sql", [
    "SELECT monthly_manage_revenue FROM cloudops.sow",
    "SELECT planned_hours FROM cloudops.work_item",
    "SELECT actual_hours FROM cloudops.work_item",
    "SELECT assigned_to FROM cloudops.work_item",
    "SELECT assignment_workgroup FROM cloudops.work_item",
    "SELECT company_raw FROM cloudops.work_item",
    "SELECT * FROM cloudops.work_item",
    "SELECT raw_extra FROM cloudops.resource",
    "SELECT delivery_location FROM cloudops.tenant",
    "SELECT contract_end FROM cloudops.tenant",
    "SELECT customer_raw FROM cloudops.cost_finding",
    "SELECT classification FROM cloudops.triage_result",
    "SELECT reason FROM cloudops.triage_review",
    "SELECT output FROM cloudops.agent_run",
    "SELECT prompt_redacted FROM cloudops.llm_call",
    "SELECT prompt_hash FROM cloudops.llm_cache",  # 0023: only what a cache lookup reads
    "SELECT status FROM cloudops.portal_onboarding",
    "SELECT role_id FROM cloudops.portal_role_agreement",
    "SELECT alias FROM cloudops.alias_proposal",
    "SELECT body FROM cloudops.knowledge_chunk",
    "SELECT actor FROM audit.action_log",
    "SELECT name FROM staging.resource_quarantine",
    "SELECT * FROM access.principal",
])
async def test_customer_scope_is_refused_what_it_may_not_see(
    app_db: Database, seeded: Seeded, sql: str,
) -> None:
    """Refused, not filtered: naming the field is an error, so no serialiser,
    view or forgotten WHERE clause can let it through."""
    with pytest.raises(Exception, match="permission denied"):
        await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER), sql)


@pytest.mark.parametrize(("table", "column"), [
    ("cloudops.triage_result", "classification"),
    ("cloudops.agent_run", "output"),
    ("cloudops.llm_call", "prompt_redacted"),
    ("cloudops.portal_onboarding", "status"),
])
async def test_staff_only_policy_holds_if_a_grant_is_ever_made(
    app_db: Database, seeded: Seeded, table: str, column: str,
) -> None:
    """The second lock: with a grant made by mistake, the restrictive policy
    still returns nothing to a customer scope, and still serves staff."""
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(f"GRANT SELECT ON {table} TO cloudops_customer")
    try:
        customer = await _rows(app_db, scope(seeded.a, role=Role.CUSTOMER),
                               f"SELECT {column} FROM {table}")
    finally:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute(f"REVOKE SELECT ON {table} FROM cloudops_customer")
    assert customer == []


async def test_customer_reads_what_it_is_granted(
    app_db: Database, seeded: Seeded, changes: dict[UUID, str],
) -> None:
    rows = await _rows(
        app_db, scope(seeded.a, role=Role.CUSTOMER),
        "SELECT w.number, w.state, w.close_notes, r.name, t.canonical_name"
        " FROM cloudops.work_item w LEFT JOIN cloudops.resource r ON r.id = w.resource_id"
        " JOIN cloudops.tenant t ON t.id = w.tenant_id ORDER BY 1",
    )
    assert {f"INC-{seeded.a.name}", changes[seeded.a.id]} <= {r[0] for r in rows}
    assert {r[4] for r in rows} == {seeded.a.name}


async def test_api_never_sends_a_customer_effort_or_workgroup(
    api: Api, principals: Principals, seeded: Seeded, changes: dict[UUID, str],
) -> None:
    number = changes[seeded.a.id]
    queue = (await api.get("/v1/work-items?status=all", principals.customer_a)).json()
    assert queue["workgroups"] == []
    assert all(i["workgroup"] is None for i in queue["items"])
    detail = await api.get(f"/v1/work-items/{number}", principals.customer_a)
    assert detail.status_code == 200
    assert detail.json()["assigned_to"] is None
    assert "GTN Azure L2" not in detail.text
    assert "A. Getronics" not in detail.text
    staff = (await api.get(f"/v1/work-items/{number}", principals.engineer_a)).json()
    assert staff["assigned_to"] == "A. Getronics"
    # A workgroup filter from a customer cannot probe the hidden column.
    probe = await api.get("/v1/work-items", principals.customer_a,
                          params={"workgroup": "GTN Azure L2", "status": "all"})
    assert probe.status_code == 200


async def test_commercial_endpoint_refuses_every_customer_role(
    api: Api, principals: Principals, role_users: RoleUsers,
) -> None:
    for oid in (principals.customer_a, principals.tenant_admin_a, *role_users.by_role.values()):
        response = await api.get("/v1/commercial/sows", oid)
        assert response.status_code == 403
        assert "SOW" not in response.text


async def test_no_customer_role_can_be_granted_access_management(
    app_db: Database, principals: Principals,
) -> None:
    with pytest.raises(Exception, match=r"access\.manage cannot be granted to a customer role"):
        await _call(app_db, "SELECT access.grant_permission(:a, 'CXO', 'access.manage')",
                    a=principals.admin)
    with pytest.raises(Exception, match="commercial permission"):
        await _call(app_db, "SELECT access.grant_permission(:a, 'CXO', 'commercial.read')",
                    a=principals.admin)


# --- 3. Another tenant's object by id: 404, never 403 ------------------------------


async def test_another_tenants_objects_are_not_found(
    api: Api, principals: Principals, seeded: Seeded, role_users: RoleUsers,
    changes: dict[UUID, str],
) -> None:
    b = seeded.b
    paths = (f"/v1/resources/{b.resource_id}", f"/v1/resources/{b.resource_id}/context",
             f"/v1/work-items/{changes[b.id]}", f"/v1/work-items/INC-{b.name}")
    for oid in (principals.customer_a, role_users.by_role["CXO"],
                role_users.by_role["Customer engineer"]):
        for path in paths:
            foreign = await api.get(path, oid)
            missing = await api.get(path.replace(b.name, "nobody").replace(
                str(b.resource_id), str(uuid4())), oid)
            assert foreign.status_code == missing.status_code == 404, path
            assert foreign.json()["error"]["code"] == "not_found"


async def test_another_tenants_user_is_not_found_for_an_administrator(
    api: Api, principals: Principals,
) -> None:
    """A tenant administrator cannot reach tenant b's administrator, a staff
    member or the platform admin. Each reads as a user that does not exist."""
    for target in (principals.tenant_admin_b, principals.engineer_a, principals.admin,
                   principals.customer_c_limited, uuid4()):
        disabled = await _post(api, f"/v1/tenant/users/{target}/disable",
                               principals.tenant_admin_a)
        assert disabled.status_code == 404, target
        assert disabled.json()["error"]["code"] == "not_found"
    for target in (principals.tenant_admin_b, principals.engineer_a, principals.admin):
        taken = await _post(api, "/v1/tenant/users", principals.tenant_admin_a,
                            {"oid": str(target), "display_name": "Mine now", "role": "CXO"})
        assert taken.status_code == 404, target
    me_b = (await api.get("/v1/me", principals.tenant_admin_b)).json()
    assert me_b["tenant_admin"] is True
    assert me_b["role"] == "CXO"
    staff = (await api.get("/v1/me", principals.engineer_a)).json()
    assert staff["kind"] == "staff"


async def test_administrator_lists_only_its_own_tenant(
    api: Api, principals: Principals, role_users: RoleUsers,
) -> None:
    users = (await api.get("/v1/tenant/users", principals.tenant_admin_a)).json()
    oids = {u["oid"] for u in users}
    assert {str(o) for o in role_users.by_role.values()} <= oids
    assert str(principals.tenant_admin_a) in oids
    assert str(principals.customer_a) in oids
    for outsider in (principals.tenant_admin_b, principals.tenant_admin_c,
                     principals.customer_c_limited, principals.engineer_a, principals.admin):
        assert str(outsider) not in oids
    roles = (await api.get("/v1/tenant/roles", principals.tenant_admin_a)).json()
    assert set(AGREED_ROLES) <= {r["name"] for r in roles}


# --- Onboarding (M2) and the live gate ----------------------------------------------


@pytest.fixture
def new_tenant(seeded: Seeded) -> UUID:
    tenant = uuid4()
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO cloudops.tenant (id, canonical_name, short_code) VALUES (%s, %s, %s)",
            (tenant, f"delta {tenant.hex[:6]}", f"D{tenant.hex[:6].upper()}"),
        )
    return tenant


async def test_onboarding_runs_in_order_and_gates_customers(
    api: Api, principals: Principals, app_db: Database, new_tenant: UUID,
) -> None:
    admin, t = principals.admin, new_tenant
    base = f"/v1/onboarding/{t}"
    # Assigned by a platform admin before onboarding: does not resolve.
    viewer = uuid4()
    await _call(app_db, "SELECT access.set_principal(:a, :o, 'customer', 'v', 'CXO', false)",
                a=admin, o=viewer)
    await _call(app_db, "SELECT access.grant_tenant(:a, :o, :t)", a=admin, o=viewer, t=t)
    assert (await api.get("/v1/me", viewer)).json()["error"]["code"] == "no_scope"

    out_of_order = await _post(api, f"{base}/roles", admin,
                               {"roles": ["CXO"], "agreement_ref": "SOW-7 schedule 4"})
    assert out_of_order.status_code == 409
    assert out_of_order.json()["error"]["code"] == "step_out_of_order"
    too_soon = await _post(api, f"{base}/live", admin, {"live": True})
    assert too_soon.json()["error"]["code"] == "step_out_of_order"

    bad_domain = await _post(api, f"{base}/federation", admin, {
        "idp_kind": "entra", "idp_reference": str(uuid4()), "domains": ["not a domain"]})
    assert bad_domain.status_code == 422
    federated = await _post(api, f"{base}/federation", admin, {
        "idp_kind": "entra", "idp_reference": str(uuid4()), "domains": ["Delta.Example.com"]})
    assert federated.status_code == 200, federated.text
    assert federated.json()["domains"] == ["delta.example.com"]

    unknown = await _post(api, f"{base}/roles", admin,
                          {"roles": ["CXO", "CFO"], "agreement_ref": "SOW-7 schedule 4"})
    assert unknown.status_code == 422  # CFO is a staff role, never agreeable
    agreed = await _post(api, f"{base}/roles", admin,
                         {"roles": ["CXO", "Security officer"],
                          "agreement_ref": "SOW-7 schedule 4"})
    assert agreed.json()["agreed_roles"] == ["CXO", "Security officer"]

    first = uuid4()
    not_agreed = await _post(api, f"{base}/first-admin", admin,
                             {"oid": str(first), "display_name": "Delta IT", "role": "Approver"})
    assert not_agreed.status_code == 422
    assigned = await _post(api, f"{base}/first-admin", admin,
                           {"oid": str(first), "display_name": "Delta IT", "role": "CXO"})
    assert assigned.status_code == 200, assigned.text
    again = await _post(api, f"{base}/first-admin", admin,
                        {"oid": str(uuid4()), "display_name": "Second", "role": "CXO"})
    assert again.status_code == 409
    assert (await api.get("/v1/me", first)).status_code == 403  # not live yet

    live = await _post(api, f"{base}/live", admin, {"live": True})
    assert live.json()["status"] == "live"
    me = (await api.get("/v1/me", first)).json()
    assert me["tenant_admin"] is True
    assert me["landing"] == "/customers"
    assert [x["id"] for x in me["tenants"]] == [str(t)]
    assert (await api.get("/v1/me", viewer)).status_code == 200

    # A role outside the agreed set does not resolve, whoever assigned it.
    await _call(app_db, "SELECT access.set_principal(:a, :o, 'customer', 'v', 'Approver', false)",
                a=admin, o=viewer)
    assert (await api.get("/v1/me", viewer)).json()["error"]["code"] == "no_scope"

    suspended = await _post(api, f"{base}/live", admin, {"live": False})
    assert suspended.json()["status"] == "suspended"
    assert (await api.get("/v1/me", first)).json()["error"]["code"] == "no_scope"
    await _post(api, f"{base}/live", admin, {"live": True})
    assert (await api.get("/v1/me", first)).status_code == 200

    listed = (await api.get("/v1/onboarding", admin)).json()
    assert next(o for o in listed if o["tenant"]["id"] == str(t))["status"] == "live"


async def test_onboarding_is_platform_administrators_only(
    api: Api, principals: Principals, new_tenant: UUID,
) -> None:
    for oid in (principals.engineer_a, principals.finops_all, principals.customer_a,
                principals.tenant_admin_a):
        listed = await api.get("/v1/onboarding", oid)
        assert listed.status_code == 403
        step = await _post(api, f"/v1/onboarding/{new_tenant}/federation", oid, {
            "idp_kind": "entra", "idp_reference": "x", "domains": ["x.example.com"]})
        assert step.status_code == 403


async def test_onboarding_an_unknown_tenant_is_not_found(
    api: Api, principals: Principals,
) -> None:
    response = await _post(api, f"/v1/onboarding/{uuid4()}/federation", principals.admin, {
        "idp_kind": "saml", "idp_reference": "https://idp.example.com", "domains": ["e.com"]})
    assert response.status_code == 404


async def test_onboarding_changes_are_logged(seeded: Seeded, principals: Principals) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        actions = {r[0] for r in cx.execute(
            "SELECT action FROM access.change_log WHERE actor = %s", (str(principals.admin),))}
    assert {"portal_record_federation", "portal_agree_roles", "portal_assign_first_admin",
            "portal_go_live"} <= actions


# --- The tenant administrator ------------------------------------------------------


async def test_administrator_manages_users_within_the_agreed_set(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    admin_a = principals.tenant_admin_a
    oid = uuid4()
    refused = await _post(api, "/v1/tenant/users", admin_a,
                          {"oid": str(oid), "display_name": "New", "role": "Engineer"})
    assert refused.status_code == 422  # a staff role
    added = await _post(api, "/v1/tenant/users", admin_a,
                        {"oid": str(oid), "display_name": "New", "role": "Approver"})
    assert added.status_code == 200
    me = (await api.get("/v1/me", oid)).json()
    assert me["role"] == "Approver"
    assert me["landing"] == "/approvals"
    assert [t["id"] for t in me["tenants"]] == [str(seeded.a.id)]

    disabled = await _post(api, f"/v1/tenant/users/{oid}/disable", admin_a)
    assert disabled.status_code == 200
    assert (await api.get("/v1/me", oid)).status_code == 403
    with psycopg.connect(seeded.admin_dsn) as cx:
        logged = cx.execute(
            "SELECT action FROM access.change_log WHERE actor = %s AND detail->>'oid' = %s"
            " ORDER BY id", (str(admin_a), str(oid))).fetchall()
    assert [r[0] for r in logged] == ["tenant_set_user", "tenant_disable_user"]


async def test_the_last_administrator_stays(api: Api, principals: Principals) -> None:
    admin_c = principals.tenant_admin_c
    gone = await _post(api, f"/v1/tenant/users/{admin_c}/disable", admin_c)
    assert gone.status_code == 409
    assert gone.json()["error"]["code"] == "last_administrator"
    demoted = await _post(api, "/v1/tenant/users", admin_c,
                          {"oid": str(admin_c), "display_name": "First admin", "role": "CXO",
                           "tenant_admin": False})
    assert demoted.json()["error"]["code"] == "last_administrator"
    assert (await api.get("/v1/me", admin_c)).json()["tenant_admin"] is True


async def test_a_subscription_limited_customer_cannot_administer(
    api: Api, principals: Principals, app_db: Database, seeded: Seeded,
) -> None:
    """It could otherwise create users wider than itself (D16)."""
    limited = principals.customer_c_limited
    await _call(app_db,
                "SELECT access.set_principal(:a, :o, 'customer', 'limited', 'CXO', false)",
                a=principals.admin, o=limited)
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("UPDATE access.principal SET is_tenant_admin = true WHERE oid = %s",
                   (limited,))
    try:
        response = await api.get("/v1/tenant/users", limited)
        assert response.status_code == 403
        promoted = await _post(api, "/v1/tenant/users", principals.tenant_admin_c,
                               {"oid": str(limited), "display_name": "limited",
                                "role": "CXO", "tenant_admin": True})
        assert promoted.status_code == 422
    finally:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("UPDATE access.principal SET is_tenant_admin = false WHERE oid = %s",
                       (limited,))
        await _call(app_db,
                    "SELECT access.set_principal(:a, :o, 'customer', 'limited',"
                    " 'Customer viewer', false)", a=principals.admin, o=limited)


def test_only_a_customer_can_be_a_tenant_administrator(
    seeded: Seeded, principals: Principals,
) -> None:
    with (pytest.raises(psycopg.errors.CheckViolation),
          psycopg.connect(seeded.admin_dsn) as cx):
        cx.execute("UPDATE access.principal SET is_tenant_admin = true WHERE oid = %s",
                   (principals.engineer_a,))


# --- The default roles and their landing views ---------------------------------------


LANDINGS = {
    "CXO": ("/customers", "/v1/customers"),
    "VP or director": ("/performance", "/v1/performance"),
    "Architect": ("/resources", "/v1/resources"),
    "Customer engineer": ("/work", "/v1/work-items"),
    "Security officer": ("/posture", "/v1/posture"),
    "Approver": ("/approvals", "/v1/approvals"),
}
NEVER = ("/v1/commercial/sows", "/v1/mapping/tenants", "/v1/connectors/health",
         "/v1/onboarding")


@pytest.mark.parametrize("role", sorted(LANDINGS))
async def test_each_role_lands_on_its_view_and_nothing_staff_only(
    api: Api, role_users: RoleUsers, changes: dict[UUID, str], role: str,
) -> None:
    oid = role_users.by_role[role]
    landing, endpoint = LANDINGS[role]
    me = (await api.get("/v1/me", oid)).json()
    assert me["landing"] == landing
    assert me["kind"] == "customer"
    assert not {"commercial.read", "audit.read", "access.manage", "mapping.manage",
                "connectors.read", "triage.review"} & set(me["permissions"])
    assert (await api.get(endpoint, oid)).status_code == 200
    for path in NEVER:
        assert (await api.get(path, oid)).status_code == 403, path


async def test_views_hold_only_the_callers_tenant(
    api: Api, principals: Principals, seeded: Seeded, role_users: RoleUsers,
    changes: dict[UUID, str],
) -> None:
    approvals = (await api.get("/v1/approvals", role_users.by_role["Approver"])).json()
    assert changes[seeded.a.id] in {i["number"] for i in approvals["items"]}
    assert {i["tenant"]["id"] for i in approvals["items"]} == {str(seeded.a.id)}
    assert approvals["can_act"] is False
    staff = (await api.get("/v1/approvals", principals.admin)).json()
    assert {i["number"] for i in staff["items"]} >= set(changes.values())

    # The customer's figures are exactly a tenant-a-only engineer's, and fewer
    # than an all-tenant administrator's.
    posture = (await api.get("/v1/posture", role_users.by_role["Security officer"])).json()
    engineer = (await api.get("/v1/posture", principals.engineer_a)).json()
    assert _strip_volatile(posture) == _strip_volatile(engineer)
    assert f"vm-{seeded.a.name}-01" in {f["asset_name"] for f in posture["findings"]}
    assert not {f"vm-{t.name}-01" for t in (seeded.b, seeded.c)} & {
        f["asset_name"] for f in posture["findings"]}

    performance = (await api.get("/v1/performance",
                                 role_users.by_role["VP or director"])).json()
    assert len(performance["months"]) == 6
    engineer = (await api.get("/v1/performance", principals.engineer_a)).json()
    assert _strip_volatile(performance) == _strip_volatile(engineer)
    everyone = (await api.get("/v1/performance", principals.admin)).json()
    assert everyone["open_incidents"]["value"] > performance["open_incidents"]["value"]
