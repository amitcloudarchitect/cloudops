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
    CredentialSource,
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
    tls = tmp_path / "temporal-tls"
    tls.mkdir()
    for name in ("ca.crt", "tls.crt", "tls.key"):
        (tls / name).write_text(f"-----BEGIN {name}-----\n", encoding="utf-8")
    return {
        "CLOUDOPS_ENV": "azure",
        "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000001",
        "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000002",
        "AZURE_FEDERATED_TOKEN_FILE": str(token),
        "CLOUDOPS_SECRETS_DIR": str(mount),
        "CLOUDOPS_REDIS_HOST": "redis-cloudops-dev-weu.redis.cache.windows.net",
        "CLOUDOPS_BRONZE_CONTAINER_URL": "https://stcloudopsbronzedevweu.blob.core.windows.net/bronze",
        "CLOUDOPS_REPORTS_CONTAINER_URL": "https://stcloudopsreportsdevweu.blob.core.windows.net/reports",
        "CLOUDOPS_TEMPORAL_ADDRESS": "temporal-frontend.temporal.svc.cluster.local:7233",
        "CLOUDOPS_TEMPORAL_NAMESPACE": "cloudops",
        "CLOUDOPS_TEMPORAL_TLS_DIR": str(tls),
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


@pytest.mark.parametrize("component", [Component.TELEMETRY])
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
    assert s.temporal.tls is not None
    assert s.temporal.tls.server_name == "temporal-frontend.temporal.svc.cluster.local"


# Temporal: mutual TLS, never plaintext (ADR 0010) -------------------------------------


def test_azure_temporal_without_a_certificate_is_refused(azure_env: dict[str, str]) -> None:
    del azure_env["CLOUDOPS_TEMPORAL_TLS_DIR"]
    with pytest.raises(ConfigurationError, match="CLOUDOPS_TEMPORAL_TLS_DIR"):
        load_settings(azure_env, require={Component.TEMPORAL})


@pytest.mark.parametrize("missing", ["ca.crt", "tls.crt", "tls.key"])
def test_azure_temporal_with_an_incomplete_certificate_is_refused(
    azure_env: dict[str, str], missing: str
) -> None:
    (Path(azure_env["CLOUDOPS_TEMPORAL_TLS_DIR"]) / missing).unlink()
    with pytest.raises(ConfigurationError, match=missing.replace(".", r"\.")):
        load_settings(azure_env, require={Component.TEMPORAL})


@pytest.mark.parametrize("address", ["10.0.0.7:7233", "temporal:7233", ":7233"])
def test_azure_temporal_needs_a_dns_name_the_certificate_can_prove(
    azure_env: dict[str, str], address: str
) -> None:
    azure_env["CLOUDOPS_TEMPORAL_ADDRESS"] = address
    with pytest.raises(ConfigurationError, match="DNS name"):
        load_settings(azure_env, require={Component.TEMPORAL})


def test_the_private_key_never_appears_in_a_repr(azure_env: dict[str, str]) -> None:
    key = Path(azure_env["CLOUDOPS_TEMPORAL_TLS_DIR"]) / "tls.key"
    key.write_text("PRIVATE-KEY-MATERIAL", encoding="utf-8")
    tls = load_settings(azure_env, require={Component.TEMPORAL}).temporal.tls
    assert tls is not None
    material = tls.read()
    assert material.key == b"PRIVATE-KEY-MATERIAL"
    assert "PRIVATE-KEY-MATERIAL" not in repr(material)


def test_local_temporal_is_plaintext_to_the_compose_container(local_env: dict[str, str]) -> None:
    assert load_settings(local_env, require={Component.TEMPORAL}).temporal.tls is None


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
    # A real ServiceNow instance and a customer credential are opt-in locally, and the
    # live Azure cost feed is Azure-only (all tested below).
    s = load_settings(local_env,
                      require=set(Component) - {Component.SERVICENOW, Component.AZURE_COST,
                                                Component.CUSTOMER_ACCESS})
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


# --- ServiceNow (ADR 0009) ----------------------------------------------------------


def _with_servicenow(env: dict[str, str], url: str = "https://dev1.service-now.com") -> None:
    env["CLOUDOPS_SERVICENOW_URL"] = url
    env["CLOUDOPS_SERVICENOW_CLIENT_ID"] = "82e09795000b4f1693138c3427623f04"


def test_azure_servicenow_names_the_secret_and_never_holds_it(azure_env: dict[str, str]) -> None:
    _with_servicenow(azure_env)
    (Path(azure_env["CLOUDOPS_SECRETS_DIR"]) / "servicenow-read-client-secret").write_text(
        "do-not-copy", encoding="utf-8")
    sn = load_settings(azure_env, require={Component.SERVICENOW}).servicenow
    assert sn.instance_url == "https://dev1.service-now.com"
    assert sn.client_secret == "servicenow-read-client-secret"
    assert "do-not-copy" not in repr(sn)


def test_azure_servicenow_without_its_secret_refuses(azure_env: dict[str, str]) -> None:
    _with_servicenow(azure_env)
    with pytest.raises(MissingSecretError, match="servicenow-read-client-secret"):
        load_settings(azure_env, require={Component.SERVICENOW})


@pytest.mark.parametrize("url", ["http://dev1.service-now.com", "https://dev1.service-now.com/api",
                                 "https://dev1.service-now.com?x=1", "dev1.service-now.com"])
def test_servicenow_url_must_be_a_bare_https_instance(azure_env: dict[str, str], url: str) -> None:
    _with_servicenow(azure_env, url)
    (Path(azure_env["CLOUDOPS_SECRETS_DIR"]) / "servicenow-read-client-secret").write_text(
        "x", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="CLOUDOPS_SERVICENOW_URL"):
        load_settings(azure_env, require={Component.SERVICENOW})


def test_servicenow_needs_a_client_id(azure_env: dict[str, str]) -> None:
    azure_env["CLOUDOPS_SERVICENOW_URL"] = "https://dev1.service-now.com"
    with pytest.raises(ConfigurationError, match="CLOUDOPS_SERVICENOW_CLIENT_ID"):
        load_settings(azure_env, require={Component.SERVICENOW})


def test_local_real_servicenow_is_opt_in_and_says_how(local_env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError, match="--fixtures") as info:
        load_settings(local_env, require={Component.SERVICENOW})
    assert "Azure" not in str(info.value)


# Azure OpenAI (ADR 0012) -------------------------------------------------------------


def test_azure_llm_is_the_openai_account_never_the_mock(azure_env: dict[str, str]) -> None:
    azure_env["CLOUDOPS_AZURE_OPENAI_ENDPOINT"] = "https://oai-cloudops-dev-weu.openai.azure.com/"
    s = load_settings(azure_env, require={Component.LLM})
    assert s.azure_openai.endpoint == "https://oai-cloudops-dev-weu.openai.azure.com/"
    with pytest.raises(ConfigurationError, match="wrong type"):
        s.llm  # noqa: B018 - the mock is local-only


def test_azure_llm_without_an_endpoint_is_refused(azure_env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError, match="CLOUDOPS_AZURE_OPENAI_ENDPOINT"):
        load_settings(azure_env, require={Component.LLM})


@pytest.mark.parametrize("url", [
    "http://oai-cloudops-dev-weu.openai.azure.com",
    "https://oai-cloudops-dev-weu.openai.azure.com/openai/deployments",
    "https://api.openai.com",
    "https://oai-cloudops-dev-weu.openai.azure.com.example.net",
])
def test_azure_llm_endpoint_must_be_an_azure_openai_account(
    azure_env: dict[str, str], url: str
) -> None:
    azure_env["CLOUDOPS_AZURE_OPENAI_ENDPOINT"] = url
    with pytest.raises(ConfigurationError, match=r"openai\.azure\.com"):
        load_settings(azure_env, require={Component.LLM})


# The live Azure cost feed (ADR 0023) ----------------------------------------------

_EXPORTS = "https://stcloudopsbronzedevweu.blob.core.windows.net"
_ROOT = "cost-exports-platform/focus/cloudops-platform-dev-0a1b2c3d"


@pytest.fixture
def export_env(azure_env: dict[str, str]) -> dict[str, str]:
    azure_env["CLOUDOPS_AZURE_COST_LINES"] = "export"
    return azure_env


def test_azure_cost_names_the_export_account_and_roots(export_env: dict[str, str]) -> None:
    azure_env = export_env
    azure_env["CLOUDOPS_AZURE_COST_EXPORT_ACCOUNT_URL"] = _EXPORTS + "/"
    azure_env["CLOUDOPS_AZURE_COST_EXPORT_ROOTS"] = f"{_ROOT}, {_ROOT}-2"
    cost = load_settings(azure_env, require={Component.AZURE_COST}).azure_cost
    assert cost.lines == "export"
    assert cost.export_account_url == _EXPORTS
    assert cost.export_roots == (_ROOT, f"{_ROOT}-2")


@pytest.mark.parametrize("url", [
    "http://stcloudopsbronzedevweu.blob.core.windows.net",
    "https://stcloudopsbronzedevweu.blob.core.windows.net/cost-exports-platform",
    "https://stcloudopsbronzedevweu.blob.core.windows.net:8443",
    "https://evil.example/stcloudopsbronzedevweu.blob.core.windows.net",
    "https://stcloudopsbronzedevweu.blob.core.windows.net.evil.example",
])
def test_azure_cost_account_must_be_a_bare_blob_endpoint(
    export_env: dict[str, str], url: str
) -> None:
    azure_env = export_env
    azure_env["CLOUDOPS_AZURE_COST_EXPORT_ACCOUNT_URL"] = url
    azure_env["CLOUDOPS_AZURE_COST_EXPORT_ROOTS"] = _ROOT
    with pytest.raises(ConfigurationError, match=r"blob\.core\.windows\.net"):
        load_settings(azure_env, require={Component.AZURE_COST})


@pytest.mark.parametrize("roots", [
    "cost-exports-platform", "/focus/x", "cost-exports-platform/../servicenow",
    "cost-exports-platform/focus/", f"{_ROOT},", f"{_ROOT},{_ROOT}",
])
def test_azure_cost_roots_are_container_and_path_each_once(
    export_env: dict[str, str], roots: str
) -> None:
    azure_env = export_env
    azure_env["CLOUDOPS_AZURE_COST_EXPORT_ACCOUNT_URL"] = _EXPORTS
    azure_env["CLOUDOPS_AZURE_COST_EXPORT_ROOTS"] = roots
    with pytest.raises(ConfigurationError, match="CLOUDOPS_AZURE_COST_EXPORT_ROOTS"):
        load_settings(azure_env, require={Component.AZURE_COST})


def test_azure_cost_needs_both_variables(export_env: dict[str, str]) -> None:
    azure_env = export_env
    with pytest.raises(ConfigurationError, match="CLOUDOPS_AZURE_COST_EXPORT_ACCOUNT_URL"):
        load_settings(azure_env, require={Component.AZURE_COST})
    azure_env["CLOUDOPS_AZURE_COST_EXPORT_ACCOUNT_URL"] = _EXPORTS
    with pytest.raises(ConfigurationError, match="CLOUDOPS_AZURE_COST_EXPORT_ROOTS"):
        load_settings(azure_env, require={Component.AZURE_COST})


def test_azure_cost_says_where_its_lines_come_from(azure_env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError, match="CLOUDOPS_AZURE_COST_LINES"):
        load_settings(azure_env, require={Component.AZURE_COST})
    azure_env["CLOUDOPS_AZURE_COST_LINES"] = "exports"
    with pytest.raises(ConfigurationError, match="'export' or 'query'"):
        load_settings(azure_env, require={Component.AZURE_COST})


def test_azure_cost_from_the_query_api_needs_no_storage(azure_env: dict[str, str]) -> None:
    azure_env["CLOUDOPS_AZURE_COST_LINES"] = "query"
    cost = load_settings(azure_env, require={Component.AZURE_COST}).azure_cost
    assert (cost.lines, cost.export_account_url, cost.export_roots) == ("query", None, ())
    azure_env["CLOUDOPS_AZURE_COST_EXPORT_ROOTS"] = _ROOT
    with pytest.raises(ConfigurationError, match="say export"):
        load_settings(azure_env, require={Component.AZURE_COST})


def test_local_has_no_live_cost_feed(local_env: dict[str, str]) -> None:
    local_env["CLOUDOPS_AZURE_COST_EXPORT_ACCOUNT_URL"] = _EXPORTS
    local_env["CLOUDOPS_AZURE_COST_EXPORT_ROOTS"] = _ROOT
    with pytest.raises(ConfigurationError, match="--fixtures"):
        load_settings(local_env, require={Component.AZURE_COST})


# Customer credentials (ADR 0003, guardrail 4; ADR 0027) ----------------------------


@pytest.mark.parametrize("deployment", ["dev", "tst", "uat"])
def test_stored_customer_keys_are_allowed_before_production(
    azure_env: dict[str, str], deployment: str
) -> None:
    azure_env |= {"CLOUDOPS_CUSTOMER_CREDENTIALS": "stored_key",
                  "CLOUDOPS_DEPLOYMENT": deployment}
    access = load_settings(azure_env, require={Component.CUSTOMER_ACCESS}).customer_access
    assert access.source is CredentialSource.STORED_KEY
    assert access.deployment == deployment


def test_stored_customer_keys_are_refused_in_production(azure_env: dict[str, str]) -> None:
    azure_env |= {"CLOUDOPS_CUSTOMER_CREDENTIALS": "stored_key", "CLOUDOPS_DEPLOYMENT": "prd"}
    with pytest.raises(ConfigurationError, match="refused in production"):
        load_settings(azure_env, require={Component.CUSTOMER_ACCESS})


def test_stored_customer_keys_are_refused_in_production_from_a_laptop_too(
    local_env: dict[str, str],
) -> None:
    local_env |= {"CLOUDOPS_CUSTOMER_CREDENTIALS": "stored_key", "CLOUDOPS_DEPLOYMENT": "prd"}
    with pytest.raises(ConfigurationError, match="refused in production"):
        load_settings(local_env, require={Component.CUSTOMER_ACCESS})


@pytest.mark.parametrize("deployment", [None, "", "production", "PRD"])
def test_an_unnamed_deployment_never_admits_stored_keys(
    azure_env: dict[str, str], deployment: str | None
) -> None:
    azure_env["CLOUDOPS_CUSTOMER_CREDENTIALS"] = "stored_key"
    if deployment is not None:
        azure_env["CLOUDOPS_DEPLOYMENT"] = deployment
    with pytest.raises(ConfigurationError, match="CLOUDOPS_DEPLOYMENT"):
        load_settings(azure_env, require={Component.CUSTOMER_ACCESS})


@pytest.mark.parametrize("value", [None, "", "stored", "Stored_Key", "both"])
def test_the_credential_source_is_explicit(azure_env: dict[str, str], value: str | None) -> None:
    azure_env["CLOUDOPS_DEPLOYMENT"] = "dev"
    if value is not None:
        azure_env["CLOUDOPS_CUSTOMER_CREDENTIALS"] = value
    with pytest.raises(ConfigurationError, match="stored_key' or 'delegated"):
        load_settings(azure_env, require={Component.CUSTOMER_ACCESS})


@pytest.mark.parametrize("deployment", ["dev", "prd"])
def test_delegated_access_is_not_built(azure_env: dict[str, str], deployment: str) -> None:
    azure_env |= {"CLOUDOPS_CUSTOMER_CREDENTIALS": "delegated",
                  "CLOUDOPS_DEPLOYMENT": deployment}
    with pytest.raises(NotBuiltError, match="broker"):
        load_settings(azure_env, require={Component.CUSTOMER_ACCESS})


def test_a_laptop_may_use_a_stored_key(local_env: dict[str, str]) -> None:
    local_env["CLOUDOPS_CUSTOMER_CREDENTIALS"] = "stored_key"
    access = load_settings(local_env, require={Component.CUSTOMER_ACCESS}).customer_access
    assert (access.source, access.deployment) == (CredentialSource.STORED_KEY, "local")
