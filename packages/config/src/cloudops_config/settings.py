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
import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from cloudops_config.errors import ConfigurationError, NotBuiltError
from cloudops_config.identity import TOKEN_FILE, WorkloadIdentity
from cloudops_config.llm import LlmGateway, MockLlmGateway
from cloudops_config.secrets import EnvFileSecretStore, MountedSecretStore, SecretStore
from cloudops_config.temporal_tls import TemporalTls

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
    AUTH = "auth"
    SERVICENOW = "servicenow"


class DbLogin(enum.StrEnum):
    """Which database login a service connects as; the value is its secret name."""

    APP = "postgres-app-dsn"
    INGEST = "postgres-ingest-dsn"


_DB_COMPONENT: Final = {
    Component.DATABASE_APP: DbLogin.APP,
    Component.DATABASE_INGEST: DbLogin.INGEST,
}


class AuthMode(enum.StrEnum):
    NONE = "none"  # local Redis
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
    """``tls`` is the mounted client certificate (mutual TLS, ADR 0010); ``None``
    only in the local profile, whose Temporal container is plaintext."""

    address: str
    namespace: str
    tls: TemporalTls | None


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    url: str
    database: str
    username: str
    password_secret: str
    auth: AuthMode


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """How the API validates bearer tokens (step 04, ADR 0002).

    Exactly one key source: ``jwks_url`` (Entra, Azure) or ``jwks_secret`` (the
    local dev issuer's public keys, from the secret store). Tokens must come from
    ``tenant_id``'s directory, for ``audience``, carrying ``required_scope``.
    """

    issuer: str
    audience: str
    tenant_id: str
    required_scope: str
    jwks_url: str | None
    jwks_secret: str | None


@dataclass(frozen=True, slots=True)
class ServiceNowConfig:
    """A real ServiceNow instance, read with OAuth client credentials (ADR 0009).

    ``instance_url`` and ``client_id`` are not secret; the client secret is read
    from the secret store by name each time a token is fetched, so a rotated
    secret is picked up without a restart.
    """

    instance_url: str
    client_id: str
    client_secret: str  # the secret's *name* in the store, never its value


@dataclass(frozen=True, slots=True)
class AzureOpenAIConfig:
    """The Azure OpenAI account the gateway calls (ADR 0012). Only the endpoint:
    authentication is the workload identity's Entra token, and the deployments
    are the tier aliases (cloudops_gateway.pricing)."""

    endpoint: str


AZURE_OPENAI_ENDPOINT_VAR: Final = "CLOUDOPS_AZURE_OPENAI_ENDPOINT"


def _azure_openai(environ: Mapping[str, str]) -> AzureOpenAIConfig:
    url = _required(environ, AZURE_OPENAI_ENDPOINT_VAR).rstrip("/")
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (parts.scheme != "https" or parts.path or parts.query
            or not host.endswith(".openai.azure.com")):
        raise ConfigurationError(
            f"{AZURE_OPENAI_ENDPOINT_VAR} must be the account's https://<name>.openai.azure.com "
            "address, with no path"
        )
    return AzureOpenAIConfig(f"https://{host}/")


SERVICENOW_URL_VAR: Final = "CLOUDOPS_SERVICENOW_URL"
SERVICENOW_CLIENT_ID_VAR: Final = "CLOUDOPS_SERVICENOW_CLIENT_ID"
SERVICENOW_CLIENT_SECRET: Final = "servicenow-read-client-secret"  # noqa: S105 - a name


def _servicenow(environ: Mapping[str, str], store: SecretStore) -> ServiceNowConfig:
    url = _required(environ, SERVICENOW_URL_VAR).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.path or parts.query:
        raise ConfigurationError(
            f"{SERVICENOW_URL_VAR} must be the instance's https:// address, with no path"
        )
    client_id = _required(environ, SERVICENOW_CLIENT_ID_VAR).strip()
    store.get(SERVICENOW_CLIENT_SECRET)  # raises MissingSecretError, naming the secret
    return ServiceNowConfig(url, client_id, SERVICENOW_CLIENT_SECRET)


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
    def auth(self) -> AuthConfig:
        return self._get(Component.AUTH, AuthConfig)

    @property
    def servicenow(self) -> ServiceNowConfig:
        return self._get(Component.SERVICENOW, ServiceNowConfig)

    @property
    def llm(self) -> LlmGateway:
        """The local mock. In Azure the LLM component is ``azure_openai``."""
        return self._get(Component.LLM, MockLlmGateway)

    @property
    def azure_openai(self) -> AzureOpenAIConfig:
        return self._get(Component.LLM, AzureOpenAIConfig)


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
LOCAL_TEMPORAL: Final = TemporalConfig("127.0.0.1:7233", "default", tls=None)
LOCAL_TELEMETRY: Final = TelemetryConfig(
    url="http://127.0.0.1:8123",
    database="telemetry",
    username="cloudops_telemetry",
    password_secret="clickhouse-telemetry-password",  # noqa: S106 - a secret's name
    auth=AuthMode.PASSWORD_SECRET,
)


