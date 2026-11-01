"""Customer master endpoints (step 14b, HLD M1, ADR 0015): the alias quarantine,
tenants with their aliases, onboarding, and proposing and deciding a mapping of
a source's company name to a tenant.

Every endpoint needs ``mapping.manage``, which no customer role can hold. The
writes are the database's functions (migration 0009), called under the caller's
scope with the caller's object id: they check the permission, the tenant's
assignment and scope, and the two-person rule again, so this module is a thin
door, not the guard. Their refusals map to structured codes here; nothing from
the database reaches the client.

A tenant is internal to Getronics or a customer, chosen when it is onboarded and
changeable later, and at most one internal tenant hosts the platform (ADR 0031).
Both are a platform administrator's (``access.manage``): onboarding an internal
tenant needs it as well as ``mapping.manage``, and the database checks it again.
An internal tenant is reported on exactly like a customer.

A mapping changes only what is ingested from now on. Records already held in
quarantine move only when someone asks, here, after approval: the preview says
how many sightings were held (an upper bound: overlap and replays count again),
the request is a row the connector worker picks up and runs through the bronze
replay narrowed to that name, and the request reports the exact number of
records that moved (ADR 0015). The API starts nothing itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
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
from cloudops_api.provenance import ApiModel, Metric, query_ref
from cloudops_tenancy import ScopedConnection

router = APIRouter(prefix="/v1/mapping", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

PERMISSION: Final = "mapping.manage"
KIND_PERMISSION: Final = "access.manage"
SourceSystem = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,40}$")]
MAX_PROPOSALS: Final = 500

# The functions' refusals (migration 0009), by SQLSTATE. Anything else is a 500.
_REFUSALS: Final = {
    "42501": (403, "forbidden"),
    "CMSCP": (404, "not_found"),  # a tenant outside scope reads as one that does not exist
    "P0002": (404, "not_found"),
    "CMDUP": (409, "conflict"),
    "CMTWO": (409, "second_approver_required"),
    "CMNOT": (409, "not_approved"),
    "CMNIL": (409, "nothing_held"),
    "CMSTL": (409, "mapping_changed"),
    "23514": (422, "invalid_request"),
    "TKHST": (409, "platform_host_rule"),  # the host is internal; only internal hosts
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
    # The database refuses a customer anyway; this keeps the reason out of reach.
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


# --- views -------------------------------------------------------------------------


class QuarantinedAlias(ApiModel):
    id: str  # an identifier, not a quantity
    source_system: str
    alias: str
    occurrences: Metric[int]
    first_seen: datetime
    last_seen: datetime
    suggested_tenant: TenantView | None
    pending_proposal_id: UUID | None


class QuarantineList(ApiModel):
    items: list[QuarantinedAlias]
    names: Metric[int]  # distinct names held, every source
    sightings: Metric[int]  # their sightings: overlap and replays count again
    # How many people an approval needs here: 1 in dev, 2 in UAT and production.
    approvers_required: Literal[1, 2]


class AliasView(ApiModel):
    source_system: str
    alias: str
    confirmed_at: datetime | None


class TenantAliases(ApiModel):
    tenant: TenantView
    short_code: str
    status: str
    internal: bool  # internal to Getronics; reported like a customer (ADR 0031)
    hosts_platform: bool  # the platform runs in its subscription
    aliases: list[AliasView]
    # Cloud accounts it already holds, by provider ("azure": 1): a customer may hold
    # several, and onboarding refuses one another tenant holds (ADR 0031).
    accounts: dict[str, int]


class OnboardRequest(ApiModel):
    canonical_name: str = Field(min_length=1, max_length=200)
    short_code: str = Field(pattern=r"^[A-Z0-9]{2,10}$")
    # Required: a platform administrator chooses, never a default (ADR 0031).
    internal: bool


class OnboardedTenant(ApiModel):
    tenant: TenantView
    short_code: str
    internal: bool


class TenantKindRequest(ApiModel):
    internal: bool


class PlatformHostRequest(ApiModel):
    tenant_id: UUID | None  # None: no tenant hosts the platform


class TenantKind(ApiModel):
    tenant: TenantView
    internal: bool
    hosts_platform: bool


class ProposalRequest(ApiModel):
    source_system: SourceSystem
    # Exactly as the source sends it: never trimmed, the match is exact.
    alias: str = Field(min_length=1, max_length=200)
    tenant_id: UUID
    note: str | None = Field(default=None, max_length=1000)


class DecisionRequest(ApiModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=1000)


class DecisionView(ApiModel):
    decision: Literal["approve", "reject"]
    reason: str | None
    decided_by: UUID
    decided_at: datetime
    # Approved: the quarantine row this mapping cleared, as it stood.
    held_occurrences: Metric[int] | None
    held_first_seen: datetime | None
    held_last_seen: datetime | None


ReprocessStatus = Literal["requested", "running", "succeeded", "refused", "failed"]


class ReprocessView(ApiModel):
    id: UUID
    status: ReprocessStatus
    requested_by_me: bool
    requested_at: datetime
    window_start: date  # the bronze days replayed, inclusive
    window_end: date
    started_at: datetime | None
    finished_at: datetime | None
    # Records that were held and are now in the tenant; set when the run ends.
    moved: Metric[int] | None
    outcome: str | None  # why it was refused or failed: a code, never payload text


class ReprocessPreview(ApiModel):
    available: bool
    # Why not: not_approved, nothing_held, mapping_changed or already_requested.
    reason: str | None
    # Sightings held when approved: what the run can move at most, never more.
    upper_bound: Metric[int] | None
    window_start: date | None
    window_end: date | None


class ProposalView(ApiModel):
    id: UUID
    tenant: TenantView
    source_system: str
    alias: str
    note: str | None
    proposed_by: UUID
    proposed_at: datetime
    proposed_by_me: bool
    decision: DecisionView | None
    # Pending, and this caller may approve it (two-person rule).
    may_approve: bool
    reprocess: ReprocessView | None  # the latest request, if any


# --- reads -------------------------------------------------------------------------


async def _approvers(conn: ScopedConnection) -> Literal[1, 2]:
    return 1 if int(await conn.scalar(text("SELECT access.mapping_approvers()"))) == 1 else 2


@router.get("/quarantine", response_model=QuarantineList)
@requires(PERMISSION)
async def list_quarantine(request: Request, access: AccessDep) -> QuarantineList:
    """Names no tenant matched, across every source, most sighted first. Cross-tenant
    by nature, so only for mapping.manage; never the raw payload reference."""
    _staff(access)
    async with _guarded(request, access) as conn:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in await conn.execute(text(
            "SELECT q.id, q.source_system, q.alias, q.occurrence_count, q.first_seen,"
            " q.last_seen, q.suggested_tenant_id, t.canonical_name, q.pending_proposal_id"
            " FROM access.alias_quarantine(:actor) q"
            " LEFT JOIN cloudops.tenant t ON t.id = q.suggested_tenant_id"
            " ORDER BY q.occurrence_count DESC, q.last_seen DESC, q.id"),
            {"actor": access.identity.oid})]
        approvers = await _approvers(conn)
    now = datetime.now(UTC)

    def total(value: int, unit: str, name: str) -> Metric[int]:
        return Metric[int](value=value, unit=unit, source_system="cloudops_quarantine",
                           as_of=now, query_ref=query_ref(name, 1, live=True),
                           is_modelled=False)

    return QuarantineList(
        names=total(len(rows), "names", "mapping.quarantine_names"),
        sightings=total(sum(int(r[3]) for r in rows), "sightings",
                        "mapping.quarantine_sightings_total"),
        items=[QuarantinedAlias(
            id=str(r[0]), source_system=r[1], alias=r[2],
            occurrences=Metric[int](
                value=int(r[3]), unit="sightings", source_system=r[1], as_of=r[5],
                query_ref=query_ref("mapping.quarantine_sightings", 1, id=r[0]),
                is_modelled=False),
            first_seen=r[4], last_seen=r[5],
            suggested_tenant=None if r[6] is None or r[7] is None
            else TenantView(id=r[6], name=r[7]),
            pending_proposal_id=r[8],
        ) for r in rows],
        approvers_required=approvers,
    )


@router.get("/tenants", response_model=list[TenantAliases])
@requires(PERMISSION)
async def list_tenants(request: Request, access: AccessDep) -> list[TenantAliases]:
    _staff(access)
    async with _guarded(request, access) as conn:
        tenants = [tuple(r) for r in await conn.execute(text(
            "SELECT id, canonical_name, short_code, status, internal, hosts_platform"
            " FROM cloudops.tenant ORDER BY canonical_name"))]
        aliases = [tuple(r) for r in await conn.execute(text(
            "SELECT tenant_id, source_system, alias, confirmed_at FROM cloudops.tenant_alias"
            " ORDER BY source_system, alias"))]
        held = [tuple(r) for r in await conn.execute(text(
            "SELECT tenant_id, provider, count(*) FROM cloudops.cloud_account"
            " GROUP BY tenant_id, provider ORDER BY provider"))]
    by_tenant: dict[UUID, list[AliasView]] = {}
    for tid, source, alias, confirmed in aliases:
        by_tenant.setdefault(tid, []).append(
            AliasView(source_system=source, alias=alias, confirmed_at=confirmed))
    accounts: dict[UUID, dict[str, int]] = {}
    for tid, provider, count in held:
        accounts.setdefault(tid, {})[provider] = int(count)
    return [TenantAliases(tenant=TenantView(id=t[0], name=t[1]), short_code=t[2], status=t[3],
                          internal=t[4], hosts_platform=t[5], aliases=by_tenant.get(t[0], []),
                          accounts=accounts.get(t[0], {})) for t in tenants]


_PROPOSALS: Final = (
    "SELECT p.id, p.tenant_id, t.canonical_name, p.source_system, p.alias, p.note,"
    " p.proposed_by, p.proposed_at, d.decision, d.reason, d.decided_by, d.decided_at,"
    " d.held_occurrences, d.held_first_seen, d.held_last_seen,"
    " r.id, r.status, r.requested_by, r.requested_at, r.window_start, r.window_end,"
    " r.started_at, r.finished_at, r.moved, r.outcome"
    " FROM cloudops.alias_proposal p JOIN cloudops.tenant t ON t.id = p.tenant_id"
    " LEFT JOIN cloudops.alias_decision d ON d.proposal_id = p.id"
    " LEFT JOIN LATERAL (SELECT * FROM cloudops.alias_reprocess x WHERE x.proposal_id = p.id"
    "   ORDER BY x.requested_at DESC LIMIT 1) r ON true"
)


def _reprocess(row: tuple[Any, ...], source: str, access: Access) -> ReprocessView | None:
    (rid, status, by, at, start, end, started, finished, moved, outcome) = row
    if rid is None:
        return None
    return ReprocessView(
        id=rid, status=status, requested_by_me=by == access.identity.oid, requested_at=at,
        window_start=start, window_end=end, started_at=started, finished_at=finished,
        moved=None if moved is None else Metric[int](
            value=int(moved), unit="records", source_system=source, as_of=finished or at,
            query_ref=query_ref("mapping.reprocess_moved", 1, request=str(rid)),
            is_modelled=False),
        outcome=outcome,
    )


def _proposal(row: tuple[Any, ...], access: Access, approvers: int) -> ProposalView:
    (pid, tid, tname, source, alias, note, by, at, decision, reason, decided_by, decided_at,
     held, held_first, held_last) = row[:15]
    mine = by == access.identity.oid
    return ProposalView(
        id=pid, tenant=TenantView(id=tid, name=tname), source_system=source, alias=alias,
        note=note, proposed_by=by, proposed_at=at, proposed_by_me=mine,
        decision=None if decision is None else DecisionView(
            decision=decision, reason=reason, decided_by=decided_by, decided_at=decided_at,
            held_occurrences=None if held is None else Metric[int](
                value=int(held), unit="sightings", source_system=source, as_of=decided_at,
                query_ref=query_ref("mapping.held_at_approval", 1, proposal=str(pid)),
                is_modelled=False),
            held_first_seen=held_first, held_last_seen=held_last),
        may_approve=decision is None and (approvers == 1 or not mine),
        reprocess=_reprocess(row[15:], source, access),
    )


@router.get("/proposals", response_model=list[ProposalView])
@requires(PERMISSION)
async def list_proposals(
    request: Request,
    access: AccessDep,
    status: Annotated[Literal["pending", "decided", "all"], Query()] = "pending",
) -> list[ProposalView]:
    _staff(access)
    where = {"pending": " WHERE d.id IS NULL", "decided": " WHERE d.id IS NOT NULL",
             "all": ""}[status]
    async with _guarded(request, access) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            f"{_PROPOSALS}{where} ORDER BY coalesce(d.decided_at, p.proposed_at) DESC"
            " LIMIT :limit"), {"limit": MAX_PROPOSALS})]
        approvers = await _approvers(conn)
    return [_proposal(r, access, approvers) for r in rows]


# --- writes ------------------------------------------------------------------------


@router.post("/tenants", response_model=OnboardedTenant, status_code=201)
@requires(PERMISSION)
async def onboard_tenant(
    request: Request, body: OnboardRequest, access: AccessDep,
) -> OnboardedTenant:
    """A new tenant, internal or a customer. Every all-tenant principal can see it
    from its next request (ADR 0002, D9); a principal assigned named tenants needs
    it granted. Internal needs a platform administrator (ADR 0031)."""
    _staff(access)
    if body.internal and not access.allows(KIND_PERMISSION):
        raise ApiError(403, "forbidden")
    name = body.canonical_name.strip()
    if not name:
        raise ApiError(422, "invalid_request")
    async with _guarded(request, access) as conn:
        created = await conn.scalar(
            text("SELECT access.onboard_tenant(:actor, :name, :code, :internal)"),
            {"actor": access.identity.oid, "name": name, "code": body.short_code,
             "internal": body.internal})
    return OnboardedTenant(tenant=TenantView(id=created, name=name), short_code=body.short_code,
                           internal=body.internal)


_KIND: Final = text("SELECT id, canonical_name, internal, hosts_platform FROM cloudops.tenant"
                    " WHERE id = :id")


async def _kind(conn: ScopedConnection, tenant: UUID) -> TenantKind:
    found = (await conn.execute(_KIND, {"id": tenant})).one_or_none()
    if found is None:
        raise ApiError(404, "not_found")
    r = tuple(found)
    return TenantKind(tenant=TenantView(id=r[0], name=r[1]), internal=r[2], hosts_platform=r[3])


@router.post("/tenants/{tenant_id}/kind", response_model=TenantKind)
@requires(KIND_PERMISSION)
async def set_kind(request: Request, tenant_id: UUID, body: TenantKindRequest,
                   access: AccessDep) -> TenantKind:
    """Internal to Getronics, or a customer (ADR 0031). The platform's host must
    stay internal: move the host first."""
    _staff(access)
    async with _guarded(request, access) as conn:
        await conn.execute(text("SELECT access.set_tenant_internal(:actor, :t, :i)"),
                           {"actor": access.identity.oid, "t": tenant_id, "i": body.internal})
        return await _kind(conn, tenant_id)


@router.post("/platform-host", response_model=TenantKind | None)
@requires(KIND_PERMISSION)
async def set_platform_host(request: Request, body: PlatformHostRequest,
                            access: AccessDep) -> TenantKind | None:
    """The internal tenant whose subscription the platform runs in: its platform
    resources are the platform's own cost, and help-only assistant history is kept
    under it (ADR 0031). At most one; naming another moves it."""
    _staff(access)
    async with _guarded(request, access) as conn:
        await conn.execute(text("SELECT access.set_platform_host(:actor, :t)"),
                           {"actor": access.identity.oid, "t": body.tenant_id})
        return None if body.tenant_id is None else await _kind(conn, body.tenant_id)


async def _one(conn: ScopedConnection, proposal: UUID, access: Access) -> ProposalView:
    found = (await conn.execute(text(f"{_PROPOSALS} WHERE p.id = :id"),
                                {"id": proposal})).one()
    return _proposal(tuple(found), access, await _approvers(conn))


@router.post("/proposals", response_model=ProposalView, status_code=201)
@requires(PERMISSION)
async def propose(request: Request, body: ProposalRequest, access: AccessDep) -> ProposalView:
    _staff(access)
    if not body.alias.strip():
        raise ApiError(422, "invalid_request")
    async with _guarded(request, access) as conn:
        created = await conn.scalar(text(
            "SELECT access.propose_alias(:actor, :source, :alias, :tenant, :note)"),
            {"actor": access.identity.oid, "source": body.source_system, "alias": body.alias,
             "tenant": body.tenant_id, "note": body.note})
        view = await _one(conn, created, access)
    audit_only(request, [body.tenant_id])
    return view


@router.post("/proposals/{proposal_id}/decision", response_model=ProposalView, status_code=201)
@requires(PERMISSION)
async def decide(
    request: Request, proposal_id: UUID, body: DecisionRequest, access: AccessDep,
) -> ProposalView:
    """Approve: the name maps to the tenant from the next ingest on, and its
    quarantine row is cleared (its counts kept on the decision). Reject: needs a
    reason. Either is final."""
    _staff(access)
    if body.decision == "reject" and not (body.reason or "").strip():
        raise ApiError(422, "invalid_request")
    async with _guarded(request, access) as conn:
        await conn.execute(text("SELECT access.decide_alias(:actor, :id, :decision, :reason)"),
                           {"actor": access.identity.oid, "id": proposal_id,
                            "decision": body.decision, "reason": body.reason})
        view = await _one(conn, proposal_id, access)
    audit_only(request, [view.tenant.id])
    return view


# --- re-processing what an approved name held ----------------------------------------


@router.get("/proposals/{proposal_id}/reprocess", response_model=ReprocessPreview)
@requires(PERMISSION)
async def preview_reprocess(
    request: Request, proposal_id: UUID, access: AccessDep,
) -> ReprocessPreview:
    """What a re-process would replay, before anyone asks for it. The bound is the
    sightings held at approval; the run reports the exact count."""
    _staff(access)
    async with _guarded(request, access) as conn:
        found = (await conn.execute(text(
            "SELECT p.tenant_id, p.source_system, d.decision, d.held_occurrences, d.decided_at,"
            # The window request_reprocess will set (migration 0009), shown in advance.
            " (d.held_first_seen AT TIME ZONE 'UTC')::date - 1,"
            " (d.held_last_seen AT TIME ZONE 'UTC')::date,"
            " EXISTS (SELECT 1 FROM cloudops.tenant_alias a WHERE a.source_system ="
            "   p.source_system AND a.alias = p.alias AND a.tenant_id = p.tenant_id),"
            " EXISTS (SELECT 1 FROM cloudops.alias_reprocess r WHERE r.proposal_id = p.id"
            "   AND r.status IN ('requested', 'running'))"
            " FROM cloudops.alias_proposal p"
            " LEFT JOIN cloudops.alias_decision d ON d.proposal_id = p.id WHERE p.id = :id"),
            {"id": proposal_id})).one_or_none()
    if found is None:
        raise ApiError(404, "not_found")
    tenant, source, decision, held, decided_at, start, end, current, open_ = tuple(found)
    audit_only(request, [tenant])
    reason = ("not_approved" if decision != "approve" else "nothing_held" if held is None
              else "mapping_changed" if not current else "already_requested" if open_
              else None)
    return ReprocessPreview(
        available=reason is None, reason=reason,
        upper_bound=None if held is None else Metric[int](
            value=int(held), unit="sightings", source_system=source, as_of=decided_at,
            query_ref=query_ref("mapping.held_at_approval", 1, proposal=str(proposal_id)),
            is_modelled=False),
        window_start=start, window_end=end,
    )


@router.post("/proposals/{proposal_id}/reprocess", response_model=ProposalView,
             status_code=201)
@requires(PERMISSION)
async def request_reprocess(
    request: Request, proposal_id: UUID, access: AccessDep,
) -> ProposalView:
    """Ask for the held records to be re-processed. Recorded and audited here; the
    connector worker runs it, re-checking the mapping first."""
    _staff(access)
    async with _guarded(request, access) as conn:
        await conn.execute(text("SELECT access.request_reprocess(:actor, :id)"),
                           {"actor": access.identity.oid, "id": proposal_id})
        view = await _one(conn, proposal_id, access)
    audit_only(request, [view.tenant.id])
    return view
