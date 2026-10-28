"""Backend selection by environment. Explicit, eager, and fatal when incomplete.

    settings = load_settings(os.environ, require={Component.DATABASE_APP, Component.REDIS})
    db = Database.create(settings.database_dsn(DbLogin.APP))

Rules this module exists to enforce:

1. ``CLOUDOPS_ENV`` must be set to ``local`` or ``azure``. There is no default;
   unset is an error, not "probably local".
2. Each profile resolves only its own backends. The Azure profile never reads
   a local value, and nothing in Azure falls back to the local profile when a
   variable, credential or secret is missing — it raises.
3. A service declares the components it needs at startup and everything they
   require is validated then, so a misconfigured pod dies at boot rather than
   on its first request. Asking later for a component not declared is an error.
4. The local profile refuses to run inside Kubernetes or alongside an Azure
   workload identity: that combination is a mistake, never a dev laptop.

Security settings (TLS, ports, auth mode) are constants per profile, not
variables — only addresses vary.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeVar
from urllib.parse import urlsplit

from cloudops_config.errors import ConfigurationError, NotBuiltError
from cloudops_config.identity import TOKEN_FILE, WorkloadIdentity
from cloudops_config.llm import LlmGateway, MockLlmGateway
from cloudops_config.secrets import EnvFileSecretStore, MountedSecretStore, SecretStore

ENV_VAR: Final = "CLOUDOPS_ENV"
LOCAL_SECRETS_FILE_VAR: Final = "CLOUDOPS_LOCAL_SECRETS_FILE"
SECRETS_DIR_VAR: Final = "CLOUDOPS_SECRETS_DIR"
DEFAULT_LOCAL_SECRETS_FILE: Final = ".env.local"

# Signs that we are in a cluster or a federated pod, where `local` is never right.
_NOT_A_LAPTOP: Final = ("KUBERNETES_SERVICE_HOST", TOKEN_FILE)


class Environment(enum.StrEnum):
    LOCAL = "local"
    AZURE = "azure"


class Component(enum.StrEnum):
    DATABASE_APP = "database_app"
    DATABASE_INGEST = "database_ingest"
    REDIS = "redis"
    BLOB = "blob"
    TEMPORAL = "temporal"
    TELEMETRY = "telemetry"
    LLM = "llm"


class DbLogin(enum.StrEnum):
    """Which database login a service connects as; the value is its secret name."""

    APP = "postgres-app-dsn"
    INGEST = "postgres-ingest-dsn"


_DB_COMPONENT: Final = {
    Component.DATABASE_APP: DbLogin.APP,
    Component.DATABASE_INGEST: DbLogin.INGEST,
}


class AuthMode(enum.StrEnum):
    NONE = "none"  # local Redis, Temporal
    AZURITE_DEV_ACCOUNT = "azurite_dev_account"  # Azurite's published dev account
    PASSWORD_SECRET = "password_secret"  # noqa: S105 - a mode name; local ClickHouse user
    WORKLOAD_IDENTITY = "workload_identity"  # every Azure backend


@dataclass(frozen=True, slots=True)
class RedisConfig:
    host: str
    port: int
    tls: bool
    auth: AuthMode


@dataclass(frozen=True, slots=True)
class BlobConfig:
    bronze_container_url: str
    reports_container_url: str
    auth: AuthMode


@dataclass(frozen=True, slots=True)
class TemporalConfig:
    address: str
    namespace: str
    tls: bool
    auth: AuthMode


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    url: str
    database: str
    username: str
    password_secret: str
    auth: AuthMode


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class Settings:
    environment: Environment
    secrets: SecretStore
    workload_identity: WorkloadIdentity | None
    components: Mapping[Component, object]

    def _get(self, component: Component, kind: type[_T]) -> _T:
        try:
            value = self.components[component]
        except KeyError:
            raise ConfigurationError(
                f"{component} was not declared in load_settings(require=...); "
                "declare it so it is validated at startup"
            ) from None
        if not isinstance(value, kind):
            raise ConfigurationError(f"{component} resolved to the wrong type")
        return value

    def database_dsn(self, login: DbLogin) -> str:
        component = next(c for c, db in _DB_COMPONENT.items() if db is DbLogin(login))
        self._get(component, _DatabaseDeclared)
        return self.secrets.get(login.value)

    @property
    def redis(self) -> RedisConfig:
        return self._get(Component.REDIS, RedisConfig)

    @property
    def blob(self) -> BlobConfig:
        return self._get(Component.BLOB, BlobConfig)

    @property
    def temporal(self) -> TemporalConfig:
        return self._get(Component.TEMPORAL, TemporalConfig)

    @property
    def telemetry(self) -> TelemetryConfig:
        return self._get(Component.TELEMETRY, TelemetryConfig)

    @property
    def llm(self) -> LlmGateway:
        # Only the mock exists so far; the Azure profile refuses LLM at load.
        return self._get(Component.LLM, MockLlmGateway)


@dataclass(frozen=True, slots=True)
class _DatabaseDeclared:
    """Marker: the DSN secrets were checked at startup."""


def environment_from(environ: Mapping[str, str]) -> Environment:
    raw = environ.get(ENV_VAR)
    if raw is None or not raw.strip():
        raise ConfigurationError(f"{ENV_VAR} is not set; set it to 'local' or 'azure' explicitly")
    try:
        return Environment(raw.strip())
    except ValueError:
        raise ConfigurationError(f"{ENV_VAR} must be 'local' or 'azure'") from None


def load_settings(environ: Mapping[str, str], *, require: Iterable[Component]) -> Settings:
    """Resolve and validate every required component, or raise."""
    wanted = frozenset(Component(c) for c in require)
    env = environment_from(environ)
    if env is Environment.LOCAL:
        return _local(environ, wanted)
    return _azure(environ, wanted)


# Local: the containers in docker-compose.yml ------------------------------------

LOCAL_REDIS: Final = RedisConfig("127.0.0.1", 6379, tls=False, auth=AuthMode.NONE)
# Azurite's account name and URL layout are public constants, not secrets.
LOCAL_BLOB: Final = BlobConfig(
    bronze_container_url="http://127.0.0.1:10000/devstoreaccount1/bronze",
    reports_container_url="http://127.0.0.1:10000/devstoreaccount1/reports",
    auth=AuthMode.AZURITE_DEV_ACCOUNT,
)
LOCAL_TEMPORAL: Final = TemporalConfig("127.0.0.1:7233", "default", tls=False, auth=AuthMode.NONE)
LOCAL_TELEMETRY: Final = TelemetryConfig(
    url="http://127.0.0.1:8123",
    database="telemetry",
    username="cloudops_telemetry",
    password_secret="clickhouse-telemetry-password",  # noqa: S106 - a secret's name
    auth=AuthMode.PASSWORD_SECRET,
)


def _local(environ: Mapping[str, str], wanted: frozenset[Component]) -> Settings:
    present = [v for v in _NOT_A_LAPTOP if environ.get(v)]
    if present:
        raise ConfigurationError(
            f"{ENV_VAR}=local but {', '.join(present)} is set: this is a cluster or a "
            "federated pod, not a laptop. Refusing to use local backends here"
        )
    path = Path(environ.get(LOCAL_SECRETS_FILE_VAR) or DEFAULT_LOCAL_SECRETS_FILE)
    store = EnvFileSecretStore.from_file(path)
    components: dict[Component, object] = {}
    for c in sorted(wanted):
        if c in _DB_COMPONENT:
            components[c] = _check_database_secret(store, _DB_COMPONENT[c])
        elif c is Component.REDIS:
            components[c] = LOCAL_REDIS
        elif c is Component.BLOB:
            components[c] = LOCAL_BLOB
        elif c is Component.TEMPORAL:
            components[c] = LOCAL_TEMPORAL
        elif c is Component.TELEMETRY:
            store.get(LOCAL_TELEMETRY.password_secret)
            components[c] = LOCAL_TELEMETRY
        elif c is Component.LLM:
            components[c] = MockLlmGateway.with_packaged_responses()
    return Settings(Environment.LOCAL, store, None, components)


# Azure: real services through workload identity -------------------------------

REDIS_HOST_VAR: Final = "CLOUDOPS_REDIS_HOST"
BRONZE_URL_VAR: Final = "CLOUDOPS_BRONZE_CONTAINER_URL"
REPORTS_URL_VAR: Final = "CLOUDOPS_REPORTS_CONTAINER_URL"
TEMPORAL_ADDRESS_VAR: Final = "CLOUDOPS_TEMPORAL_ADDRESS"
TEMPORAL_NAMESPACE_VAR: Final = "CLOUDOPS_TEMPORAL_NAMESPACE"

# LLD, Redis: TLS required, non-TLS port disabled.
AZURE_REDIS_PORT: Final = 6380


def _azure(environ: Mapping[str, str], wanted: frozenset[Component]) -> Settings:
    identity = WorkloadIdentity.from_environ(environ)
    store = MountedSecretStore(Path(_required(environ, SECRETS_DIR_VAR)))
    components: dict[Component, object] = {}
    for c in sorted(wanted):
        if c in _DB_COMPONENT:
            components[c] = _check_database_secret(store, _DB_COMPONENT[c])
        elif c is Component.REDIS:
            components[c] = RedisConfig(
                _required(environ, REDIS_HOST_VAR),
                AZURE_REDIS_PORT,
                tls=True,
                auth=AuthMode.WORKLOAD_IDENTITY,
            )
        elif c is Component.BLOB:
            components[c] = BlobConfig(
                bronze_container_url=_https_url(environ, BRONZE_URL_VAR),
                reports_container_url=_https_url(environ, REPORTS_URL_VAR),
                auth=AuthMode.WORKLOAD_IDENTITY,
            )
        elif c is Component.TEMPORAL:
            # TLS assumed; the LLD does not yet say how Temporal is hosted in AKS.
            components[c] = TemporalConfig(
                _required(environ, TEMPORAL_ADDRESS_VAR),
                _required(environ, TEMPORAL_NAMESPACE_VAR),
                tls=True,
                auth=AuthMode.WORKLOAD_IDENTITY,
            )
        elif c is Component.TELEMETRY:
            raise NotBuiltError(
                "the Azure telemetry store is undecided (LLD open item L1: ClickHouse or "
                "Azure Data Explorer); no Azure telemetry backend exists yet"
            )
        elif c is Component.LLM:
            raise NotBuiltError(
                "the Azure OpenAI gateway is not built yet (step 06); the mock is local-only"
            )
    return Settings(Environment.AZURE, store, identity, components)


def _check_database_secret(store: SecretStore, login: DbLogin) -> _DatabaseDeclared:
    store.get(login.value)  # raises MissingSecretError, naming the secret
    return _DatabaseDeclared()


def _required(environ: Mapping[str, str], var: str) -> str:
    value = environ.get(var, "").strip()
    if not value:
        raise ConfigurationError(f"{var} is required in the Azure profile and is not set")
    return value


def _https_url(environ: Mapping[str, str], var: str) -> str:
    value = _required(environ, var)
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or parts.path.strip("/") == "":
        raise ConfigurationError(f"{var} must be an https:// container URL")
    return value
