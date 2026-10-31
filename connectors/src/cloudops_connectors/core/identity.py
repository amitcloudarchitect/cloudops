"""Identity resolution shared by the step 15 connectors (LLD, Identity
resolution; ADR 0004).

Customer, by name (Qualys, CoreStack, SharePoint): ``state.resolve_aliases``,
exactly as ServiceNow does: exact, then normalised, then quarantine.

Customer, by account or directory id (Azure subscriptions, AWS accounts, Entra
directories): the id is an alias of its own source, approved through the
customer mapping (ADR 0015). Only an *exact* match counts: an id that merely
normalises to another source's spelling is not the same id. An unknown id is
quarantined, so it shows in the mapping console; nothing it carries is written
until a person maps it. Once mapped, the connector keeps ``cloud_account`` in
step with what the provider reports.

Resource (Qualys, CoreStack, CUR): external key, then ``name_norm`` plus IP,
then ``name_norm`` alone, each only if exactly one resource in the tenant
matches; otherwise unlinked and counted for the resource quarantine.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.core import state
from cloudops_tenancy import ScopedConnection

_UPSERT_ACCOUNT: Final = text(
    "INSERT INTO cloudops.cloud_account (tenant_id, provider, native_id, display_name,"
    " region_default, onboarded_at, connection_state, last_discovery)"
    " VALUES (:tenant_id, :provider, :native_id,"
    "         coalesce(CAST(:display_name AS text), :native_id), :region, now(), 'healthy',"
    "         now())"
    " ON CONFLICT (provider, native_id) DO UPDATE SET"
    "   display_name = coalesce(CAST(:display_name AS text),"
    "                           cloudops.cloud_account.display_name),"
    "   region_default = coalesce(EXCLUDED.region_default,"
    "                             cloudops.cloud_account.region_default),"
    "   last_discovery = now()"
    " RETURNING id"
)
_ACCOUNTS: Final = text(
    "SELECT native_id, id, tenant_id FROM cloudops.cloud_account"
    " WHERE provider = :provider AND native_id = ANY(CAST(:ids AS text[]))"
)


def name_norm(name: str) -> str:
    """As ``cloudops.resource.name_norm`` computes it: the short name, lowercased."""
    return name.split(".", 1)[0].lower()


async def resolve_ids(
    conn: ScopedConnection, source: str, ids: Iterable[str]
) -> dict[str, UUID | None]:
    """Account or directory ids to tenants, exact matches only."""
    resolved = await state.resolve_aliases(conn, source, ids)
    return {k: (r.tenant_id if r.outcome == "exact" else None) for k, r in resolved.items()}


@dataclass(frozen=True, slots=True)
class AccountSeen:
    native_id: str
    display_name: str | None
    region: str | None = None


async def upsert_accounts(
    conn: ScopedConnection, provider: str, tenant_of: Mapping[str, UUID],
    seen: Iterable[AccountSeen],
) -> dict[str, UUID]:
    """``cloud_account`` ids for accounts already resolved to tenants, created on
    first sight. Must run in a transaction scoped to those tenants: an account
    held by another tenant is invisible here and the upsert fails loudly."""
    out: dict[str, UUID] = {}
    for a in sorted(seen, key=lambda x: x.native_id):
        tenant = tenant_of.get(a.native_id)
        if tenant is None:
            continue
        out[a.native_id] = UUID(str(await conn.scalar(_UPSERT_ACCOUNT, {
            "tenant_id": tenant, "provider": provider, "native_id": a.native_id,
            # No name from the source keeps the onboarded one.
            "display_name": a.display_name, "region": a.region,
        })))
    return out


async def known_accounts(
    conn: ScopedConnection, provider: str, ids: Iterable[str]
) -> dict[str, tuple[UUID, UUID]]:
    """native id -> (account id, tenant id), for accounts visible in scope."""
    rows = [tuple(r) for r in await conn.execute(
        _ACCOUNTS, {"provider": provider, "ids": sorted(set(ids))})]
    return {str(r[0]): (UUID(str(r[1])), UUID(str(r[2]))) for r in rows}


@dataclass(frozen=True, slots=True)
class Candidate:
    id: UUID
    tenant_id: UUID
    external_key: str | None
    name_norm: str
    ips: frozenset[str]


def _ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ip_address(value.strip()))
    except ValueError:
        return None


def link(
    tenant: UUID, external_key: str | None, name: str | None, ip: str | None,
    candidates: Sequence[Candidate],
) -> UUID | None:
    """LLD: external key, then name_norm plus IP, then name_norm alone. A step
    that matches more than one resource links to none and falls through."""
    mine = [c for c in candidates if c.tenant_id == tenant]
    if external_key:
        hits = {c.id for c in mine if c.external_key == external_key}
        if len(hits) == 1:
            return next(iter(hits))
    if name:
        norm = name_norm(name)
        named = [c for c in mine if c.name_norm == norm]
        addr = _ip(ip)
        if addr is not None:
            hits = {c.id for c in named if addr in c.ips}
            if len(hits) == 1:
                return next(iter(hits))
        hits = {c.id for c in named}
        if len(hits) == 1:
            return next(iter(hits))
    return None


def candidate_rows(rows: Iterable[Sequence[object]]) -> list[Candidate]:
    """Rows of (id, tenant_id, external_key, name_norm, ip_addresses)."""
    out = []
    for r in rows:
        ips = r[4] if isinstance(r[4], list) else []
        out.append(Candidate(
            UUID(str(r[0])), UUID(str(r[1])), None if r[2] is None else str(r[2]), str(r[3]),
            frozenset(a for a in (_ip(str(i)) for i in ips) if a is not None),
        ))
    return out


@dataclass(frozen=True, slots=True, order=True)
class Unlinked:
    """A resource a source names that matched nothing in its tenant."""

    tenant_id: UUID
    external_key: str
    name: str | None
    account_id: UUID | None = None
    ip: str | None = None


_QUARANTINE: Final = text(
    "INSERT INTO staging.resource_quarantine"
    " (tenant_id, account_id, source_system, external_key, name, ip_address,"
    "  occurrence_count, raw_ref)"
    " VALUES (:tenant_id, :account_id, :source, :key, :name, CAST(:ip AS inet), :count,"
    "         :raw_ref)"
    " ON CONFLICT (tenant_id, source_system, external_key) DO UPDATE"
    " SET occurrence_count = staging.resource_quarantine.occurrence_count"
    "                        + EXCLUDED.occurrence_count,"
    "     last_seen = now(), name = EXCLUDED.name, ip_address = EXCLUDED.ip_address,"
    "     account_id = coalesce(EXCLUDED.account_id, staging.resource_quarantine.account_id),"
    "     raw_ref = EXCLUDED.raw_ref"
)


async def quarantine(
    conn: ScopedConnection, source: str, counts: Mapping[Unlinked, int], raw_ref: str
) -> None:
    if counts:
        await conn.execute(_QUARANTINE, [
            {"tenant_id": u.tenant_id, "account_id": u.account_id, "source": source,
             "key": u.external_key, "name": u.name, "ip": _ip(u.ip), "count": n,
             "raw_ref": raw_ref}
            for u, n in sorted(counts.items())
        ])
