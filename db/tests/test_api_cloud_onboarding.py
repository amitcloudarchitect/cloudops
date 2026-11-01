"""The cloud-onboarding API against the real schema (ADR 0027): only a platform
administrator reaches it, a customer never does; it records identifiers and
names the Key Vault secrets but never accepts a secret; the 90-day expiry and
one live credential per tenant hold through it; refusals are codes that name no
tenant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from .conftest import Api, Principals, Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


def _expiry(days: int = 60) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _azure(tenant: UUID, **kw: Any) -> dict[str, Any]:
    return {"provider": "azure", "tenant_id": str(tenant), "directory_id": str(uuid4()),
            "client_id": str(uuid4()), "subscription_ids": [str(uuid4())],
            "secret_expires_at": _expiry()} | kw


ENDPOINTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", "/v1/cloud-onboarding/credentials", None),
    ("POST", "/v1/cloud-onboarding/credentials", _azure(uuid4())),
    ("POST", f"/v1/cloud-onboarding/credentials/{uuid4()}/checks", {}),
    ("POST", f"/v1/cloud-onboarding/credentials/{uuid4()}/revoke", None),
)


async def _send(api: Api, method: str, path: str, oid: UUID, body: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {api.token(oid)}"}
    if method == "GET":
        return await api.client.get(path, headers=headers)
    return await api.client.post(path, json=body, headers=headers)


def _tenant(seeded: Seeded, code: str) -> UUID:
    t = uuid4()
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code)"
                   " VALUES (%s, %s, %s)", (t, f"API {code} {t}", code))
    return t


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
async def test_only_platform_administrators_reach_any_of_it(
    api: Api, principals: Principals, method: str, path: str, body: Any
) -> None:
    for oid in (principals.customer_a, principals.customer_c_limited, principals.engineer_a,
                principals.finops_all, principals.tickets_only):
        response = await _send(api, method, path, oid, body)
        assert response.status_code == 403, (oid, path)
        assert response.json()["error"]["code"] == "forbidden"


async def test_onboarding_names_the_secrets_and_queues_a_check(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    t = _tenant(seeded, "APIAZ")
    sub = str(uuid4()).upper()
    response = await _send(api, "POST", "/v1/cloud-onboarding/credentials", principals.admin,
                           _azure(t, subscription_ids=[sub]))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["secret_names"] == ["customer-apiaz-azure-secret"]
    assert body["account_ids"] == [sub.lower()]
    assert (body["status"], body["credential_source"], body["owned_by_me"]) == (
        "pending", "stored_key", True)
    assert body["latest_check"]["status"] == "requested"
    assert body["latest_check"]["reason"] == "onboard"
    assert 58 <= body["days_to_expiry"]["value"] <= 60
    listed = await _send(api, "GET", "/v1/cloud-onboarding/credentials", principals.admin)
    assert body["id"] in {c["id"] for c in listed.json()}


@pytest.mark.parametrize("field", ["client_secret", "secret", "secret_access_key"])
async def test_a_request_carrying_a_secret_is_refused(
    api: Api, principals: Principals, seeded: Seeded, field: str,
) -> None:
    response = await _send(api, "POST", "/v1/cloud-onboarding/credentials", principals.admin,
                           _azure(seeded.a.id, **{field: "do-not-send-me"}))
    assert response.status_code == 422
    assert "do-not-send-me" not in response.text


async def test_an_expiry_beyond_ninety_days_is_refused(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    response = await _send(api, "POST", "/v1/cloud-onboarding/credentials", principals.admin,
                           _azure(seeded.a.id, secret_expires_at=_expiry(120)))
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_request")


async def test_a_second_live_credential_for_a_tenant_is_a_conflict_naming_nobody(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    t = _tenant(seeded, "APITWO")
    first = await _send(api, "POST", "/v1/cloud-onboarding/credentials", principals.admin,
                        _azure(t))
    assert first.status_code == 201
    again = await _send(api, "POST", "/v1/cloud-onboarding/credentials", principals.admin,
                        _azure(t))
    assert (again.status_code, again.json()["error"]["code"]) == (409, "conflict")
    assert str(t) not in again.text
    assert "APITWO" not in again.text
    # A recheck while the first check waits is a conflict too; a revoke is not.
    cid = first.json()["id"]
    recheck = await _send(api, "POST", f"/v1/cloud-onboarding/credentials/{cid}/checks",
                          principals.admin, {})
    assert recheck.status_code == 409
    revoked = await _send(api, "POST", f"/v1/cloud-onboarding/credentials/{cid}/revoke",
                          principals.admin)
    assert (revoked.status_code, revoked.json()["status"]) == (200, "revoked")


async def test_aws_records_the_account_and_key_id(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    t = _tenant(seeded, "APIAWS")
    response = await _send(api, "POST", "/v1/cloud-onboarding/credentials", principals.admin, {
        "provider": "aws", "tenant_id": str(t), "account_id": "123456789012",
        "access_key_id": "AKIAI44QH8DHBEXAMPLE", "secret_expires_at": _expiry(30)})
    assert response.status_code == 201, response.text
    assert response.json()["secret_names"] == ["customer-apiaws-aws-access-key-id",
                                               "customer-apiaws-aws-secret-access-key"]


async def test_an_unknown_credential_reads_as_not_found(
    api: Api, principals: Principals,
) -> None:
    response = await _send(api, "POST", f"/v1/cloud-onboarding/credentials/{uuid4()}/revoke",
                           principals.admin)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")
