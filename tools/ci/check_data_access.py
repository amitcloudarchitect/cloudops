"""CI gate: no data access path may exist outside packages/tenancy.

Fails (exit 1) when any file outside ``packages/tenancy/``:

- imports a PostgreSQL or telemetry-store driver, or the SQLAlchemy modules
  that create engines, pools, sessions or raw connections;
- names an engine factory (``create_engine``, ``create_async_engine``, ...);
- imports a module dynamically, which this check cannot verify;
- mentions the scope settings ``app.tenant_ids`` / ``app.role`` /
  ``app.account_ids`` or
  ``set_config`` in a string — scope is set by the tenancy package alone;
- in TypeScript/JavaScript, imports a database client at all — the web tier
  never talks to the database;
- declares a database driver as a dependency in pyproject.toml or package.json.

SQLAlchemy Core *expressions* (``select``, ``Table``, ``text`` ...) remain
allowed everywhere: building a query is fine, executing it needs a
``ScopedConnection`` from the tenancy package.

Exceptions go in ``data_access_allowlist.txt`` with a justification, and are
reviewed like any other change to this gate. An entry ending in ``/`` covers a
directory.

Usage: python tools/ci/check_data_access.py [repo_root]
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

TENANCY_DIR = "packages/tenancy"

# This file and its tests necessarily spell out what they forbid.
SELF_EXEMPT = frozenset(
    {
        "tools/ci/check_data_access.py",
        "tools/ci/tests/test_check_data_access.py",
    }
)

SKIP_DIRS = frozenset(
    {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
        ".ruff_cache", ".pytest_cache", ".next", "dist", "build", ".terraform",
    }
)

BANNED_PY_MODULES = (
    "psycopg", "psycopg2", "psycopg_pool", "psycopg_binary", "psycopg_c",
    "asyncpg", "pg8000", "aiopg", "databases", "sqlmodel", "records",
    "sqlalchemy.engine", "sqlalchemy.ext.asyncio", "sqlalchemy.pool",
    "sqlalchemy.orm", "sqlalchemy.connectors",
    "sqlalchemy.dialects.postgresql.psycopg", "sqlalchemy.dialects.postgresql.psycopg2",
    "sqlalchemy.dialects.postgresql.asyncpg", "sqlalchemy.dialects.postgresql.pg8000",
    "sqlalchemy.dialects.postgresql.psycopg2cffi",
    # Telemetry store: isolation there is by query construction, so raw
    # clients are banned exactly like database drivers (LLD, Telemetry store).
    "clickhouse_connect", "clickhouse_driver", "asynch", "aiochclient", "azure.kusto",
)

BANNED_NAMES = frozenset(
    {
        "create_engine", "create_async_engine", "engine_from_config",
        "async_engine_from_config", "create_mock_engine", "create_pool_from_url",
    }
)

DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__"})

SCOPE_STRING = re.compile(
    r"\bapp\.(tenant_ids|role|account_ids)\b|\bset_config\b", re.IGNORECASE
)

BANNED_JS_PACKAGES = (
    "pg", "pg-pool", "pg-promise", "postgres", "@neondatabase/serverless", "@vercel/postgres",
    "knex", "kysely", "drizzle-orm", "@prisma/client", "prisma", "typeorm", "sequelize",
    "slonik", "mikro-orm", "@mikro-orm/core", "@clickhouse/client", "azure-kusto-data",
)
_JS_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"})
_JS_SPEC = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"]([^'"]+)['"]"""
)

BANNED_PY_DISTRIBUTIONS = frozenset(
    {
        "psycopg", "psycopg2", "psycopg2-binary", "psycopg-binary", "psycopg-pool",
        "asyncpg", "pg8000", "aiopg", "databases", "sqlmodel", "records",
        "clickhouse-connect", "clickhouse-driver", "asynch", "aiochclient", "azure-kusto-data",
    }
)


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _is_banned_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in BANNED_PY_MODULES)


def _is_banned_js(spec: str) -> bool:
    return any(spec == p or spec.startswith(p + "/") for p in BANNED_JS_PACKAGES)


