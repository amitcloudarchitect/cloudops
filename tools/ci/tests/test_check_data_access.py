"""Tests for the data-access CI gate, including a run over the real repository."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_data_access", REPO_ROOT / "tools/ci/check_data_access.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def test_repository_has_no_data_access_outside_tenancy() -> None:
    violations = checker.check_repo(REPO_ROOT)
    assert violations == [], "\n".join(str(v) for v in violations)


@pytest.mark.parametrize(
    "source",
    [
        "import psycopg",
        "import psycopg.rows",
        "import psycopg2 as pg",
        "import asyncpg",
        "from psycopg import AsyncConnection",
        "from sqlalchemy import create_engine",
        "from sqlalchemy.ext.asyncio import create_async_engine",
        "from sqlalchemy.ext import asyncio",
        "from sqlalchemy.engine import Engine",
        "from sqlalchemy.orm import Session",
        "from sqlalchemy.pool import NullPool",
        "from sqlalchemy import *",
        "import sqlalchemy as sa\nsa.create_engine('postgresql://')",
        "import sqlalchemy.ext.asyncio",
        "import clickhouse_connect",
        "from azure.kusto.data import KustoClient",
        "import importlib\nimportlib.import_module('psycopg')",
        "import importlib\nname = 'x'\nimportlib.import_module(name)",
        "__import__('asyncpg')",
        "q = \"SELECT set_config('app.tenant_ids', '', false)\"",
        "q = 'SET app.role = cfo'",
        "q = \"SELECT current_setting('app.account_ids')\"",
        # Telemetry: no RLS there, so a hand-built query is a bypass.
        "q = 'SELECT value FROM telemetry.metric_sample'",
        "q = 'SELECT avgMerge(avg) FROM metric_5m'",
        "q = 'INSERT INTO x FORMAT JSONEachRow'",
        "h = {'X-ClickHouse-User': 'cloudops_telemetry'}",
    ],
)
def test_python_violations_are_detected(source: str) -> None:
    assert checker.check_python_source("services/api/x.py", source)


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy import select, Table, Column, MetaData, text",
        "from sqlalchemy.dialects.postgresql import UUID, JSONB, insert",
        "from cloudops_tenancy import Database, TenantScope, Role",
        "import requests\ns = requests.Session()",
        "x = 'application role'",
        "x = 'the telemetry store (ClickHouse or ADX) is undecided'",
        "metric = 'cpu.utilisation'",
    ],
)
def test_allowed_python_is_not_flagged(source: str) -> None:
    assert checker.check_python_source("services/api/x.py", source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import { Pool } from 'pg';",
        'import postgres from "postgres";',
        "const { Client } = require('pg');",
        "const db = await import('@prisma/client');",
        "import { drizzle } from 'drizzle-orm/node-postgres';",
        "export { sql } from 'kysely';",
    ],
)
def test_js_database_clients_are_detected(source: str) -> None:
    assert checker.check_js_source("apps/web/lib/db.ts", source)


def test_js_lookalike_packages_are_not_flagged() -> None:
    assert checker.check_js_source("apps/web/x.ts", "import pgp from 'pg-format-utils';") == []


def test_driver_dependencies_are_detected() -> None:
    pyproject = '[project]\nname = "x"\ndependencies = ["fastapi", "asyncpg>=0.29"]\n'
    package_json = '{"dependencies": {"next": "15.0.0", "pg": "8.13.0"}}'
    assert checker.check_pyproject("services/api/pyproject.toml", pyproject)
    assert checker.check_package_json("apps/web/package.json", package_json)


def test_unparseable_python_fails_closed() -> None:
    assert checker.check_python_source("services/api/x.py", "def (:")


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_repo_scan_exempts_tenancy_and_flags_everything_else(tmp_path: Path) -> None:
    _write(tmp_path, "packages/tenancy/src/cloudops_tenancy/database.py", "import psycopg\n")
    _write(tmp_path, "services/domain/repo.py", "import psycopg\n")
    _write(tmp_path, "connectors/snow/client.py", "from sqlalchemy import create_engine\n")
    _write(tmp_path, ".venv/lib/site.py", "import psycopg\n")

    found = {v.path for v in checker.check_repo(tmp_path)}

    assert found == {"services/domain/repo.py", "connectors/snow/client.py"}


def test_allowlist_requires_justification(tmp_path: Path) -> None:
    _write(tmp_path, "db/migrations/env.py", "from sqlalchemy import create_engine\n")
    _write(tmp_path, "tools/ci/data_access_allowlist.txt", "db/migrations/env.py\n")
    with pytest.raises(SystemExit):
        checker.check_repo(tmp_path)

    _write(
        tmp_path,
        "tools/ci/data_access_allowlist.txt",
        "db/migrations/env.py  # migration role, runs DDL as owner\n",
    )
    assert checker.check_repo(tmp_path) == []


def test_allowlist_directory_entries_cover_only_that_directory(tmp_path: Path) -> None:
    _write(tmp_path, "db/migrations/versions/0001.py", "import psycopg\n")
    _write(tmp_path, "db/migrations_extra.py", "import psycopg\n")
    _write(tmp_path, "tools/ci/data_access_allowlist.txt", "db/migrations/  # owner role\n")
    assert {v.path for v in checker.check_repo(tmp_path)} == {"db/migrations_extra.py"}


def test_main_exit_code(tmp_path: Path) -> None:
    _write(tmp_path, "services/api/ok.py", "x = 1\n")
    assert checker.main(["check", str(tmp_path)]) == 0
    _write(tmp_path, "services/api/bad.py", "import asyncpg\n")
    assert checker.main(["check", str(tmp_path)]) == 1
