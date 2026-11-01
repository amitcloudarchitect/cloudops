"""Cloud onboarding endpoints (ADR 0027, under ADR 0003): a customer's Azure
subscriptions from an app registration, or an AWS account from an access key,
read-only, until production.

A platform administrator (``access.manage``, staff only) records the identifiers:
the directory and client id and the subscriptions, or the account and the access
key id. **Never the secret.** The response names the Key Vault secret to create
(``customer-<code>-azure-secret``; for AWS the key id and the secret key), which
the administrator does out of band. Recording queues a read-only check; the
connector worker, which alone can read the secret, claims it within a minute,
checks the credential and its roles, binds the accounts to the tenant and starts
the first sync. The console polls the check (ADR 0015's pattern: this API has no
secret and no Temporal client).

The tenant is created or chosen with the customer-mapping endpoints (``/v1/mapping/
tenants``), which a platform administrator may use too.

Every write is a database function (migration 0025) that checks the permission,
staff, and the tenant's assignment and scope again; refusals map to structured
codes, with no tenant, customer or secret name in them. Expiry is at most 90 days
(ADR 0003, guardrail 3), enforced by the function and the table.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_api.access import Access
from cloudops_api.bff import TenantView
from cloudops_api.pipeline import (
    ApiError,
    AuditedRoute,
    audit_only,
    current_access,
    platform,
    requires,
)
from cloudops_api.provenance import ApiModel, Metric, query_ref
from cloudops_tenancy import ScopedConnection

router = APIRouter(prefix="/v1/cloud-onboarding", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

PERMISSION: Final = "access.manage"
MAX_LIST: Final = 500

Provider = Literal["azure", "aws"]
CheckStatus = Literal["requested", "running", "passed", "refused", "failed"]
CredentialStatus = Literal["pending", "active", "refused", "revoked"]
SubscriptionId = Annotated[
    str, Field(pattern=r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")]
AwsAccountId = Annotated[str, Field(pattern=r"^[0-9]{12}$")]

# The functions' refusals (migration 0025), by SQLSTATE.
_REFUSALS: Final = {
    "42501": (403, "forbidden"),
    "CMSCP": (404, "not_found"),
    "P0002": (404, "not_found"),
    "CMDUP": (409, "conflict"),
    "23514": (422, "invalid_request"),
}


@asynccontextmanager
async def _guarded(request: Request, access: Access) -> AsyncIterator[ScopedConnection]:
    try:
        async with platform(request).db.transaction(access.scope) as conn:
            yield conn
    except DBAPIError as exc:
        refusal = _REFUSALS.get(str(getattr(exc.orig, "sqlstate", "")))
        if refusal is None:
            raise
        raise ApiError(*refusal) from None


def _staff(access: Access) -> None:
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


# --- views -------------------------------------------------------------------------


class AzureCredentialRequest(ApiModel):
    provider: Literal["azure"]
    tenant_id: UUID
    directory_id: UUID  # the Entra tenant the app registration lives in
    client_id: UUID  # the app registration's application (client) id
    subscription_ids: list[SubscriptionId] = Field(min_length=1, max_length=50)
    secret_expires_at: datetime  # as set on the client secret; at most 90 days away


class AwsCredentialRequest(ApiModel):
    provider: Literal["aws"]
    tenant_id: UUID
    account_id: AwsAccountId
    access_key_id: str = Field(pattern=r"^AKIA[A-Z0-9]{16}$")
    secret_expires_at: datetime  # when this key will be rotated; at most 90 days away


class CheckRequest(ApiModel):
    # Set when a new secret was put in Key Vault under the same name (a rotation).
    secret_expires_at: datetime | None = None

    @model_validator(mode="after")
    def _aware(self) -> CheckRequest:
        if self.secret_expires_at is not None and self.secret_expires_at.tzinfo is None:
            raise ValueError("secret_expires_at needs a time zone")
        return self


class CheckView(ApiModel):
    id: UUID
    reason: Literal["onboard", "rotation", "recheck"]
    status: CheckStatus
    requested_at: datetime
    finished_at: datetime | None
    outcome: str | None  # our code: read_only, role_not_allowed, secret_not_in_key_vault...
    # What the check found: role names, account ids it was onboarded for, counts.
    # Never a secret, a token, or another tenant's account id.
    detail: dict[str, Any] | None


class CredentialView(ApiModel):
    id: UUID
    tenant: TenantView
    provider: Provider
    credential_source: Literal["stored_key"]
    directory_id: UUID | None
    client_id: UUID | None
    access_key_id: str | None
    account_ids: list[str]
    # Create these in Key Vault, out of band. Names, never values.
    secret_names: list[str]
    owned_by_me: bool
    created_at: datetime
    secret_set_at: datetime
    expires_at: datetime
    days_to_expiry: Metric[int]
    status: CredentialStatus
    validated_at: datetime | None
    revoked_at: datetime | None
    # Accounts also held by the internal tenant: the platform's own subscription,
    # whose cost appears under both tenants (ADR 0023, amended by ADR 0027).
    shared_with_platform: list[str]
    latest_check: CheckView | None


_CREDENTIALS: Final = """
SELECT k.id, k.tenant_id, t.canonical_name, k.provider, k.directory_id, k.client_id,
       k.access_key_id, k.account_ids, k.secret_names, k.owner_oid, k.created_at,
       k.secret_set_at, k.expires_at, k.status, k.validated_at, k.revoked_at,
       ARRAY(SELECT a.native_id FROM cloudops.cloud_account a
             WHERE a.tenant_id = k.tenant_id AND a.provider = k.provider
               AND a.native_id = ANY(k.account_ids) AND a.shared_with_platform
             ORDER BY a.native_id),
       c.id, c.reason, c.status, c.requested_at, c.finished_at, c.outcome, c.detail
