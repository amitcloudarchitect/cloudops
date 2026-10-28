"""Local developer tooling over the stand-in Key Vault.

    python -m cloudops_config secret postgres-migration-dsn

Prints one secret from ``.env.local`` so the Makefile can hand it to a tool
that wants a file (alembic's DSN file) through process substitution, without
writing it to disk. Refuses outside the local profile: in Azure, secrets are
mounted files and nothing should print them.
"""

from __future__ import annotations

import os
import sys

from cloudops_config.errors import ConfigurationError
from cloudops_config.settings import Environment, load_settings


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "secret":
        print("usage: python -m cloudops_config secret <name>", file=sys.stderr)
        return 2
    try:
        settings = load_settings(os.environ, require=())
        if settings.environment is not Environment.LOCAL:
            raise ConfigurationError("printing secrets is local-only")
        value = settings.secrets.get(argv[2])
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(value + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
