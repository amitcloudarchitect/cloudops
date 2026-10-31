"""Who a metric belongs to, and whether it is kept.

The same identity rules as the batch connectors (``core/identity.py``): the
account id resolves to a tenant only by an exact, approved alias (an unknown
one is quarantined for the customer mapping); the resource must exist in that
tenant's inventory under that account (an unknown one is quarantined with a
count). Then the allowlist: only (resource type, metric) pairs in
``staging.metric_allowlist`` are stored (LLD open item L3).

At design volume (50 million samples a day) these lookups cannot run per
sample, so answers are cached for ``ttl`` seconds, negative answers too: a
newly mapped subscription or resource is picked up within one TTL.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.core import identity
from cloudops_connectors.telemetry.normalise import AWS, AZURE
from cloudops_tenancy import Database, Role, TenantScope

PRINCIPAL: Final = "connector:telemetry"
_ALLOWLIST: Final = text("SELECT type_canonical, metric FROM staging.metric_allowlist")
_RESOURCES: Final = text(
    "SELECT id, tenant_id, account_id, lower(native_id),"
    "       lower(regexp_replace(native_id, '^.*/', '')), type_canonical"
    " FROM cloudops.resource"
    " WHERE account_id = ANY(CAST(:accounts AS uuid[])) AND deleted_at IS NULL"
    "   AND (lower(native_id) = ANY(CAST(:keys AS text[]))"
    "        OR lower(regexp_replace(native_id, '^.*/', '')) = ANY(CAST(:keys AS text[])))"
)
_PROVIDER: Final = {AZURE: "azure", AWS: "aws"}


@dataclass(frozen=True, slots=True)
class Account:
    tenant_id: UUID
    account_id: UUID | None  # None: mapped, but the inventory has not seen it yet


@dataclass(frozen=True, slots=True)
class Resource:
    id: UUID
    tenant_id: UUID
    account_id: UUID
    type_canonical: str


class Resolver:
    def __init__(self, db: Database, *, ttl: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._db = db
        self._ttl = ttl
        self._clock = clock
        self._accounts: dict[tuple[str, str], tuple[float, Account | None]] = {}
        self._resources: dict[tuple[str, str, str], tuple[float, Resource | None]] = {}
        self._allow: tuple[float, frozenset[tuple[str, str]]] | None = None

    def _fresh(self, at: float) -> bool:
        return self._clock() - at < self._ttl

    async def allowlist(self) -> frozenset[tuple[str, str]]:
        if self._allow is None or not self._fresh(self._allow[0]):
            async with self._db.transaction(TenantScope((), Role.INGEST, PRINCIPAL)) as conn:
                pairs = frozenset((str(r[0]), str(r[1])) for r in
                                  (tuple(x) for x in await conn.execute(_ALLOWLIST)))
            self._allow = (self._clock(), pairs)
        return self._allow[1]

    async def accounts(self, wanted: Iterable[tuple[str, str]]) -> dict[tuple[str, str],
                                                                         Account | None]:
        wanted = set(wanted)
        missing = [k for k in wanted if k not in self._accounts
                   or not self._fresh(self._accounts[k][0])]
        if missing:
            now = self._clock()
            async with self._db.transaction(TenantScope((), Role.INGEST, PRINCIPAL)) as conn:
                tenants: dict[tuple[str, str], UUID | None] = {}
                for provider in {p for p, _ in missing}:
                    ids = [a for p, a in missing if p == provider]
                    for native, tenant in (await identity.resolve_ids(
                            conn, _PROVIDER[provider], ids)).items():
                        tenants[(provider, native)] = tenant
            found = {t for t in tenants.values() if t is not None}
            known: dict[tuple[str, str], UUID] = {}
            if found:
                async with self._db.transaction(TenantScope(found, Role.INGEST,
                                                            PRINCIPAL)) as conn:
                    for provider in {p for p, _ in missing}:
                        rows = await identity.known_accounts(
                            conn, _PROVIDER[provider], [a for p, a in missing if p == provider])
                        known.update({(provider, k): v[0] for k, v in rows.items()})
            for key in missing:
                tenant = tenants.get(key)
                self._accounts[key] = (now, None if tenant is None else Account(
                    tenant, known.get(key)))
        return {k: self._accounts[k][1] for k in wanted}

    async def resources(
        self, wanted: Iterable[tuple[str, str, str]], accounts: dict[tuple[str, str],
                                                                     Account | None],
    ) -> dict[tuple[str, str, str], Resource | None]:
        """(provider, account, resource key) -> the resource, for mapped accounts."""
        wanted = set(wanted)
        missing = [k for k in wanted if k not in self._resources
                   or not self._fresh(self._resources[k][0])]
        by_account: dict[UUID, list[tuple[str, str, str]]] = {}
        tenants: set[UUID] = set()
        for key in missing:
            acct = accounts.get((key[0], key[1]))
            if acct is not None and acct.account_id is not None:
                by_account.setdefault(acct.account_id, []).append(key)
                tenants.add(acct.tenant_id)
        now = self._clock()
        found: dict[tuple[str, str, str], Resource] = {}
        if by_account:
            async with self._db.transaction(TenantScope(tenants, Role.INGEST, PRINCIPAL)) as conn:
                rows = [tuple(r) for r in await conn.execute(_RESOURCES, {
                    "accounts": list(by_account),
                    "keys": sorted({k[2] for ks in by_account.values() for k in ks}),
                })]
            for acct_id, keys in by_account.items():
                for key in keys:
                    hits = {r[0] for r in rows if r[2] == acct_id and key[2] in (r[3], r[4])}
                    if len(hits) == 1:
                        row = next(r for r in rows if r[0] in hits)
                        found[key] = Resource(UUID(str(row[0])), UUID(str(row[1])),
                                              UUID(str(row[2])), str(row[5]))
        for key in missing:
            self._resources[key] = (now, found.get(key))
        return {k: self._resources[k][1] for k in wanted}
