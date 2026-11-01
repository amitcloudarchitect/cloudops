"""Dev only (ADR 0028): the real Qualys connector over the simulated Azure
Sponsorship estate, until dev has a Qualys subscription.

    python -m cloudops_devdata.simulation.qualys_live worker     # Azure profile, dev
    python -m cloudops_devdata.simulation.qualys_live schedule
    python -m cloudops_devdata.simulation.qualys_live run --fixtures   # the example estate

The estate is the VMs the platform already holds for the customer (``resource``
rows of type ``compute.vm`` under the tenant ``Azure Sponsorship`` resolves to),
never a file: the step ADR 0025 named after the portal export. The platform's
own VMs in the shared subscription (``{type}-cloudops-...``, the devbox among
them) are left out. From that estate the simulation (``model.build``, with a fixed
anchor and seed, so a host's findings do not move from one day to the next)
renders the Host List Detection and KnowledgeBase responses, and the connector's
own fake subscription serves them to the connector code that would read Qualys:
the same fetch, bronze, tag resolution, linking and upsert.

Guarded three ways: ``CLOUDOPS_QUALYS_SOURCE=simulation`` is refused by the
configuration unless the deployment is dev; this entry point refuses ``live``;
and every row it writes carries ``"simulated": true`` in ``raw_extra``, under the
source instance ``SIMULATION_HOST``. Nothing here reads a credential.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import AsyncExitStack
from datetime import date, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import httpx
from sqlalchemy import text

from cloudops_config import Component, ConfigurationError, DbLogin, QualysSource, Settings
from cloudops_connectors.core import state
from cloudops_connectors.core.cli import Live, main
from cloudops_connectors.core.ingest import principal
from cloudops_connectors.qualys.client import QualysClient
from cloudops_connectors.qualys.fake import FakeQualys
from cloudops_connectors.qualys.mapping import ENTITY, SOURCE
from cloudops_connectors.qualys.source import QualysSource as Connector
from cloudops_devdata.simulation import qualys
from cloudops_devdata.simulation.model import COMPANY, Estate, build, example_estate
from cloudops_devdata.simulation.rows import platform_rows
from cloudops_tenancy import Database, Role, TenantScope

SIMULATION_HOST: Final = "https://qualys.simulation.invalid"
ANCHOR: Final = date(2026, 10, 31)  # the day Azure Sponsorship was onboarded (ADR 0027)
SEED: Final = 11
# The platform's own resources follow {type}-cloudops-{workload}-{env}-{region} (LLD).
PLATFORM: Final = re.compile(r"^[a-z0-9]+-cloudops-", re.IGNORECASE)
_COLUMNS: Final = ("NAME", "SUBSCRIPTION", "TIME CREATED", "OPERATING SYSTEM", "RESOURCE GROUP",
                   "VIRTUAL NETWORK", "LOCATION", "STATUS", "SIZE", "PUBLIC IP ADDRESS", "DISKS",
                   "RESOURCE LINK")
_RG: Final = re.compile(r"/resourceGroups/([^/]+)/", re.IGNORECASE)
_VMS: Final = text(
    "SELECT r.name, r.native_id, r.operating_system, r.region, r.state, r.first_seen,"
    "       a.native_id, a.display_name"
    " FROM cloudops.resource r JOIN cloudops.cloud_account a ON a.id = r.account_id"
    " WHERE r.type_canonical = 'compute.vm' AND r.deleted_at IS NULL"
    "   AND a.provider = 'azure'"
    " ORDER BY a.native_id, r.name"
)


def estate_from_resources(rows: Iterable[Sequence[Any]]) -> Estate | None:
    """The customer's VMs as the portal export would list them, less the platform's
    own; None if there are none. One subscription: the first, if there are more."""
    vms = [r for r in rows if not PLATFORM.match(str(r[0]))
           and not ((m := _RG.search(str(r[1]))) and PLATFORM.match(m.group(1)))]
    if not vms:
        return None
    subscription, name = str(vms[0][6]).lower(), str(vms[0][7] or vms[0][6])
    out = []
    for vm_name, native, os_name, region, power, first_seen, sub, _ in vms:
        if str(sub).lower() != subscription:
            continue
        rg = m.group(1) if (m := _RG.search(str(native))) else "unknown"
        created = first_seen if isinstance(first_seen, datetime) else datetime.fromisoformat(
            str(first_seen))
        out.append(dict(zip(_COLUMNS, (
            str(vm_name), name, created.strftime("%Y-%m-%d %H:%M:%S"),
            "Windows" if "windows" in str(os_name or "").lower() else "Linux", rg,
            f"vnet-{rg}",  # not in the inventory; it only spreads the simulated addresses
            str(region or "westeurope"),
            "Running" if "running" in str(power or "running").lower() else "Stopped (deallocated)",
            "Standard_D2s_v5", "-", "1", f"https://portal.azure.com#resource{native}"),
            strict=True)))
    return Estate(subscription, name, tuple(out))


def responses(estate: Estate) -> tuple[bytes, bytes]:
    """The detection and KnowledgeBase responses for an estate."""
    sim = build(ANCHOR, SEED, estate)
    return qualys.detections(sim, platform_rows(sim, UUID(int=0))), qualys.knowledge_base()


async def load_estate(db: Database) -> Estate | None:
    """The VMs of the tenant ``Azure Sponsorship`` resolves to, read under its scope."""
    async with db.transaction(TenantScope((), Role.INGEST, principal(SOURCE))) as conn:
        tenant = (await state.resolve_aliases(conn, SOURCE, [COMPANY]))[COMPANY].tenant_id
    if tenant is None:
        return None
    async with db.transaction(TenantScope([tenant], Role.INGEST, principal(SOURCE))) as conn:
        return estate_from_resources([tuple(r) for r in await conn.execute(_VMS)])


_EMPTY: Final = (b"<HOST_LIST_VM_DETECTION_OUTPUT><RESPONSE><HOST_LIST/></RESPONSE>"
                 b"</HOST_LIST_VM_DETECTION_OUTPUT>")


class _Simulated(httpx.AsyncBaseTransport):
    """The fake subscription, built from the estate on the first request."""

    def __init__(self, load: Callable[[], Awaitable[Estate | None]]) -> None:
        self._load = load
        self._lock = asyncio.Lock()
        self._fake: FakeQualys | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async with self._lock:
            if self._fake is None:
                estate = await self._load()
                detections, kb = responses(estate) if estate else (_EMPTY,
                                                                   qualys.knowledge_base())
                self._fake = FakeQualys(detections, kb)
        return await self._fake.transport().handle_async_request(request)


def _fixtures() -> Connector:
    detections, kb = responses(example_estate())
    return Connector(QualysClient(SIMULATION_HOST, transport=FakeQualys(detections, kb)
                                  .transport()), simulated=True)


def live_source(settings: Settings, stack: AsyncExitStack) -> Connector:
    if settings.qualys.source is not QualysSource.SIMULATION:
        raise ConfigurationError(
            "this entry point serves CLOUDOPS_QUALYS_SOURCE=simulation only; live Qualys is "
            "python -m cloudops_connectors.qualys")
    db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=1)
    stack.push_async_callback(db.dispose)
    client = QualysClient(SIMULATION_HOST, transport=_Simulated(lambda: load_estate(db)))
    stack.push_async_callback(client.aclose)
    return Connector(client, simulated=True)


LIVE: Final = Live(frozenset({Component.QUALYS}), lambda _: frozenset({ENTITY}), live_source,
                   schedule_requires=frozenset({Component.QUALYS}))


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {ENTITY: timedelta(days=1)}, _fixtures, live=LIVE))
