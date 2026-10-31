"""cloudops_config — which backends this process talks to, chosen explicitly.

    settings = load_settings(os.environ, require={Component.DATABASE_APP, Component.LLM})

``CLOUDOPS_ENV`` selects ``local`` (the docker-compose containers, ``.env.local``
as a stand-in Key Vault, a mock LLM) or ``azure`` (real services, CSI-mounted
secrets, workload identity). Nothing falls back from one to the other.
"""

from cloudops_config.errors import (
    ConfigurationError,
    MissingCredentialError,
    MissingSecretError,
    NotBuiltError,
)
from cloudops_config.identity import WorkloadIdentity
from cloudops_config.llm import LlmGateway, MockLlmGateway, Tier
from cloudops_config.secrets import EnvFileSecretStore, MountedSecretStore, SecretStore
from cloudops_config.settings import (
    AuthConfig,
    AuthMode,
    AzureCostConfig,
    AzureOpenAIConfig,
    BlobConfig,
    Component,
    DbLogin,
    Environment,
    RedisConfig,
    ServiceNowConfig,
    Settings,
    TelemetryConfig,
    TemporalConfig,
    load_settings,
)
from cloudops_config.temporal_tls import TemporalTls, TlsMaterial

__all__ = [
    "AuthConfig",
    "AuthMode",
    "AzureCostConfig",
    "AzureOpenAIConfig",
    "BlobConfig",
    "Component",
    "ConfigurationError",
    "DbLogin",
    "EnvFileSecretStore",
    "Environment",
    "LlmGateway",
    "MissingCredentialError",
    "MissingSecretError",
    "MockLlmGateway",
    "MountedSecretStore",
    "NotBuiltError",
    "RedisConfig",
    "SecretStore",
    "ServiceNowConfig",
    "Settings",
    "TelemetryConfig",
    "TemporalConfig",
    "TemporalTls",
    "Tier",
    "TlsMaterial",
    "WorkloadIdentity",
    "load_settings",
]
