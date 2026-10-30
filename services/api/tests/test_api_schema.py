"""The committed OpenAPI schema is the API's (ADR 0014): the console's types are
generated from ``packages/contracts/openapi.json``, so a stale file means the
console is compiled against an API that no longer exists."""

from __future__ import annotations

from pathlib import Path

from cloudops_api.schema import render

COMMITTED = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "openapi.json"


def test_the_committed_schema_matches_the_api() -> None:
    assert COMMITTED.read_text(encoding="utf-8") == render(), (
        "regenerate: uv run python -m cloudops_api.schema > packages/contracts/openapi.json"
        " && npm run generate -w @cloudops/contracts"
    )
