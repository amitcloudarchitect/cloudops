"""Secret stores: where a named secret's value comes from.

Names are Key Vault secret names (lowercase, hyphenated), the same in every
environment. Code asks for ``postgres-app-dsn``; the store decides where that
lives:

- Azure: a file of that name in the directory the Secrets Store CSI driver
  mounts from Key Vault (LLD, Identity, secrets and keys). Never environment
  variables, never config.
- Local: the gitignored ``.env.local``, where ``postgres-app-dsn`` is the line
  ``POSTGRES_APP_DSN=...``. It plays the part of Key Vault and nothing else.

Neither store caches, logs or puts a value in an exception message.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Protocol

from cloudops_config.errors import ConfigurationError, MissingSecretError

# Key Vault's own rule is [0-9a-zA-Z-]{1,127}; lowercase-only keeps names
# unambiguous across stores, and excluding '.' and '/' makes path traversal
# out of the mount directory impossible.
_NAME: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ENV_KEY: Final = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$")


def validate_name(name: str) -> str:
    if not is_valid_name(name):
        raise ConfigurationError("secret names are lowercase, hyphen-separated, at most 127 chars")
    return name


def is_valid_name(name: object) -> bool:
    return isinstance(name, str) and len(name) <= 127 and _NAME.match(name) is not None


def env_key(name: str) -> str:
    """``postgres-app-dsn`` -> ``POSTGRES_APP_DSN``."""
    return validate_name(name).upper().replace("-", "_")


class SecretStore(Protocol):
    def get(self, name: str) -> str:
        """The secret's value. Raises ``MissingSecretError`` if absent or empty."""
        ...

    def __contains__(self, name: object) -> bool: ...


class MountedSecretStore:
    """Secrets as files in a CSI-mounted directory (Azure)."""

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        if not root.is_dir():
            raise ConfigurationError(f"secrets mount {root} does not exist or is not a directory")
        self._root = root

    def _path(self, name: str) -> Path:
        return self._root / validate_name(name)

    def get(self, name: str) -> str:
        path = self._path(name)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise MissingSecretError(f"secret {name!r} is not mounted") from None
        if not value:
            raise MissingSecretError(f"secret {name!r} is mounted but empty")
        return value

    def __contains__(self, name: object) -> bool:
        return is_valid_name(name) and self._path(str(name)).is_file()


class EnvFileSecretStore:
    """Secrets from a dotenv-style file (local development only).

    Deliberately strict: ``KEY=value`` lines, ``#`` comments and blank lines.
    No quoting rules, no interpolation, no ``export``; a duplicate key or a
    malformed line is an error rather than a guess about which one was meant.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    @classmethod
    def from_file(cls, path: Path) -> EnvFileSecretStore:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigurationError(
                f"{path} not found; run `make env` from the repository root to create it"
            ) from None
        return cls(parse_env_file(text, source=str(path)))

    def get(self, name: str) -> str:
        value = self._values.get(env_key(name), "")
        if not value:
            raise MissingSecretError(f"secret {name!r} ({env_key(name)}) is not in the env file")
        return value

    def __contains__(self, name: object) -> bool:
        return is_valid_name(name) and bool(self._values.get(env_key(str(name))))


def parse_env_file(text: str, *, source: str = "env file") -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        # Line numbers only: the line itself may hold a secret.
        if not sep or not _ENV_KEY.match(key):
            raise ConfigurationError(f"{source}:{lineno}: expected KEY=value")
        if key in values:
            raise ConfigurationError(f"{source}:{lineno}: {key} is set twice")
        values[key] = value.strip()
    return values
