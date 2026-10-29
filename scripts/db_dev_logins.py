"""Create the dev database, its password logins and Temporal's databases, and put
the connection strings in Key Vault. Dev only (ADR 0008).

    uv run python scripts/db_dev_logins.py            # create what is missing
    uv run python scripts/db_dev_logins.py --rotate   # new passwords for every login

Runs on the devbox as the server's Entra administrator (your az login), after
stacks 01, 04 and 04b are applied. Idempotent: rerunning creates nothing twice.

What it does, in order:

1. In ``postgres``: the login roles ``cloudops_app_azure`` and
   ``cloudops_ingest_azure`` (members of ``cloudops_app`` / ``cloudops_ingest``, never
   able to bypass RLS), the ``temporal`` login, and the databases ``cloudops``
   (owned by the migration identity, as locally by the migrator), ``temporal``
   and ``temporal_visibility``.
2. In ``cloudops``: the extensions the migrations expect an administrator to
   create, as ``infra/local/postgres/init.sh`` does locally.
3. In ``temporal_visibility``: ``btree_gin``.
4. For each login without a Key Vault secret (or all, with ``--rotate``): a new
   random password, sent to the server only as a SCRAM-SHA-256 verifier so the
   plaintext never reaches it or its logs, then stored in Key Vault. Nothing is
   printed or left on disk. Pods read the new value when they restart.

The Entra token (the psql password) and the generated passwords live only in this
process's memory and its children's environment or stdin.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SCRAM_ITERATIONS = 4096
_IDENT = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


@dataclass(frozen=True)
class Login:
    role: str
    secret: str
    group: str | None  # the NOLOGIN group role it joins, if any
    database: str | None  # set: the secret is a DSN for this database; None: the password


LOGINS = (
    Login("cloudops_app_azure", "postgres-app-dsn", "cloudops_app", "cloudops"),
    Login("cloudops_ingest_azure", "postgres-ingest-dsn", "cloudops_ingest", "cloudops"),
    Login("temporal", "temporal-db-password", None, None),
)


def scram_verifier(password: str, *, salt: bytes | None = None,
                   iterations: int = SCRAM_ITERATIONS) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores (RFC 5802/7677), computed here so
    ``ALTER ROLE ... PASSWORD`` never carries the plaintext. ASCII passwords only,
    which need no SASLprep."""
    if not password.isascii():
        raise ValueError("ASCII passwords only")
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(b: bytes) -> str:
        return base64.b64encode(b).decode()

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def quote_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unexpected identifier {name!r}")
    return '"' + name + '"'


def bootstrap_sql(migrator: str) -> str:
    """Roles and databases, in ``postgres``. Idempotent."""
    m = quote_ident(migrator)
    # Only quote_ident-checked names are interpolated below (S608).
    roles = "\n".join(
        f"SELECT 'CREATE ROLE {quote_ident(login.role)} LOGIN NOSUPERUSER NOBYPASSRLS "  # noqa: S608
        f"NOCREATEDB NOCREATEROLE' WHERE NOT EXISTS "
        f"(SELECT 1 FROM pg_roles WHERE rolname = '{login.role}')\\gexec"
        + (f"\nGRANT {quote_ident(login.group)} TO {quote_ident(login.role)};"
           if login.group else "")
        for login in LOGINS
    )
    dbs = "\n".join(
        f"SELECT 'CREATE DATABASE {quote_ident(db)} OWNER {owner}' WHERE NOT EXISTS "  # noqa: S608
        f"(SELECT 1 FROM pg_database WHERE datname = '{db}')\\gexec\n"
        f"REVOKE ALL ON DATABASE {quote_ident(db)} FROM PUBLIC;"
        for db, owner in (("cloudops", m), ("temporal", '"temporal"'),
                          ("temporal_visibility", '"temporal"'))
    )
    return f"""\\set ON_ERROR_STOP on
{roles}
-- CREATE DATABASE ... OWNER needs the right to act as the owner.
GRANT {m} TO CURRENT_USER;
GRANT "temporal" TO CURRENT_USER;
{dbs}
GRANT CONNECT ON DATABASE "cloudops" TO "cloudops_app", "cloudops_ingest";
"""