FROM cloudops.customer_credential k
JOIN cloudops.tenant t ON t.id = k.tenant_id
LEFT JOIN LATERAL (
    SELECT * FROM cloudops.credential_check c WHERE c.credential_id = k.id
    ORDER BY c.requested_at DESC LIMIT 1) c ON true
"""


def _view(r: tuple[Any, ...], access: Access) -> CredentialView:
    now = datetime.now(UTC)
    expires: datetime = r[12]
    check = None if r[17] is None else CheckView(
        id=r[17], reason=r[18], status=r[19], requested_at=r[20], finished_at=r[21],
        outcome=r[22], detail=r[23])
    return CredentialView(
        id=r[0], tenant=TenantView(id=r[1], name=r[2]), provider=r[3],
        credential_source="stored_key", directory_id=r[4], client_id=r[5],
        access_key_id=r[6], account_ids=list(r[7]), secret_names=list(r[8]),
        owned_by_me=r[9] == access.identity.oid, created_at=r[10],
        secret_set_at=r[11], expires_at=expires,
        days_to_expiry=Metric[int](
            value=max((expires - now).days, 0), unit="days", source_system="platform",
            as_of=now, query_ref=query_ref("cloud_onboarding.expiry", 1, credential=str(r[0])),
            is_modelled=False),
        status=r[13], validated_at=r[14], revoked_at=r[15], shared_with_platform=list(r[16]),
        latest_check=check)


async def _one(conn: ScopedConnection, credential: UUID, access: Access) -> CredentialView:
    found = (await conn.execute(text(f"{_CREDENTIALS} WHERE k.id = :id"),
                                {"id": credential})).one_or_none()
    if found is None:
        raise ApiError(404, "not_found")
    return _view(tuple(found), access)


# --- endpoints ---------------------------------------------------------------------


@router.get("/credentials", response_model=list[CredentialView])
@requires(PERMISSION)
async def list_credentials(request: Request, access: AccessDep) -> list[CredentialView]:
    """The inventory (ADR 0003, guardrail 6): every stored credential in scope,
    revoked ones included, soonest expiry first."""
    _staff(access)
    async with _guarded(request, access) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            f"{_CREDENTIALS} ORDER BY k.revoked_at IS NOT NULL, k.expires_at LIMIT :n"),
            {"n": MAX_LIST})]
    return [_view(r, access) for r in rows]


@router.post("/credentials", response_model=CredentialView, status_code=201)
@requires(PERMISSION)
async def onboard(
    request: Request,
    body: Annotated[AzureCredentialRequest | AwsCredentialRequest, Field(discriminator="provider")],
    access: AccessDep,
) -> CredentialView:
    """Record a credential's identifiers and queue its check. The secret goes into
    Key Vault under the returned ``secret_names``, never here."""
    _staff(access)
    if body.secret_expires_at.tzinfo is None:
        raise ApiError(422, "invalid_request")
    if body.tenant_id not in access.scope.tenant_ids:
        raise ApiError(404, "not_found")
    params: dict[str, object]
    if isinstance(body, AzureCredentialRequest):
        params = {"dir": body.directory_id, "client": body.client_id, "key": None,
                  "accounts": [s.lower() for s in body.subscription_ids]}
    else:
        params = {"dir": None, "client": None, "key": body.access_key_id,
                  "accounts": [body.account_id]}
    async with _guarded(request, access) as conn:
        created = await conn.scalar(text(
            "SELECT access.onboard_cloud_credential(:actor, :tenant, :provider, :dir, :client,"
            " :key, CAST(:accounts AS text[]), :expires)"),
            {"actor": access.identity.oid, "tenant": body.tenant_id, "provider": body.provider,
             "expires": body.secret_expires_at, **params})
        view = await _one(conn, created, access)
    audit_only(request, [body.tenant_id])
    return view


@router.post("/credentials/{credential_id}/checks", response_model=CredentialView,
             status_code=201)
@requires(PERMISSION)
async def request_check(
    request: Request, credential_id: UUID, body: CheckRequest, access: AccessDep,
) -> CredentialView:
    """Check again: after creating the secret, after fixing a role, or with a new
    expiry after rotating the secret under the same name."""
    _staff(access)
    async with _guarded(request, access) as conn:
        await conn.execute(text("SELECT access.request_credential_check(:a, :c, :e)"),
                           {"a": access.identity.oid, "c": credential_id,
                            "e": body.secret_expires_at})
        view = await _one(conn, credential_id, access)
    audit_only(request, [view.tenant.id])
    return view


@router.post("/credentials/{credential_id}/revoke", response_model=CredentialView)
@requires(PERMISSION)
async def revoke(request: Request, credential_id: UUID, access: AccessDep) -> CredentialView:
    """Stop using it now. Delete the Key Vault secret and the app registration's
    secret (or deactivate the access key) out of band; the row stays as the record."""
    _staff(access)
    async with _guarded(request, access) as conn:
        await conn.execute(text("SELECT access.revoke_cloud_credential(:a, :c)"),
                           {"a": access.identity.oid, "c": credential_id})
        view = await _one(conn, credential_id, access)
    audit_only(request, [view.tenant.id])
    return view
