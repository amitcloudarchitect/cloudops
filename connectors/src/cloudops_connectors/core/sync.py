"""How often customer data syncs, and syncing now when a customer is onboarded
(ADR 0037).

**Every six hours** (``CUSTOMER_SYNC``): SharePoint, Qualys and CoreStack, whose
sources have no change feed worth polling more often. Connector health expects the
same interval (``staging.connector_schedule``, migration 0035). The schedule job
applies a changed interval to an existing schedule (``cli.upsert_schedule``
replaces its spec); it never triggers a run on an update.

**Once, now, when a customer is onboarded.** The database queues a
``staging.sync_request`` for a source when a tenant is onboarded, an alias for that
source is approved, or an account is added (a passed credential check adds them):
triggers, migration 0035. The API starts nothing (ADR 0015's pattern). Each of
these sources' workers polls every minute (``serve``), claims its own source's
waiting request and triggers each of its schedules once. ``BUFFER_ONE``: a run
already going started before the change, so one more runs after it rather than
being skipped. The claim and the triggers share a transaction, so a trigger that
fails leaves the request waiting for the next pass. A schedule not created yet is
passed over: the schedule job's first creation runs it anyway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from temporalio.client import Client, ScheduleOverlapPolicy
from temporalio.service import RPCError, RPCStatusCode

from cloudops_connectors.core.ingest import principal
from cloudops_tenancy import Database, Role, TenantScope

log = logging.getLogger(__name__)

CUSTOMER_SYNC: Final = timedelta(hours=6)
# The sources the database queues requests for (migration 0035, SYNC_SOURCES).
SYNC_SOURCES: Final = frozenset({"sharepoint", "qualys", "corestack"})
POLL: Final = timedelta(minutes=1)
KEEP: Final = timedelta(days=30)  # claimed requests, kept for a person to read

_CLAIM: Final = text(
    "UPDATE staging.sync_request SET claimed_at = now()"
    " WHERE source_system = :source AND claimed_at IS NULL RETURNING reason"
)
_PRUNE: Final = text(
    "DELETE FROM staging.sync_request"
    " WHERE source_system = :source AND claimed_at < now() - CAST(:keep AS interval)"
)


async def claim(db: Database, temporal: Client, source: str,
                schedule_ids: Iterable[str]) -> list[str]:
    """Claim the source's waiting request, if any, and trigger its schedules once.
    Returns the schedules triggered."""
    async with db.transaction(TenantScope((), Role.INGEST, principal(source))) as conn:
        await conn.execute(_PRUNE, {"source": source, "keep": f"{KEEP.days} days"})
        reasons = [str(r[0]) for r in await conn.execute(_CLAIM, {"source": source})]
        if not reasons:
            return []
        triggered: list[str] = []
        for schedule_id in schedule_ids:
            try:
                await temporal.get_schedule_handle(schedule_id).trigger(
                    overlap=ScheduleOverlapPolicy.BUFFER_ONE)
            except RPCError as exc:
                if exc.status is not RPCStatusCode.NOT_FOUND:
                    raise
                continue
            triggered.append(schedule_id)
        log.info("sync requested (%s): triggered %s", ", ".join(reasons),
                 ", ".join(triggered) or "nothing yet")
        return triggered


async def serve(db: Database, temporal: Client, source: str,
                schedule_ids: Iterable[str], interval: timedelta = POLL) -> None:
    """Claim requests every ``interval`` until cancelled."""
    ids = tuple(schedule_ids)
    while True:
        try:
            await claim(db, temporal, source, ids)
        except (DBAPIError, RPCError, OSError):
            log.exception("sync request pass failed; trying again")
        await asyncio.sleep(interval.total_seconds())
