"""Building the AI gateway for a process: the AI worker, and the API for the
assistant (ADR 0029).

Locally the gateway uses the mock providers. In Azure it calls the Azure OpenAI
account (ADR 0012) as the process's workload identity: an Entra token, no key,
over the private endpoint. Nothing falls back from one to the other.
"""

from __future__ import annotations

from contextlib import AsyncExitStack

from cloudops_config import Environment, Settings
from cloudops_gateway.azure_openai import TOKEN_SCOPE, AzureOpenAIProvider
from cloudops_gateway.gateway import Gateway
from cloudops_gateway.pricing import AZURE_DEPLOYMENTS, LOCAL_DEPLOYMENTS
from cloudops_gateway.providers import MockProvider, Provider
from cloudops_tenancy import Database


async def build_gateway(settings: Settings, db: Database, stack: AsyncExitStack) -> Gateway:
    if settings.environment is Environment.LOCAL:
        return Gateway(db, {"mock": MockProvider("mock"),
                            "mock-secondary": MockProvider("mock-secondary")},
                       LOCAL_DEPLOYMENTS)
    from azure.identity.aio import WorkloadIdentityCredential

    credential = WorkloadIdentityCredential()  # caches and renews the token
    stack.push_async_callback(credential.close)

    async def token() -> str:
        return (await credential.get_token(TOKEN_SCOPE)).token

    provider = AzureOpenAIProvider(settings.azure_openai.endpoint, token=token)
    stack.push_async_callback(provider.aclose)
    providers: dict[str, Provider] = {provider.name: provider}
    return Gateway(db, providers, AZURE_DEPLOYMENTS)
