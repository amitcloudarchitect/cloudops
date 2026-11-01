"""Entra bearer tokens from the pod's workload identity, as an ``httpx.Auth``.

    credential = workload_credential(settings.workload_identity)
    client = AzureClient(ARM, auth=EntraAuth(credential, ARM_SCOPE, ARM))
    blobs = BlobClient(url, auth=EntraAuth(credential, STORAGE_SCOPE, url))

The platform's own identity (``id-cloudops-ingest-{env}``), federated to the
connector's service account (stack 05). It is not a customer credential: what
it reaches is what stack 04b grants it, which for this path is Cost Management
Reader on the platform's own subscriptions and Storage Blob Data Reader on the
export container (ADR 0023). Nothing here reads a secret, a key or a SAS.

- **One audience, one host.** Each ``EntraAuth`` holds one scope and one origin
  (https, host, port). A request anywhere else is refused before a token is
  fetched, so a redirect or a stray ``nextLink`` cannot carry the token away.
  The clients already refuse foreign links; this is the second lock.
- **Async only.** The clients are async; the sync flow raises.
- **Tokens are the credential's.** ``WorkloadIdentityCredential`` caches and
  renews them; a 401 is not retried here (Temporal's retry policy owns that),
  and no token appears in an exception or a log.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from typing import TYPE_CHECKING, Final, Protocol

import httpx

if TYPE_CHECKING:
    from azure.identity.aio import WorkloadIdentityCredential

    from cloudops_config.identity import WorkloadIdentity

ARM_SCOPE: Final = "https://management.azure.com/.default"
STORAGE_SCOPE: Final = "https://storage.azure.com/.default"
GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"


class _Token(Protocol):
    @property
    def token(self) -> str: ...


class TokenCredential(Protocol):
    """``azure.core.credentials_async.AsyncTokenCredential``, as much as is used."""

    async def get_token(self, *scopes: str) -> _Token: ...


class ForeignHostError(ValueError):
    """A request for a host this token is not for. Never sent."""


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


class EntraAuth(httpx.Auth):
    def __init__(self, credential: TokenCredential, scope: str, base_url: str) -> None:
        origin = httpx.URL(base_url)
        if origin.scheme != "https" or not origin.host:
            raise ValueError("an Entra token is only sent over https")
        if not scope.startswith("https://") or not scope.endswith("/.default"):
            raise ValueError("a scope is a resource's /.default")
        self._credential = credential
        self._scope = scope
        self._origin = _origin(origin)

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        raise RuntimeError("EntraAuth is async only")

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if _origin(request.url) != self._origin:
            raise ForeignHostError("refusing to send a token to another host")
        token = (await self._credential.get_token(self._scope)).token
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def workload_credential(identity: WorkloadIdentity,
                        client_id: str | None = None) -> WorkloadIdentityCredential:
    """The async credential for the identity the Azure profile validated at start.
    Its values, not the environment's: the profile is the one place that reads them.

    ``client_id`` names an app registration to exchange the same projected token
    for instead of the pod's managed identity: one with a federated credential for
    this service account (``app-cloudops-sharepoint``, ADR 0028). Same directory."""
    from azure.identity.aio import WorkloadIdentityCredential

    kwargs = {} if identity.authority_host is None else {"authority": identity.authority_host}
    return WorkloadIdentityCredential(tenant_id=identity.tenant_id,
                                      client_id=client_id or identity.client_id,
                                      token_file_path=str(identity.token_file), **kwargs)