def cloudops_sql(migrator: str) -> str:
    """Extensions in ``cloudops``: an administrator's job, as locally."""
    m = quote_ident(migrator)
    return f"""\\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA IF NOT EXISTS partman;
CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;
GRANT ALL ON SCHEMA partman TO {m};
GRANT ALL ON ALL TABLES IN SCHEMA partman TO {m};
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA partman TO {m};
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA partman TO {m};
"""


VISIBILITY_SQL = "\\set ON_ERROR_STOP on\nCREATE EXTENSION IF NOT EXISTS btree_gin;\n"


def password_sql(role: str, verifier: str) -> str:
    if not verifier.startswith("SCRAM-SHA-256$") or "'" in verifier:
        raise ValueError("not a SCRAM verifier")
    return f"\\set ON_ERROR_STOP on\nALTER ROLE {quote_ident(role)} PASSWORD '{verifier}';\n"


def dsn(role: str, password: str, host: str, database: str) -> str:
    return (f"postgresql://{role}:{password}@{host}:5432/{database}"
            "?sslmode=verify-full&sslrootcert=system")


# --- side effects ------------------------------------------------------------------
#
# Every subprocess below runs a fixed argv (no shell) with a resolved executable;
# the only variable parts are names validated above and values passed via stdin,
# a file or the environment, never interpolated into a command line (S603).


def _exe(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"error: {name} is not installed")
    return path


def _az(*args: str) -> str:
    return subprocess.run([_exe("az"), *args], check=True,  # noqa: S603
                          capture_output=True, text=True).stdout


def _psql(host: str, admin: str, token: str, database: str, sql: str) -> None:
    conn = f"host={host} dbname={database} user={admin} sslmode=require connect_timeout=15"
    subprocess.run([_exe("psql"), "-q", "-X", conn], input=sql, text=True,  # noqa: S603
                   check=True,
                   env={**os.environ, "PGPASSWORD": token}, stdout=subprocess.DEVNULL)


def _secret_exists(vault: str, name: str) -> bool:
    found = subprocess.run([_exe("az"), "keyvault", "secret", "show",  # noqa: S603
                            "--vault-name", vault,
                            "--name", name, "--query", "id", "-o", "tsv"],
                           capture_output=True, text=True)
    return found.returncode == 0 and bool(found.stdout.strip())


def _write_secret(vault: str, name: str, value: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "value"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        # -o none: `secret set` would otherwise echo the value back.
        subprocess.run([_exe("az"), "keyvault", "secret", "set",  # noqa: S603
                        "--vault-name", vault, "--name",
                        name, "--file", str(path), "--encoding", "utf-8",
                        "--content-type", "text/plain", "-o", "none"], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", default="psql-cloudops-dev-neu")
    parser.add_argument("--vault", default="kv-cloudops-app-dev-weu")
    parser.add_argument("--migrator", default="id-cloudops-migrate-dev")
    parser.add_argument("--rotate", action="store_true", help="new passwords for every login")
    args = parser.parse_args(argv)
    if "-dev-" not in args.server or "-dev-" not in args.vault:
        print("error: dev only (ADR 0008)", file=sys.stderr)
        return 2

    host = f"{args.server}.postgres.database.azure.com"
    admin = json.loads(_az("ad", "signed-in-user", "show", "-o", "json"))["userPrincipalName"]
    token = _az("account", "get-access-token", "--resource-type", "oss-rdbms",
                "--query", "accessToken", "-o", "tsv").strip()

    _psql(host, admin, token, "postgres", bootstrap_sql(args.migrator))
    _psql(host, admin, token, "cloudops", cloudops_sql(args.migrator))
    _psql(host, admin, token, "temporal_visibility", VISIBILITY_SQL)
    print("roles, databases and extensions: ok")

    for login in LOGINS:
        if not args.rotate and _secret_exists(args.vault, login.secret):
            print(f"{login.role}: secret {login.secret} exists, unchanged")
            continue
        password = secrets.token_urlsafe(32)
        _psql(host, admin, token, "postgres", password_sql(login.role, scram_verifier(password)))
        value = dsn(login.role, password, host, login.database) if login.database else password
        _write_secret(args.vault, login.secret, value)
        print(f"{login.role}: new password set, stored as {login.secret}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
