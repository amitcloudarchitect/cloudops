"""Apply the telemetry column store's schema (``db/telemetry/migrations``).

    python db/telemetry/migrate.py --url http://127.0.0.1:8123 --user cloudops_admin \
        --password-file <file>

Run by the pipeline as the store's administrator, never by an application, like
the PostgreSQL migrations. Files apply in name order, each statement once; the
applied versions are recorded in ``telemetry.schema_version``. Statements are
written ``IF NOT EXISTS``, so a partly applied file can be applied again.

The application's ClickHouse user (``cloudops_telemetry``) gets SELECT and
INSERT only, and reaches the tables only through ``cloudops_tenancy.telemetry``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_BOOKKEEPING = (
    "CREATE TABLE IF NOT EXISTS telemetry.schema_version"
    " (version String, applied_at DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY version"
)


def statements(sql: str) -> list[str]:
    """Split a file on semicolons that end a line; comments dropped."""
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";\n") if s.strip().rstrip(";")]


def migrate(client: httpx.Client) -> list[str]:
    def run(query: str) -> str:
        response = client.post("/", content=query.encode())
        response.raise_for_status()
        return response.text

    run("CREATE DATABASE IF NOT EXISTS telemetry")
    run(_BOOKKEEPING)
    done = set(run("SELECT version FROM telemetry.schema_version FORMAT TabSeparated").split())
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.stem in done:
            continue
        for statement in statements(path.read_text(encoding="utf-8")):
            run(statement.rstrip(";"))
        run(f"INSERT INTO telemetry.schema_version (version) VALUES ('{path.stem}')")  # noqa: S608 - a file name of ours
        applied.append(path.stem)
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="db/telemetry/migrate.py")
    parser.add_argument("--url", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password-file", required=True, type=Path)
    args = parser.parse_args(argv)
    password = args.password_file.read_text(encoding="utf-8").strip()
    headers = {"X-ClickHouse-User": args.user, "X-ClickHouse-Key": password}
    with httpx.Client(base_url=args.url, headers=headers, timeout=60.0) as client:
        for version in migrate(client):
            print(f"applied {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
