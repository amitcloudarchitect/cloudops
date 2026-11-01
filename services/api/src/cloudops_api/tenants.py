"""Editing and deleting a customer (ADR 0038): Settings, Customers.

- **Edit** (``mapping.manage``, the customer master's): the canonical name, and the
  short code while no live stored credential is named by it. Aliases and mappings are
  keyed by tenant id and do not change.
- **Delete is offboarding**, a platform administrator's: effective at once. The tenant
  leaves every scope (lists, the selector, dashboards, every query), its stored cloud
  credentials are revoked, its portal is suspended and its names resolve to nothing,
  so new records for them are quarantined. The exact name and a reason are required.
- **Restore** within the retention (30 days): it comes back with its credentials
  revoked and its portal suspended, to be onboarded again.
- **Purge** after the retention, with the name and the short code typed: a request the
  purge worker picks up (``cloudops_connectors.lifecycle``). The API starts nothing.
  The audit log and a tombstone are kept.

The database's functions (migration 0036) check the permission, the role class, the
assignment and the scope again, so this module is a thin door; their refusals map to
structured codes and nothing from the database reaches the client. An offboarded
tenant is in no scope, so restore, purge and the list reach it through the functions
alone, never through a query here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field
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
from cloudops_api.provenance import ApiModel
from cloudops_tenancy import Role, ScopedConnection

router = APIRouter(prefix="/v1/tenants", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

EDIT_PERMISSION: Final = "mapping.manage"
LIFECYCLE_PERMISSION: Final = "access.manage"

# The functions' refusals (migrations 0009 and 0036), by SQLSTATE. Anything else is a 500.
_REFUSALS: Final = {
    "42501": (403, "forbidden"),
    "CMSCP": (404, "not_found"),  # outside scope or unassigned reads as nonexistent
    "CMDUP": (409, "conflict"),
    "23514": (422, "invalid_request"),
    "TKCNF": (422, "confirmation_mismatch"),
    "TKCOD": (409, "short_code_in_use"),  # a live credential's secrets are named by it
    "TKHST": (409, "platform_host_rule"),
    "TKSTA": (409, "tenant_state"),  # not offboarded, or a purge already waits
    "TKRET": (409, "retention_not_ended"),
    "TKEXP": (409, "retention_ended"),
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


def _platform_admin(access: Access) -> None:
    """The database checks the role class again; this keeps a refusal cheap."""
    _staff(access)
    if access.scope.role is not Role.PLATFORM_ADMIN:
        raise ApiError(403, "forbidden")


# --- views -------------------------------------------------------------------------


class TenantEdit(ApiModel):
    canonical_name: str = Field(min_length=1, max_length=200)
    short_code: str = Field(pattern=r"^[A-Z0-9]{2,10}$")


class TenantEdited(ApiModel):
    tenant: TenantView
    short_code: str


class OffboardRequest(ApiModel):
    # Typed by the person, exactly as the tenant is named: never trimmed or folded.
    confirm_name: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class Offboarded(ApiModel):
    tenant: TenantView
    status: Literal["offboarded"]
    purge_after: datetime  # when a purge may be asked for; until then, restore


class Restored(ApiModel):
    tenant_id: UUID
    # Back in scope from the next request; credentials and the portal need onboarding.
    status: Literal["restored"]


class PurgeRequest(ApiModel):
    confirm_name: str = Field(min_length=1, max_length=200)
    confirm_code: str = Field(min_length=1, max_length=10)


PurgeStatus = Literal["requested", "running", "succeeded", "failed"]


class PurgeView(ApiModel):
    id: UUID
    status: PurgeStatus
    requested_at: datetime
    finished_at: datetime | None
    outcome: str | None  # why it failed: a code, never payload text


class OffboardedTenant(ApiModel):
    tenant: TenantView
    short_code: str
    internal: bool
    status: Literal["offboarded", "purging", "purged"]
    offboarded_at: datetime
    offboarded_by_me: bool
    reason: str
    purge_after: datetime
    purged_at: datetime | None
    # Within the retention and still offboarded.
    may_restore: bool
    # After the retention, offboarded or a failed purge, and nothing waiting.
    may_purge: bool
    purge: PurgeView | None  # the latest request, if any


# --- reads -------------------------------------------------------------------------


_OFFBOARDED: Final = text(
    "SELECT tenant_id, canonical_name, short_code, internal, status, offboarded_at,"
    " offboarded_by, offboard_reason, purge_after, purged_at, purge_id, purge_status,"
    " purge_requested_at, purge_finished_at, purge_outcome, now()"
    " FROM access.offboarded_tenants(:actor)"
)


def _offboarded(row: tuple[Any, ...], access: Access) -> OffboardedTenant:
    (tid, name, code, internal, status, at, by, reason, after, purged, pid, pstatus,
     prequested, pfinished, poutcome, now) = row
    waiting = pstatus in ("requested", "running")
    return OffboardedTenant(
        tenant=TenantView(id=tid, name=name), short_code=code, internal=internal,
        status=status, offboarded_at=at, offboarded_by_me=by == access.identity.oid,
        reason=reason, purge_after=after, purged_at=purged,
        may_restore=status == "offboarded" and now < after,
        may_purge=status in ("offboarded", "purging") and now >= after and not waiting,
        purge=None if pid is None else PurgeView(
            id=pid, status=pstatus, requested_at=prequested, finished_at=pfinished,
            outcome=poutcome),
    )


@router.get("/offboarded", response_model=list[OffboardedTenant])
@requires(LIFECYCLE_PERMISSION)
async def list_offboarded(request: Request, access: AccessDep) -> list[OffboardedTenant]:
    """Offboarded, purging and purged tenants this platform administrator is assigned
    (or every one), latest first: the only place a tenant that has left is listed."""
    _platform_admin(access)
    async with _guarded(request, access) as conn:
        rows = [tuple(r) for r in await conn.execute(
            _OFFBOARDED, {"actor": access.identity.oid})]
    return [_offboarded(r, access) for r in rows]


# --- writes ------------------------------------------------------------------------


@router.post("/{tenant_id}/edit", response_model=TenantEdited)
@requires(EDIT_PERMISSION)
async def edit_tenant(request: Request, tenant_id: UUID, body: TenantEdit,
                      access: AccessDep) -> TenantEdited:
    """Rename, or change the short code where it is safe. An internal tenant also
    needs a platform administrator, as onboarding one does (ADR 0031)."""
    _staff(access)
    name = body.canonical_name.strip()
    if not name:
        raise ApiError(422, "invalid_request")
    async with _guarded(request, access) as conn:
        await conn.execute(text("SELECT access.update_tenant(:a, :t, :n, :c)"), {
            "a": access.identity.oid, "t": tenant_id, "n": name, "c": body.short_code})
    audit_only(request, [tenant_id])
    return TenantEdited(tenant=TenantView(id=tenant_id, name=name), short_code=body.short_code)


@router.post("/{tenant_id}/offboard", response_model=Offboarded)
@requires(LIFECYCLE_PERMISSION)
async def offboard_tenant(request: Request, tenant_id: UUID, body: OffboardRequest,
                          access: AccessDep) -> Offboarded:
    """The console's Delete: effective at once. Recorded under the tenant while it is
    still in this request's scope."""
    _platform_admin(access)
    if not body.reason.strip():
        raise ApiError(422, "invalid_request")
    async with _guarded(request, access) as conn:
        purge_after = await conn.scalar(
            text("SELECT access.offboard_tenant(:a, :t, :n, :r)"),
            {"a": access.identity.oid, "t": tenant_id, "n": body.confirm_name,
             "r": body.reason})
    audit_only(request, [tenant_id])
    return Offboarded(tenant=TenantView(id=tenant_id, name=body.confirm_name),
                      status="offboarded", purge_after=purge_after)


