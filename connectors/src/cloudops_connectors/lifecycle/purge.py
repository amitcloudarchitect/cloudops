"""Purging an offboarded tenant (ADR 0038).

A platform administrator asks for a purge once the retention has ended
(``access.request_tenant_purge``); the API records it and starts nothing (ADR 0015's
pattern). This worker, every minute (``serve``):

1. **Claims** the oldest waiting purge with ``cloudops.claim_tenant_purge``, before
   it knows the tenant, from an empty scope.
2. **Bronze.** Under the tenant's scope it reads the ids of every stored credential
   the tenant ever had (revoked ones included: offboarding revoked them), and
   deletes each bronze page whose metadata names one (``instance`` ends
   ``#credential=<id>``, ADR 0027). Those pages hold that tenant's estate alone.
   Pages a platform-wide source fetched (a ServiceNow page, the platform's own cost
   feed) hold many tenants' records and are not deleted: the ADR says why.
3. **Telemetry.** With a column store configured, its rows for the tenant at every
   grain (``TelemetryStore.purge``). Without one: recorded as ``not_configured``.
4. **The database.** ``cloudops.purge_tenant`` deletes the tenant's rows from every
   table, keeps the audit log, leaves the tombstone and closes the request, in one
   transaction.

Any failure closes the request ``failed`` with a code, never payload text, and the
tenant stays ``purging``: nothing is restored, and a platform administrator asks
again. Steps 2 and 3 are idempotent, so a second attempt finishes the first.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Protocol
from uuid import UUID

from azure.storage.blob.aio import ContainerClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_tenancy import Database, Role, TenantScope
from cloudops_tenancy.telemetry import TelemetryStore

log = logging.getLogger(__name__)

WHO: Final = "worker:purge"
# The connector whose worker runs the purge loop: stored credentials' pages land in
# its bronze container (ADR 0027), AWS ones too.
HOST: Final = "azure"
POLL: Final = timedelta(minutes=1)
CREDENTIAL_MARK: Final = "#credential="

_CLAIM: Final = text("SELECT id, tenant_id FROM cloudops.claim_tenant_purge()")
# Every credential the tenant ever had: a revoked one's pages are still its.
_CREDENTIALS: Final = text("SELECT id FROM cloudops.customer_credential WHERE tenant_id = :t")
_PURGE: Final = text("SELECT cloudops.purge_tenant(:p, :t, :blobs, :telemetry)")
_FAIL: Final = text("SELECT cloudops.fail_tenant_purge(:p, :outcome)")


class BronzePages(Protocol):
    async def credential_pages(self, credentials: frozenset[str]) -> list[str]:
        """Page references whose metadata names one of these credentials."""
        ...

    async def delete(self, ref: str) -> None: ...


class BlobBronzePages:
    """A bronze container. Every page's metadata is read: the layout is by source and
    day (``raw.page_ref``), never by tenant."""

    def __init__(self, container: ContainerClient) -> None:
        self._container = container

    async def credential_pages(self, credentials: frozenset[str]) -> list[str]:
        found: list[str] = []
        async for blob in self._container.list_blobs(include=["metadata"]):
            if credential_of((blob.metadata or {}).get("instance", "")) in credentials:
                found.append(blob.name)
        return sorted(found)

    async def delete(self, ref: str) -> None:
        await self._container.delete_blob(ref, delete_snapshots="include")


def credential_of(instance: str) -> str | None:
    """The credential id an ingest instance names, if it is a stored credential's."""
    _, mark, credential = instance.rpartition(CREDENTIAL_MARK)
    return credential if mark and credential else None


@dataclass(frozen=True)
class PurgeReport:
    purge_id: UUID
    tenant_id: UUID
    status: str  # succeeded or failed
    outcome: str | None
    blobs_deleted: int
    telemetry: str
    row_counts: dict[str, int]


class TenantPurge:
    def __init__(self, db: Database, bronze: Iterable[BronzePages],
                 telemetry: TelemetryStore | None = None) -> None:
        self._db = db
        self._bronze = tuple(bronze)
        self._telemetry = telemetry

    @staticmethod
    def _scope(tenant: UUID | None = None) -> TenantScope:
        return TenantScope(() if tenant is None else {tenant}, Role.INGEST, WHO)

    async def run(self) -> list[PurgeReport]:
        """Every waiting purge, oldest first."""
        reports: list[PurgeReport] = []
        while (report := await self.purge_one()) is not None:
            reports.append(report)
        return reports

    async def purge_one(self) -> PurgeReport | None:
        async with self._db.transaction(self._scope()) as conn:
            claimed = (await conn.execute(_CLAIM)).one_or_none()
        if claimed is None:
            return None
        row = tuple(claimed)
        purge, tenant = UUID(str(row[0])), UUID(str(row[1]))
        step = "bronze"
        blobs, telemetry = 0, "not_configured"
        try:
            async with self._db.transaction(self._scope(tenant)) as conn:
                credentials = frozenset(str(r[0]) for r in await conn.execute(
                    _CREDENTIALS, {"t": tenant}))
            if credentials:
                for bronze in self._bronze:
                    for ref in await bronze.credential_pages(credentials):
                        await bronze.delete(ref)
                        blobs += 1
            step = "telemetry"
            if self._telemetry is not None:
                await self._telemetry.purge(self._scope(tenant))
                telemetry = "deleted"
            step = "database"
            async with self._db.transaction(self._scope()) as conn:
                counts: dict[str, Any] = await conn.scalar(_PURGE, {
                    "p": purge, "t": tenant, "blobs": blobs, "telemetry": telemetry})
        except Exception:  # recorded, never left running; the type only, no payload
            log.exception("purge %s failed at %s", purge, step)
            outcome = f"{step}_failed"
            async with self._db.transaction(self._scope()) as conn:
                await conn.execute(_FAIL, {"p": purge, "outcome": outcome})
            return PurgeReport(purge, tenant, "failed", outcome, blobs, telemetry, {})
        log.info("purge %s succeeded: %d tables, %d bronze pages, telemetry %s",
                 purge, len(counts), blobs, telemetry)
        return PurgeReport(purge, tenant, "succeeded", None, blobs, telemetry,
                           {k: int(v) for k, v in counts.items()})


async def serve(purge: TenantPurge, interval: timedelta = POLL) -> None:
    """Claim purges every ``interval`` until cancelled."""
    while True:
        try:
            await purge.run()
        except (DBAPIError, OSError):
            log.exception("purge pass failed; trying again")
        await asyncio.sleep(interval.total_seconds())
