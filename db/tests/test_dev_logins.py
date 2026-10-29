"""scripts/db_dev_logins.py: the SCRAM verifier it sends really authenticates the
password it never sends, and its generated SQL refuses anything but identifiers
and verifiers (ADR 0008)."""

from __future__ import annotations

import importlib.util
import secrets
import sys
from pathlib import Path
from types import ModuleType

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "db_dev_logins.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("db_dev_logins", SCRIPT)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


logins = _load()


def test_a_verifier_authenticates_the_password_it_was_made_from(admin_server: str) -> None:
    role = f"scram_probe_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(32)
    sql = logins.password_sql(role, logins.scram_verifier(password))
    assert password not in sql
    with psycopg.connect(admin_server, autocommit=True) as cx:
        cx.execute(f'CREATE ROLE "{role}" LOGIN')
        cx.execute(sql.split("\n", 1)[1])  # drop the psql-only \set line
    def as_role(pw: str) -> str:
        return make_conninfo(admin_server, user=role, password=pw, dbname="postgres")

    try:
        with psycopg.connect(as_role(password)) as cx:
            assert cx.execute("SELECT current_user").fetchone() == (role,)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(as_role(password + "x"))
    finally:
        with psycopg.connect(admin_server, autocommit=True) as cx:
            cx.execute(f'DROP ROLE "{role}"')


def test_verifier_format_matches_postgres() -> None:
    v = logins.scram_verifier("pw", salt=b"\x00" * 16)
    assert v.startswith("SCRAM-SHA-256$4096:AAAAAAAAAAAAAAAAAAAAAA==$")
    assert logins.scram_verifier("pw") != logins.scram_verifier("pw")  # fresh salt
    with pytest.raises(ValueError, match="ASCII"):
        logins.scram_verifier("pässword")


@pytest.mark.parametrize("bad", ['x"; DROP ROLE app; --', "Upper", "a b", ""])
def test_identifiers_are_never_interpolated_unchecked(bad: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        logins.quote_ident(bad)
    with pytest.raises(ValueError, match="identifier"):
        logins.bootstrap_sql(bad)


def test_only_a_verifier_is_accepted_as_a_password() -> None:
    with pytest.raises(ValueError, match="SCRAM"):
        logins.password_sql("temporal", "plaintext-password")
    with pytest.raises(ValueError, match="SCRAM"):
        logins.password_sql("temporal", "SCRAM-SHA-256$4096:a'; DROP ROLE x; --")


def test_logins_join_their_group_and_cannot_bypass_rls() -> None:
    sql = logins.bootstrap_sql("id-cloudops-migrate-dev")
    assert 'GRANT "cloudops_app" TO "cloudops_app_azure";' in sql
    assert 'GRANT "cloudops_ingest" TO "cloudops_ingest_azure";' in sql
    assert sql.count("NOSUPERUSER NOBYPASSRLS") == len(logins.LOGINS)
    assert "CREATE DATABASE \"cloudops\" OWNER \"id-cloudops-migrate-dev\"" in sql
    assert "dsn" not in sql.lower()


def test_dsn_verifies_the_server_certificate() -> None:
    value = logins.dsn("cloudops_app_azure", "pw", "h.postgres.database.azure.com", "cloudops")
    assert "sslmode=verify-full" in value
    assert "sslrootcert=system" in value
