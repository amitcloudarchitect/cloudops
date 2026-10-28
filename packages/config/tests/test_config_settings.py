"""The configuration layer chooses backends explicitly and fails loudly.

Most of these are failure cases on purpose: the property that matters is that
an incomplete Azure configuration can never quietly become a local one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cloudops_config import (
    AuthMode,
    Component,
    ConfigurationError,
    DbLogin,
    Environment,
    MissingCredentialError,
    MissingSecretError,
    MockLlmGateway,
    NotBuiltError,
    load_settings,
)

LOCAL_SECRETS = """\
# comment
POSTGRES_APP_DSN=postgresql://cloudops_app_local:pw@127.0.0.1:5432/cloudops
POSTGRES_INGEST_DSN=postgresql://cloudops_ingest_local:pw@127.0.0.1:5432/cloudops

CLICKHOUSE_TELEMETRY_PASSWORD=pw
API_DEV_JWKS={"keys":[]}
"""


@pytest.fixture
def local_env(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / ".env.local"
    path.write_text(LOCAL_SECRETS, encoding="utf-8")
    return {"CLOUDOPS_ENV": "local", "CLOUDOPS_LOCAL_SECRETS_FILE": str(path)}


@pytest.fixture
def azure_env(tmp_path: Path) -> dict[str, str]:
    token = tmp_path / "token"
    token.write_text("eyJ.federated.token", encoding="utf-8")
    mount = tmp_path / "secrets"
    mount.mkdir()
    (mount / "postgres-app-dsn").write_text("postgresql://app@psql/cloudops\n", encoding="utf-8")
    return {
        "CLOUDOPS_ENV": "azure",
        "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000001",
        "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000002",
        "AZURE_FEDERATED_TOKEN_FILE": str(token),
        "CLOUDOPS_SECRETS_DIR": str(mount),
        "CLOUDOPS_REDIS_HOST": "redis-cloudops-dev-weu.redis.cache.windows.net",
        "CLOUDOPS_BRONZE_CONTAINER_URL": "https://stcloudopsbronzedevweu.blob.core.windows.net/bronze",
        "CLOUDOPS_REPORTS_CONTAINER_URL": "https://stcloudopsreportsdevweu.blob.core.windows.net/reports",
        "CLOUDOPS_TEMPORAL_ADDRESS": "temporal-frontend.workflow:7233",
        "CLOUDOPS_TEMPORAL_NAMESPACE": "cloudops",
    }


# Environment selection ------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_environment_is_an_error_not_a_default(value: str | None) -> None:
    environ = {} if value is None else {"CLOUDOPS_ENV": value}
    with pytest.raises(ConfigurationError, match="CLOUDOPS_ENV is not set"):
        load_settings(environ, require=())


@pytest.mark.parametrize("value", ["Local", "dev", "prod", "azure-dev", "LOCAL"])
def test_unknown_environment_is_an_error(value: str) -> None:
    with pytest.raises(ConfigurationError, match="must be 'local' or 'azure'"):
        load_settings({"CLOUDOPS_ENV": value}, require=())


# Azure fails loudly ----------------------------------------------------------------


@pytest.mark.parametrize(
    "missing", ["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE"]
)
def test_azure_without_workload_identity_refuses_to_start(
    azure_env: dict[str, str], missing: str
) -> None:
    del azure_env[missing]
    with pytest.raises(MissingCredentialError, match=missing):
        load_settings(azure_env, require=())


def test_azure_with_unreadable_token_file_refuses(
    azure_env: dict[str, str], tmp_path: Path
) -> None:
    azure_env["AZURE_FEDERATED_TOKEN_FILE"] = str(tmp_path / "absent")
    with pytest.raises(MissingCredentialError, match="not readable"):
        load_settings(azure_env, require=())


def test_azure_with_empty_token_file_refuses(azure_env: dict[str, str]) -> None:
    Path(azure_env["AZURE_FEDERATED_TOKEN_FILE"]).write_text("\n", encoding="utf-8")
    with pytest.raises(MissingCredentialError, match="empty"):
        load_settings(azure_env, require=())


def test_azure_never_degrades_to_local_even_when_env_local_exists(
    azure_env: dict[str, str], local_env: dict[str, str]
) -> None:
    """A developer's .env.local sitting next to an Azure process is never read."""
    del azure_env["AZURE_CLIENT_ID"]
    environ = {**local_env, **azure_env}
    with pytest.raises(MissingCredentialError):
        load_settings(environ, require={Component.DATABASE_APP})