def check_python_source(rel: str, source: str) -> list[Violation]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return [Violation(rel, exc.lineno or 0, "cannot parse; the gate fails closed")]
    out: list[Violation] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_banned_module(alias.name):
                    out.append(Violation(rel, line, f"imports {alias.name}"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if _is_banned_module(node.module):
                out.append(Violation(rel, line, f"imports from {node.module}"))
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if alias.name == "*" and node.module.split(".")[0] == "sqlalchemy":
                    out.append(Violation(rel, line, f"star import from {node.module}"))
                elif _is_banned_module(full) or alias.name in BANNED_NAMES:
                    out.append(Violation(rel, line, f"imports {full}"))
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
            out.append(Violation(rel, line, f"references {node.attr}"))
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            out.append(Violation(rel, line, f"references {node.id}"))
        elif isinstance(node, ast.Call):
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if fname in DYNAMIC_IMPORTERS:
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if _is_banned_module(arg.value):
                        out.append(Violation(rel, line, f"dynamically imports {arg.value}"))
                else:
                    out.append(Violation(rel, line, "dynamic import with a non-literal name"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            m = SCOPE_STRING.search(node.value)
            if m:
                out.append(Violation(rel, line, f"mentions {m.group(0)}; scope is tenancy's job"))
    return out


def check_js_source(rel: str, source: str) -> list[Violation]:
    out: list[Violation] = []
    for lineno, text in enumerate(source.splitlines(), start=1):
        for spec in _JS_SPEC.findall(text):
            if _is_banned_js(spec):
                out.append(Violation(rel, lineno, f"imports database client {spec}"))
    return out


def _requirement_name(req: str) -> str:
    return re.split(r"[\s\[<>=!~;@]", req.strip(), maxsplit=1)[0].lower().replace("_", "-")


def check_pyproject(rel: str, source: str) -> list[Violation]:
    data = tomllib.loads(source)
    reqs: list[str] = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        reqs.extend(group)
    for group in data.get("dependency-groups", {}).values():
        reqs.extend(r for r in group if isinstance(r, str))
    return [
        Violation(rel, 0, f"declares database driver dependency {_requirement_name(r)}")
        for r in reqs
        if _requirement_name(r) in BANNED_PY_DISTRIBUTIONS
    ]


def check_package_json(rel: str, source: str) -> list[Violation]:
    data = json.loads(source)
    out: list[Violation] = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name in data.get(section, {}):
            if _is_banned_js(name):
                out.append(Violation(rel, 0, f"declares database client dependency {name}"))
    return out


def load_allowlist(root: Path) -> frozenset[str]:
    path = root / "tools/ci/data_access_allowlist.txt"
    if not path.exists():
        return frozenset()
    entries: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry, sep, reason = line.partition("#")
        if not sep or not reason.strip():
            raise SystemExit(f"allowlist entry without a justification: {line!r}")
        entries.add(entry.strip())
    return frozenset(entries)


def _allowed(rel: str, allow: frozenset[str]) -> bool:
    """Exact paths, or directories given with a trailing slash."""
    return rel in allow or any(e.endswith("/") and rel.startswith(e) for e in allow)


def _walk(root: Path) -> Iterator[Path]:
    for path in root.iterdir():
        if path.is_dir():
            if path.name not in SKIP_DIRS:
                yield from _walk(path)
        else:
            yield path


def check_repo(root: Path) -> list[Violation]:
    allow = load_allowlist(root)
    out: list[Violation] = []
    for path in _walk(root):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(TENANCY_DIR + "/") or rel in SELF_EXEMPT or _allowed(rel, allow):
            continue
        out.extend(check_file(rel, path))
    return sorted(out)


def check_file(rel: str, path: Path) -> Iterable[Violation]:
    suffix = path.suffix
    if suffix not in {".py", ".pyi", ".toml", ".json"} | _JS_EXTS:
        return []
    source = path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".py", ".pyi"}:
        return check_python_source(rel, source)
    if suffix in _JS_EXTS:
        return check_js_source(rel, source)
    if path.name == "pyproject.toml":
        return check_pyproject(rel, source)
    if path.name == "package.json":
        return check_package_json(rel, source)
    return []


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    violations = check_repo(root)
    for v in violations:
        print(v)
    if violations:
        print(
            f"\n{len(violations)} data-access violation(s). All database access goes through "
            "packages/tenancy (CLAUDE.md rule 1).",
            file=sys.stderr,
        )
        return 1
    print("data-access boundary: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
