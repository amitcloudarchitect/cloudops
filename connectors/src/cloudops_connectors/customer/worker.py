"""The customer-credential worker: checks, then one ingest worker per credential.

    python -m cloudops_connectors.customer worker    # serve (connector-azure pod, dev)
    python -m cloudops_connectors.customer check     # run waiting checks once (laptop)

Every ``interval`` (a minute) it:

1. **Runs waiting checks.** ``cloudops.claim_credential_check`` hands it the oldest
   one, before the tenant is known (ADR 0015's pattern: the API has no secret and
   no Temporal client, so it only records the request). Under the tenant's scope
   it reads the credential, refuses one that is revoked or expired, fails (for a
   person to fix, then ask again) one whose secret is not in the store, and
   otherwise runs the read-only check (``check.py``). A passed check creates the
   credential's ``cloud_account`` rows in its tenant; an account another tenant
   holds, internal or not, refuses it (``account_held_by_another_tenant``: one
   owner per account, ADR 0031). The outcome is recorded with
   ``cloudops.finish_credential_check``. A passed onboarding or rotation then
   starts the first sync: the credential's schedules are created and triggered.
2. **Reconciles workers.** Each active, unexpired credential whose secrets the
   store holds gets a Temporal worker on its own task queue
   (``connectors-<provider>-customer-<code>``), running the ordinary
   ``ConnectorIngest`` over its bound source, and one schedule per entity
   (``customer-<provider>-<code>-<entity>``). No cost entity runs for an account
   the platform's own feed reads (an approved alias of the provider in the same
   tenant, ADR 0023): one cost feed per subscription (ADR 0031). A credential
   revoked, expired or no longer mounted loses its worker and its schedules at once.

Nothing here writes to a customer's estate: every call the sources and the check
make is a read, and the roles they run with are checked to be read-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.service import RPCError
from temporalio.worker import Worker

from cloudops_config import SecretStore
from cloudops_connectors.aws import iam
from cloudops_connectors.core import identity
from cloudops_connectors.core.cli import upsert_schedule
from cloudops_connectors.core.ingest import (
    WORKFLOWS,
    ConnectorActivities,
    ConnectorIngest,
    IngestRequest,
    Source,
    activities,
)
from cloudops_connectors.core.raw import RawStore
from cloudops_connectors.customer import check, credentials, sources
from cloudops_connectors.customer.credentials import (
    AWS,
    AZURE,
    PROVIDERS,
    StoredCredential,
    StoredSecretCredential,
)
from cloudops_tenancy import Database, Role, TenantScope

log = logging.getLogger(__name__)

WHO: Final = "connector:customer"
SCHEDULE_PREFIX: Final = "customer-"
FIRST_SYNC_REASONS: Final = frozenset({"onboard", "rotation"})
_CLAIM: Final = text("SELECT id, tenant_id, credential_id, reason"
                     " FROM cloudops.claim_credential_check(:provider)")
_FINISH: Final = text("SELECT cloudops.finish_credential_check(:id, :status, :outcome,"
                      " CAST(:detail AS jsonb))")
# An approved alias of the provider is how the platform's own cost feed (ADR 0023)
# finds an account: under the credential's tenant, those accounts' cost is its.
_PLATFORM_FED: Final = text(
    "SELECT alias FROM cloudops.tenant_alias WHERE tenant_id = :tenant"
    " AND source_system = :provider AND alias = ANY(CAST(:ids AS text[]))"
)
# One owner per account (ADR 0031): another tenant's row fails the insert.
OWNED_ELSEWHERE: Final = "23505"
OWNER_INDEX: Final = "cloud_account_provider_native_key"


@dataclass(frozen=True)
class CheckReport:
    check_id: UUID
    credential_id: UUID
    provider: str
    reason: str
    status: str
    outcome: str
    detail: dict[str, Any]


Checker = Callable[[StoredCredential, AsyncExitStack], Any]  # -> Awaitable[check.Outcome]
Builder = Callable[[StoredCredential, AsyncExitStack], Source]


@dataclass
class _Running:
    credential: StoredCredential
    worker: Worker
    task: asyncio.Task[None]
    stack: AsyncExitStack


class CustomerWorker:
    def __init__(self, db: Database, raw: RawStore, temporal: Client | None,
                 store: SecretStore, *, providers: tuple[str, ...] = PROVIDERS,
                 checker: Checker | None = None, builder: Builder | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        """``temporal`` None: checks only, nothing started (``check`` on a laptop)."""
        self._db = db
        self._raw = raw
        self._temporal = temporal
        self._store = store
        self._providers = providers
        self._checker = checker or self._check
        self._builder = builder or (lambda c, stack: sources.build(store, c, stack))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._running: dict[UUID, _Running] = {}

    def _scope(self, tenant: UUID | None = None) -> TenantScope:
        return TenantScope(() if tenant is None else {tenant}, Role.INGEST, WHO)

    # --- checks ----------------------------------------------------------------------

    async def run_checks(self) -> list[CheckReport]:
        """Every waiting check, oldest first."""
        reports: list[CheckReport] = []
        for provider in self._providers:
            while (report := await self.check_one(provider)) is not None:
                reports.append(report)
        return reports

    async def check_one(self, provider: str) -> CheckReport | None:
        async with self._db.transaction(self._scope()) as conn:
            claimed = (await conn.execute(_CLAIM, {"provider": provider})).one_or_none()
        if claimed is None:
            return None
        row = tuple(claimed)
        check_id, tenant, credential_id, reason = (
            UUID(str(row[0])), UUID(str(row[1])), UUID(str(row[2])), str(row[3]))
        outcome = check.Outcome("failed", "internal_error")
        found: StoredCredential | None = None
        try:
            async with self._db.transaction(self._scope(tenant)) as conn:
                found = await credentials.one(conn, credential_id)
            outcome = await self._evaluate(found)
            if outcome.status == "passed" and found is not None:
                outcome = await self._bind(found, outcome)
                found = await self._fed(found)
        except Exception:  # recorded, never left running; the type only, no payload
            log.exception("credential check %s failed", check_id)
        finally:
            async with self._db.transaction(self._scope(tenant)) as conn:
                await conn.execute(_FINISH, {"id": check_id, "status": outcome.status,
                                             "outcome": outcome.outcome,
                                             "detail": _json(outcome.detail)})
        if (outcome.status == "passed" and reason in FIRST_SYNC_REASONS and found is not None
                and self._temporal is not None):
            await self._ensure(replace(found, status="active"), first_sync=True)
        return CheckReport(check_id, credential_id, provider, reason, outcome.status,
                           outcome.outcome, outcome.detail)

    async def _evaluate(self, found: StoredCredential | None) -> check.Outcome:
        if found is None:
            return check.Outcome("refused", "not_found")
        if found.status == "revoked":
            return check.Outcome("refused", "revoked")
        if found.expires_at <= self._clock():
            return check.Outcome("refused", "secret_expired")
        missing = found.missing_secrets(self._store)
        if missing:
            # A person creates it in Key Vault (and, in Azure, the pod mounts it), then asks again.
            return check.Outcome("failed", "secret_not_in_key_vault", {"secret_names": missing})
        async with AsyncExitStack() as stack:
            result: check.Outcome = await self._checker(found, stack)
        return result

    async def _check(self, credential: StoredCredential, stack: AsyncExitStack) -> check.Outcome:
        if credential.provider == AZURE:
            token = StoredSecretCredential(self._store, credential)
            stack.push_async_callback(token.close)
            client = sources.azure_client(credential, token)
            stack.push_async_callback(client.aclose)
            return await check.check_azure(credential, token, client)
        if credential.provider == AWS:
            sts = iam.QueryClient(iam.STS, iam.STS_VERSION,
                                  auth=sources.aws_signer(self._store, credential, "sts", iam.STS))
            stack.push_async_callback(sts.aclose)
            iam_client = iam.QueryClient(
                iam.IAM, iam.IAM_VERSION,
                auth=sources.aws_signer(self._store, credential, "iam", iam.IAM))
            stack.push_async_callback(iam_client.aclose)
            return await check.check_aws(credential, self._store, sts, iam_client)
        return check.Outcome("refused", "unknown_provider")

    async def _bind(self, credential: StoredCredential, outcome: check.Outcome) -> check.Outcome:
        """The credential's accounts, in its tenant (ADR 0016: created once bound).
        An account another tenant holds refuses the whole check: one owner each."""
        tenant = {a: credential.tenant_id for a in credential.account_ids}
        try:
            async with self._db.transaction(self._scope(credential.tenant_id)) as conn:
                await identity.upsert_accounts(conn, credential.provider, tenant, [
                    identity.AccountSeen(a, None) for a in credential.account_ids])
        except DBAPIError as exc:
            orig = exc.orig
            if (str(getattr(orig, "sqlstate", "")) == OWNED_ELSEWHERE
                    and getattr(getattr(orig, "diag", None), "constraint_name", None)
                    == OWNER_INDEX):
                return check.Outcome("refused", "account_held_by_another_tenant")
            raise
        fed = await self._fed(credential)
        return replace(outcome, detail={**outcome.detail,
                                        "cost_read_by_platform": sorted(fed.platform_fed)})

    async def _fed(self, credential: StoredCredential) -> StoredCredential:
        """The credential, knowing which of its accounts' cost the platform reads."""
        async with self._db.transaction(self._scope(credential.tenant_id)) as conn:
            fed = frozenset(str(r[0]) for r in await conn.execute(_PLATFORM_FED, {
                "tenant": credential.tenant_id, "provider": credential.provider,
                "ids": list(credential.account_ids)}))
        return replace(credential, platform_fed=fed)

    # --- workers and schedules -------------------------------------------------------

    async def reconcile(self) -> None:
        """Exactly the usable credentials have a worker and schedules."""
        async with self._db.transaction(self._scope()) as conn:
            wanted = {c.id: c for p in self._providers for c in await credentials.active(conn, p)}
        usable = {i: await self._fed(c) for i, c in wanted.items()
                  if not c.missing_secrets(self._store)}
        for i, running in list(self._running.items()):
            if i not in usable or running.task.done() \
                    or running.credential.account_ids != usable[i].account_ids \
                    or running.credential.platform_fed != usable[i].platform_fed:
                await self._stop(i)
        for c in usable.values():
            if c.id not in self._running:
                await self._ensure(c, first_sync=False)
        await self._drop_schedules(keep={sources.schedule_id(c, e) for c in usable.values()
                                         for e in sources.entities(c)})

    async def _ensure(self, credential: StoredCredential, *, first_sync: bool) -> None:
        temporal = self._temporal
        runs = sources.entities(credential)
        if temporal is None or not runs:
            return
        if credential.id not in self._running:
            stack = AsyncExitStack()
            source = self._builder(credential, stack)
            worker = Worker(temporal, task_queue=sources.queue(credential), workflows=WORKFLOWS,
                            activities=activities(ConnectorActivities(self._db, self._raw,
                                                                      source)))
            self._running[credential.id] = _Running(credential, worker,
                                                    asyncio.create_task(worker.run()), stack)
        since = (self._clock() - timedelta(days=90)).isoformat()
        for entity, every in runs.items():
            sid = sources.schedule_id(credential, entity)
            await upsert_schedule(temporal, sid, Schedule(
                action=ScheduleActionStartWorkflow(
                    ConnectorIngest.run, IngestRequest(entity, since), id=sid,
                    task_queue=sources.queue(credential)),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)])))
            if first_sync:
                await temporal.get_schedule_handle(sid).trigger()

    async def _stop(self, credential_id: UUID) -> None:
        running = self._running.pop(credential_id)
        if not running.task.done():
            await running.worker.shutdown()
        try:
            await running.task
        except Exception:  # a crashed worker is replaced on the next pass
            log.exception("worker for credential %s ended with an error", credential_id)
        await running.stack.aclose()

    async def _drop_schedules(self, keep: set[str]) -> None:
        temporal = self._temporal
        if temporal is None:
            return
        async for listed in await temporal.list_schedules():
            if listed.id.startswith(SCHEDULE_PREFIX) and listed.id not in keep:
                try:
                    await temporal.get_schedule_handle(listed.id).delete()
                except RPCError:
                    log.warning("could not delete schedule %s", listed.id)

    async def serve(self, interval: timedelta = timedelta(minutes=1)) -> None:
        try:
            while True:
                try:
                    await self.run_checks()
                    await self.reconcile()
                except (DBAPIError, RPCError, httpx.TransportError, OSError):
                    log.exception("customer worker pass failed; trying again")
                await asyncio.sleep(interval.total_seconds())
        finally:
            for i in list(self._running):
                await self._stop(i)


def _json(detail: dict[str, Any]) -> str:
    return json.dumps(detail, sort_keys=True, default=str)


def report(r: CheckReport) -> dict[str, Any]:
    return {k: str(v) if isinstance(v, UUID) else v for k, v in asdict(r).items()}
