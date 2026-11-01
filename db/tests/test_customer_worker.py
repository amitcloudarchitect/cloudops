"""The customer-credential worker's checks against the real schema (ADR 0027):
it claims a check with no scope, reads the credential under its tenant, refuses
an expired one, fails one whose secret is not in the store (for a person to fix),
binds a passed credential's accounts to its tenant, refuses an account another
customer holds, and records what is shared with the platform. The cloud check
itself is stubbed here; ``connectors/tests/test_customer_contract.py`` covers it."""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_config import EnvFileSecretStore
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.customer import check
from cloudops_connectors.customer.credentials import StoredCredential
from cloudops_connectors.customer.worker import CustomerWorker
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Principals, Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


def _tenant(seeded: Seeded, code: str, *, internal: bool = False) -> UUID:
    t = uuid4()
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        if internal:
            cx.execute("UPDATE cloudops.tenant SET internal = false WHERE internal")
        cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code, internal)"
                   " VALUES (%s, %s, %s, %s)", (t, f"Worker {code} {t}", code, internal))
    return t


async def _onboard(app_db: Database, admin: UUID, tenant: UUID, sub: str) -> UUID:
    async with app_db.transaction(TenantScope([tenant], Role.PLATFORM_ADMIN, "test")) as conn:
        return UUID(str(await conn.scalar(text(
            "SELECT access.onboard_cloud_credential(:a, :t, 'azure', :d, :c, NULL,"
            " CAST(:ids AS text[]), :e)"),
            {"a": admin, "t": tenant, "d": uuid4(), "c": uuid4(), "ids": [sub],
             "e": datetime.now(UTC) + timedelta(days=60)})))


def _store(tmp_path: Path, *codes: str) -> EnvFileSecretStore:
    path = tmp_path / ".env.local"
    path.write_text("".join(f"CUSTOMER_{c.upper()}_AZURE_SECRET=s\n" for c in codes),
                    encoding="utf-8")
    return EnvFileSecretStore.from_file(path)


async def _passes(_: StoredCredential, __: AsyncExitStack) -> check.Outcome:
    return check.Outcome("passed", "read_only", {"subscriptions": 1})


async def _run_until(worker: CustomerWorker, credential: UUID) -> Any:
    for _ in range(50):
        report = await worker.check_one("azure")
        if report is None:
            break
        if report.credential_id == credential:
            return report
    raise AssertionError("the check was never run")


def _status(seeded: Seeded, credential: UUID) -> tuple[Any, ...]:
    with psycopg.connect(seeded.admin_dsn) as cx:
        row = cx.execute("SELECT k.status, c.status, c.outcome, c.detail FROM"
                          " cloudops.customer_credential k JOIN cloudops.credential_check c"
                          " ON c.credential_id = k.id WHERE k.id = %s"
                          " ORDER BY c.requested_at DESC LIMIT 1", (credential,)).fetchone()
    assert row is not None
    return tuple(row)


async def test_a_passed_check_binds_the_accounts_to_the_tenant(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
    tmp_path: Path,
) -> None:
    t, sub = _tenant(seeded, "WKPASS"), str(uuid4())
    credential = await _onboard(app_db, principals.admin, t, sub)
    worker = CustomerWorker(ingest_db, MemoryRawStore(), None, _store(tmp_path, "wkpass"),
                            providers=("azure",), checker=_passes)
    report = await _run_until(worker, credential)
    assert (report.status, report.outcome) == ("passed", "read_only")
    assert report.detail["shared_with_platform"] == []
    assert _status(seeded, credential)[:3] == ("active", "passed", "read_only")
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT tenant_id FROM cloudops.cloud_account WHERE native_id = %s",
                          (sub,)).fetchall() == [(t,)]


async def test_a_missing_secret_fails_the_check_for_a_person_to_fix(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
    tmp_path: Path,
) -> None:
    t = _tenant(seeded, "WKMISS")
    credential = await _onboard(app_db, principals.admin, t, str(uuid4()))
    called: list[Any] = []

    async def never(c: StoredCredential, _: AsyncExitStack) -> check.Outcome:
        called.append(c)
        return check.Outcome("passed", "read_only")

    worker = CustomerWorker(ingest_db, MemoryRawStore(), None, _store(tmp_path),
                            providers=("azure",), checker=never)
    report = await _run_until(worker, credential)
    assert (report.status, report.outcome) == ("failed", "secret_not_in_key_vault")
    assert report.detail == {"secret_names": ["customer-wkmiss-azure-secret"]}
    assert called == []
    assert _status(seeded, credential)[:2] == ("pending", "failed")


async def test_an_expired_credential_is_refused_without_a_cloud_call(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
    tmp_path: Path,
) -> None:
    t = _tenant(seeded, "WKEXP")
    credential = await _onboard(app_db, principals.admin, t, str(uuid4()))
    worker = CustomerWorker(ingest_db, MemoryRawStore(), None, _store(tmp_path, "wkexp"),
                            providers=("azure",), checker=_passes,
                            clock=lambda: datetime.now(UTC) + timedelta(days=61))
    report = await _run_until(worker, credential)
    assert (report.status, report.outcome) == ("refused", "secret_expired")
    assert _status(seeded, credential)[0] == "refused"


async def test_another_customers_account_refuses_the_credential(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
    tmp_path: Path,
) -> None:
    t, held = _tenant(seeded, "WKOWN"), str(uuid4())
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:  # another customer's
        cx.execute("INSERT INTO cloudops.cloud_account (tenant_id, provider, native_id,"
                   " display_name) VALUES (%s, 'azure', %s, 'b')", (seeded.b.id, held))
    credential = await _onboard(app_db, principals.admin, t, held)
    worker = CustomerWorker(ingest_db, MemoryRawStore(), None, _store(tmp_path, "wkown"),
                            providers=("azure",), checker=_passes)
    report = await _run_until(worker, credential)
    assert (report.status, report.outcome) == ("refused", "account_held_by_another_tenant")
    assert _status(seeded, credential)[0] == "refused"
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT count(*) FROM cloudops.cloud_account WHERE tenant_id = %s",
                          (t,)).fetchone() == (0,)


async def test_the_platforms_own_subscription_is_bound_and_marked_shared(
    app_db: Database, ingest_db: Database, seeded: Seeded, principals: Principals,
    tmp_path: Path,
) -> None:
    internal, customer, sub = _tenant(seeded, "WKGTX", internal=True), _tenant(
        seeded, "WKSHARE"), str(uuid4())
    async with ingest_db.transaction(TenantScope([internal], Role.INGEST, "test")) as conn:
        await conn.execute(text("INSERT INTO cloudops.cloud_account (tenant_id, provider,"
                                " native_id, display_name) VALUES (:t, 'azure', :n, 'x')"),
                           {"t": internal, "n": sub})
    try:
        credential = await _onboard(app_db, principals.admin, customer, sub)
        worker = CustomerWorker(ingest_db, MemoryRawStore(), None,
                                _store(tmp_path, "wkshare"), providers=("azure",),
                                checker=_passes)
        report = await _run_until(worker, credential)
        assert (report.status, report.detail["shared_with_platform"]) == ("passed", [sub])
    finally:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("DELETE FROM cloudops.cloud_account WHERE native_id = %s", (sub,))
            cx.execute("UPDATE cloudops.tenant SET internal = false WHERE id = %s", (internal,))