def test_azure_without_secrets_mount_refuses(azure_env: dict[str, str], tmp_path: Path) -> None:
    azure_env["CLOUDOPS_SECRETS_DIR"] = str(tmp_path / "not-mounted")
    with pytest.raises(ConfigurationError, match="secrets mount"):
        load_settings(azure_env, require=())
    del azure_env["CLOUDOPS_SECRETS_DIR"]
    with pytest.raises(ConfigurationError, match="CLOUDOPS_SECRETS_DIR"):
        load_settings(azure_env, require=())


def test_azure_missing_secret_fails_at_startup_naming_it(azure_env: dict[str, str]) -> None:
    with pytest.raises(MissingSecretError, match="postgres-ingest-dsn"):
        load_settings(azure_env, require={Component.DATABASE_INGEST})


@pytest.mark.parametrize(
    ("component", "var"),
    [
        (Component.REDIS, "CLOUDOPS_REDIS_HOST"),
        (Component.BLOB, "CLOUDOPS_BRONZE_CONTAINER_URL"),
        (Component.BLOB, "CLOUDOPS_REPORTS_CONTAINER_URL"),
        (Component.TEMPORAL, "CLOUDOPS_TEMPORAL_ADDRESS"),
        (Component.TEMPORAL, "CLOUDOPS_TEMPORAL_NAMESPACE"),
    ],
)
def test_azure_missing_endpoint_fails_for_the_component_that_needs_it(
    azure_env: dict[str, str], component: Component, var: str
) -> None:
    del azure_env[var]
    with pytest.raises(ConfigurationError, match=var):
        load_settings(azure_env, require={component})
    load_settings(azure_env, require=())  # not needed, not checked


@pytest.mark.parametrize(
    "url",
    [
        "http://stcloudopsbronzedevweu.blob.core.windows.net/bronze",
        "http://127.0.0.1:10000/devstoreaccount1/bronze",
        "https://stcloudopsbronzedevweu.blob.core.windows.net/",
    ],
)
def test_azure_blob_must_be_an_https_container(azure_env: dict[str, str], url: str) -> None:
    azure_env["CLOUDOPS_BRONZE_CONTAINER_URL"] = url
    with pytest.raises(ConfigurationError, match="https://"):
        load_settings(azure_env, require={Component.BLOB})


@pytest.mark.parametrize("component", [Component.LLM, Component.TELEMETRY])
def test_azure_backends_not_yet_built_refuse_rather_than_use_the_mock(
    azure_env: dict[str, str], component: Component
) -> None:
    with pytest.raises(NotBuiltError):
        load_settings(azure_env, require={component})


def test_azure_resolves_real_backends_with_workload_identity(azure_env: dict[str, str]) -> None:
    s = load_settings(
        azure_env,
        require={Component.DATABASE_APP, Component.REDIS, Component.BLOB, Component.TEMPORAL},
    )
    assert s.environment is Environment.AZURE
    assert s.workload_identity is not None
    assert s.database_dsn(DbLogin.APP) == "postgresql://app@psql/cloudops"
    assert (s.redis.port, s.redis.tls, s.redis.auth) == (6380, True, AuthMode.WORKLOAD_IDENTITY)
    assert s.blob.auth is AuthMode.WORKLOAD_IDENTITY
    assert s.temporal.tls is True