@router.post("/{tenant_id}/restore", response_model=Restored)
@requires(LIFECYCLE_PERMISSION)
async def restore_tenant(request: Request, tenant_id: UUID, access: AccessDep) -> Restored:
    """Back within the retention. In every scope that holds it again from the next
    request; its credentials stay revoked and its portal suspended."""
    _platform_admin(access)
    async with _guarded(request, access) as conn:
        await conn.execute(text("SELECT access.restore_tenant(:a, :t)"),
                           {"a": access.identity.oid, "t": tenant_id})
    return Restored(tenant_id=tenant_id, status="restored")


@router.post("/{tenant_id}/purge", response_model=OffboardedTenant, status_code=202)
@requires(LIFECYCLE_PERMISSION)
async def purge_tenant(request: Request, tenant_id: UUID, body: PurgeRequest,
                       access: AccessDep) -> OffboardedTenant:
    """Ask for the purge, after the retention, with the name and the short code typed.
    Accepted, not done: the purge worker runs it within a minute or two."""
    _platform_admin(access)
    async with _guarded(request, access) as conn:
        await conn.execute(text("SELECT access.request_tenant_purge(:a, :t, :n, :c)"), {
            "a": access.identity.oid, "t": tenant_id, "n": body.confirm_name,
            "c": body.confirm_code})
        rows = [tuple(r) for r in await conn.execute(
            _OFFBOARDED, {"actor": access.identity.oid})]
    found = [r for r in rows if r[0] == tenant_id]
    if not found:  # pragma: no cover - the function just found it
        raise ApiError(404, "not_found")
    return _offboarded(found[0], access)
