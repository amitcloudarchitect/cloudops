"""The simulation's platform side (docs/simulation) against a migrated database.

The base synthetic seed first, as ``make seed`` leaves it; then the simulation
finds no ``AZSPONSOR`` tenant, onboards one through ``access.onboard_tenant``,
loads its rows through ``packages/tenancy`` and reads them back by scope. Its
findings then reach the platform the way they will for real: through the step
15 Qualys connector, served the simulation's recorded responses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.core.ingest import ConnectorActivities, ConnectorIngest, IngestRequest
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.qualys import mapping
from cloudops_connectors.qualys.client import QualysClient
from cloudops_connectors.qualys.fake import FakeQualys
from cloudops_connectors.qualys.source import QualysSource
from cloudops_devdata import seed
from cloudops_devdata.simulation import load as loader
from cloudops_devdata.simulation import qualys
from cloudops_devdata.simulation.model import (
    COMPANY,
    SHORT_CODE,
    Simulation,
    build,
    example_estate,
)
from cloudops_devdata.simulation.rows import PlatformRows, platform_rows
from cloudops_devdata.synthetic import generate
from cloudops_tenancy import Database, TenantScope

from .conftest import _database
from .connectors import SINCE, no_sleep, rows, run

pytestmark = [pytest.mark.integration]

ANCHOR = date(2026, 10, 30)


class Loaded:
    def __init__(self, admin_dsn: str, ingest: Database, app: Database, sim: Simulation,
                 tenant: UUID, data: PlatformRows, other: UUID) -> None:
        self.admin_dsn, self.ingest, self.app = admin_dsn, ingest, app
        self.sim, self.tenant, self.data, self.other = sim, tenant, data, other


@pytest.fixture(scope="module")
async def loaded(admin_server: str) -> AsyncIterator[Loaded]:
    with _database(admin_server, "sim") as db:
        ingest = Database.create(db.ingest_dsn, pool_size=2, max_overflow=0)
        app = Database.create(db.app_dsn, pool_size=2, max_overflow=0)
        try:
            base = generate(ANCHOR, 7)
            await seed.load(ingest, base)
            admin = uuid4()
            async with app.transaction(TenantScope.for_resolution("test:sim")) as conn:
                await conn.execute(text("SELECT access.bootstrap_admin(:o, 'Test admin')"),
                                   {"o": admin})
            assert await loader.find_tenant(ingest) is None
            tenant = await loader.onboard(app, ingest, admin)
            sim = build(ANCHOR, 11, example_estate())
            data = await loader.load(ingest, sim, tenant)
            yield Loaded(db.admin_dsn, ingest, app, sim, tenant, data, base.tenants[0].id)
        finally:
            await ingest.dispose()
            await app.dispose()


async def test_the_tenant_is_found_by_its_short_code_not_a_fixed_id(loaded: Loaded) -> None:
    assert await loader.find_tenant(loaded.ingest) == loaded.tenant
    assert loaded.tenant != platform_rows(loaded.sim, uuid4()).tenant_id
    (name, code, location), = rows(loaded.admin_dsn, "SELECT canonical_name, short_code,"
                                   " delivery_location FROM cloudops.tenant WHERE id = %s",
                                   loaded.tenant)
    assert (name, code, location) == (COMPANY, SHORT_CODE, "India")


async def test_every_row_is_in_the_tenant_and_only_its_scope_sees_it(loaded: Loaded) -> None:
    await loader.verify(loaded.app, loaded.tenant, loaded.data, loaded.other)
    for table, expected in loaded.data.by_table().items():
        (n,), = rows(loaded.admin_dsn, f"SELECT count(*) FROM {table} WHERE tenant_id = %s",
                     loaded.tenant)
        assert n == len(expected), table
    (aliases,), = rows(loaded.admin_dsn, "SELECT count(*) FROM cloudops.tenant_alias"
                       " WHERE tenant_id = %s AND alias = %s", loaded.tenant, COMPANY)
    assert aliases == 2, "servicenow and qualys"


async def test_money_is_stored_with_its_currency(loaded: Loaded) -> None:
    sow = rows(loaded.admin_dsn, "SELECT currency, monthly_manage_revenue FROM cloudops.sow"
                                 " WHERE tenant_id = %s", loaded.tenant)
    assert [c for c, _ in sow] == ["EUR"]
    currencies = rows(loaded.admin_dsn, "SELECT DISTINCT currency FROM cloudops.cost_fact"
                                        " WHERE tenant_id = %s", loaded.tenant)
    assert currencies == [("USD",)]


async def test_a_second_load_is_refused(loaded: Loaded) -> None:
    with pytest.raises(seed.SeedError, match="already loaded"):
        await loader.load(loaded.ingest, loaded.sim, loaded.tenant)


async def test_the_qualys_connector_links_every_finding_to_its_vm(loaded: Loaded) -> None:
    detections = qualys.detections(loaded.sim, loaded.data)
    fake = FakeQualys(detections, qualys.knowledge_base())
    source = QualysSource(QualysClient(f"https://t{uuid4().hex[:8]}.qualysapi.example",
                                       transport=fake.transport(), sleep=no_sleep))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        (report,) = await run(env, ConnectorActivities(loaded.ingest, MemoryRawStore(), source),
                              ConnectorIngest.run, [IngestRequest(mapping.ENTITY, SINCE)])
    assert report.status == "succeeded"
    expected = len(loaded.data.vulnerabilities)
    assert (report.counts.written, report.counts.quarantined) == (expected, 0)
    stored = rows(loaded.admin_dsn, "SELECT r.name, v.asset_ipv4 FROM cloudops.vulnerability v"
                                    " JOIN cloudops.resource r ON r.id = v.resource_id"
                                    " WHERE v.tenant_id = %s", loaded.tenant)
    assert len(stored) == expected, "every finding linked to its VM"
    ips = {v.name: v.private_ip for v in loaded.sim.vms}
    assert all(str(ip) == ips[name] for name, ip in stored)
    assert rows(loaded.admin_dsn, "SELECT count(*) FROM staging.resource_quarantine"
                                  " WHERE source_system = 'qualys' AND tenant_id = %s",
                loaded.tenant) == [(0,)]