# A dev issuer that only exists on a laptop: tokens are minted by
# `python -m cloudops_devdata.token` with the key in .env.local. Entra never
# issues for this tenant id or this audience, so a dev token is useless in Azure.
LOCAL_AUTH: Final = AuthConfig(
    issuer="https://login.cloudops.local/00000000-0000-4000-8000-0000000c10c0/v2.0",
    audience="api://cloudops-local",
    tenant_id="00000000-0000-4000-8000-0000000c10c0",
    required_scope="access_as_user",
    jwks_url=None,
    jwks_secret="api-dev-jwks",  # noqa: S106 - a secret's name
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
        elif c is Component.AUTH:
            store.get(LOCAL_AUTH.jwks_secret or "")
            components[c] = LOCAL_AUTH
        elif c is Component.SERVICENOW:
            # A real instance from a laptop is opt-in: the same variables as Azure,
            # the client secret in .env.local. Recorded fixtures need none of this.
            missing = [v for v in (SERVICENOW_URL_VAR, SERVICENOW_CLIENT_ID_VAR)
                       if not environ.get(v, "").strip()]
            if missing:
                raise ConfigurationError(
                    f"a real ServiceNow instance needs {' and '.join(missing)} (and "
                    f"{SERVICENOW_CLIENT_SECRET} in .env.local); use --fixtures otherwise"
                )
            components[c] = _servicenow(environ, store)
    return Settings(Environment.LOCAL, store, None, components)


# Azure: real services through workload identity -------------------------------

REDIS_HOST_VAR: Final = "CLOUDOPS_REDIS_HOST"
BRONZE_URL_VAR: Final = "CLOUDOPS_BRONZE_CONTAINER_URL"
REPORTS_URL_VAR: Final = "CLOUDOPS_REPORTS_CONTAINER_URL"
TEMPORAL_ADDRESS_VAR: Final = "CLOUDOPS_TEMPORAL_ADDRESS"
TEMPORAL_NAMESPACE_VAR: Final = "CLOUDOPS_TEMPORAL_NAMESPACE"
TEMPORAL_TLS_DIR_VAR: Final = "CLOUDOPS_TEMPORAL_TLS_DIR"
ENTRA_TENANT_VAR: Final = "CLOUDOPS_ENTRA_TENANT_ID"
API_AUDIENCE_VAR: Final = "CLOUDOPS_API_AUDIENCE"

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
            components[c] = _temporal(environ)
        elif c is Component.TELEMETRY:
            raise NotBuiltError(
                "the Azure telemetry store is undecided (LLD open item L1: ClickHouse or "
                "Azure Data Explorer); no Azure telemetry backend exists yet"
            )
        elif c is Component.AUTH:
            components[c] = _entra(environ)
        elif c is Component.SERVICENOW:
            components[c] = _servicenow(environ, store)
        elif c is Component.LLM:
            components[c] = _azure_openai(environ)
    return Settings(Environment.AZURE, store, identity, components)


def _temporal(environ: Mapping[str, str]) -> TemporalConfig:
    """Mutual TLS, always (ADR 0010): no certificate directory is an error, never
    a plaintext connection. The server must prove the address's host name."""
    address = _required(environ, TEMPORAL_ADDRESS_VAR)
    host = urlsplit(f"//{address}").hostname
    if not host or "." not in host or _is_ip(host):
        raise ConfigurationError(f"{TEMPORAL_ADDRESS_VAR} must be host:port with a DNS name")
    tls = TemporalTls(Path(_required(environ, TEMPORAL_TLS_DIR_VAR)), host)
    tls.read()  # raises now, naming the missing file, rather than on first connect
    return TemporalConfig(address, _required(environ, TEMPORAL_NAMESPACE_VAR), tls)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _entra(environ: Mapping[str, str]) -> AuthConfig:
    tenant = _required(environ, ENTRA_TENANT_VAR)
    try:
        tenant = str(UUID(tenant))
    except ValueError:
        raise ConfigurationError(f"{ENTRA_TENANT_VAR} must be the directory (tenant) id") from None
    return AuthConfig(
        issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
        audience=_required(environ, API_AUDIENCE_VAR),
        tenant_id=tenant,
        required_scope="access_as_user",  # LLD: app-cloudops-api exposes access_as_user
        jwks_url=f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
        jwks_secret=None,
    )


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