# Local ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "var", ["KUBERNETES_SERVICE_HOST", "AZURE_FEDERATED_TOKEN_FILE"]
)
def test_local_refuses_inside_a_cluster_or_federated_pod(
    local_env: dict[str, str], var: str
) -> None:
    local_env[var] = "10.0.0.1"
    with pytest.raises(ConfigurationError, match="not a laptop"):
        load_settings(local_env, require=())


def test_local_without_env_file_points_at_make_env(tmp_path: Path) -> None:
    environ = {"CLOUDOPS_ENV": "local", "CLOUDOPS_LOCAL_SECRETS_FILE": str(tmp_path / "nope")}
    with pytest.raises(ConfigurationError, match="make env"):
        load_settings(environ, require=())


def test_local_resolves_the_compose_containers(local_env: dict[str, str]) -> None:
    s = load_settings(local_env, require=set(Component))
    assert s.environment is Environment.LOCAL
    assert s.workload_identity is None
    assert s.database_dsn(DbLogin.INGEST).startswith("postgresql://cloudops_ingest_local")
    assert s.redis.host == "127.0.0.1"
    assert s.blob.auth is AuthMode.AZURITE_DEV_ACCOUNT
    assert s.telemetry.username == "cloudops_telemetry"
    assert isinstance(s.llm, MockLlmGateway)


def test_undeclared_component_is_refused_after_startup(local_env: dict[str, str]) -> None:
    s = load_settings(local_env, require={Component.REDIS})
    with pytest.raises(ConfigurationError, match="not declared"):
        _ = s.blob
    with pytest.raises(ConfigurationError, match="not declared"):
        s.database_dsn(DbLogin.APP)


def test_local_missing_secret_is_named_not_shown(tmp_path: Path) -> None:
    path = tmp_path / ".env.local"
    path.write_text("POSTGRES_APP_DSN=postgresql://u:hunter2@h/db\n", encoding="utf-8")
    environ = {"CLOUDOPS_ENV": "local", "CLOUDOPS_LOCAL_SECRETS_FILE": str(path)}
    with pytest.raises(MissingSecretError) as info:
        load_settings(environ, require={Component.DATABASE_INGEST})
    assert "POSTGRES_INGEST_DSN" in str(info.value)
    assert "hunter2" not in str(info.value)


# Auth -----------------------------------------------------------------------------------


def test_azure_auth_is_entra_and_requires_tenant_and_audience(azure_env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError, match="CLOUDOPS_ENTRA_TENANT_ID"):
        load_settings(azure_env, require={Component.AUTH})
    azure_env["CLOUDOPS_ENTRA_TENANT_ID"] = "not-a-guid"
    azure_env["CLOUDOPS_API_AUDIENCE"] = "api://cloudops"
    with pytest.raises(ConfigurationError, match="directory"):
        load_settings(azure_env, require={Component.AUTH})
    azure_env["CLOUDOPS_ENTRA_TENANT_ID"] = "11111111-1111-1111-1111-111111111111"
    auth = load_settings(azure_env, require={Component.AUTH}).auth
    assert auth.issuer == "https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/v2.0"
    assert auth.jwks_url is not None
    assert auth.jwks_url.startswith("https://login.microsoftonline.com/")
    assert auth.jwks_secret is None


def test_local_auth_is_the_dev_issuer_and_needs_its_keys(local_env: dict[str, str]) -> None:
    path = Path(local_env["CLOUDOPS_LOCAL_SECRETS_FILE"])
    full = path.read_text(encoding="utf-8")
    path.write_text(full.replace('API_DEV_JWKS={"keys":[]}\n', ""), "utf-8")
    with pytest.raises(MissingSecretError, match="api-dev-jwks"):
        load_settings(local_env, require={Component.AUTH})
    path.write_text(full, "utf-8")
    auth = load_settings(local_env, require={Component.AUTH}).auth
    assert "login.microsoftonline.com" not in auth.issuer
    assert auth.jwks_url is None
