"""A stored customer credential: its inventory row, and its secret by name.

The inventory row (``cloudops.customer_credential``, migration 0021) holds the
identifiers and the Key Vault secret names, never a secret. The secret is read
from the ``SecretStore`` each time it is used (CSI-mounted from Key Vault in
Azure, ``.env.local`` on a laptop), so a rotated secret takes effect without a
restart and nothing caches it beyond the SDK's own token cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_config import SecretStore
from cloudops_connectors.aws.sigv4 import AccessKey
from cloudops_connectors.core.identity import Binding
from cloudops_tenancy import ScopedConnection

if TYPE_CHECKING:
    from azure.core.credentials import AccessToken
    from azure.identity.aio import ClientSecretCredential

AZURE: Final = "azure"
AWS: Final = "aws"
PROVIDERS: Final = (AZURE, AWS)

_COLUMNS: Final = ("id, tenant_id, code, directory_id, client_id, access_key_id, account_ids,"
                   " secret_names, expires_at")
_ACTIVE: Final = text(f"SELECT {_COLUMNS} FROM cloudops.active_credentials(:provider)")  # noqa: S608 - constants
_ONE: Final = text(f"SELECT {_COLUMNS}, provider, status"  # noqa: S608 - constants
                   " FROM cloudops.customer_credential WHERE id = :id")


@dataclass(frozen=True)
class StoredCredential:
    id: UUID
    tenant_id: UUID
    provider: str
    code: str
    directory_id: UUID | None
    client_id: UUID | None
    access_key_id: str | None
    account_ids: tuple[str, ...]
    secret_names: tuple[str, ...]
    expires_at: datetime
    status: str = "active"

    @property
    def binding(self) -> Binding:
        return Binding(self.tenant_id, frozenset(self.account_ids))

    def missing_secrets(self, store: SecretStore) -> list[str]:
        """Names the store does not hold (they are names, never values)."""
        return [n for n in self.secret_names if n not in store]


def _row(r: Any, provider: str, status: str = "active") -> StoredCredential:
    return StoredCredential(
        UUID(str(r[0])), UUID(str(r[1])), provider, str(r[2]),
        None if r[3] is None else UUID(str(r[3])), None if r[4] is None else UUID(str(r[4])),
        None if r[5] is None else str(r[5]), tuple(str(a) for a in r[6]),
        tuple(str(n) for n in r[7]), r[8], status)


async def active(conn: ScopedConnection, provider: str) -> list[StoredCredential]:
    """Active, unexpired, unrevoked credentials of a provider, every tenant: through
    ``cloudops.active_credentials``, before any tenant is known."""
    return [_row(tuple(r), provider) for r in await conn.execute(_ACTIVE,
                                                                  {"provider": provider})]


async def one(conn: ScopedConnection, credential_id: UUID) -> StoredCredential | None:
    """A credential under its tenant's scope (outside it: ``None``)."""
    found = (await conn.execute(_ONE, {"id": credential_id})).one_or_none()
    if found is None:
        return None
    r = tuple(found)
    return _row(r, str(r[9]), str(r[10]))


class StoredSecretCredential:
    """An app registration's client secret, as an async token credential.

    The secret is read from the store at each token request; a new value builds a
    new ``ClientSecretCredential`` (the old one is closed), so a rotation in Key
    Vault is picked up on the CSI driver's next refresh. Nothing here logs."""

    def __init__(self, store: SecretStore, credential: StoredCredential, *,
                 authority: str | None = None) -> None:
        if credential.provider != AZURE or credential.directory_id is None \
                or credential.client_id is None:
            raise ValueError("an Azure credential needs a directory and a client id")
        self._store = store
        self._name = credential.secret_names[0]
        self._tenant = str(credential.directory_id)
        self._client = str(credential.client_id)
        self._authority = authority
        self._secret: str | None = None
        self._inner: ClientSecretCredential | None = None

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        from azure.identity.aio import ClientSecretCredential

        secret = self._store.get(self._name)
        if self._inner is None or secret != self._secret:
            if self._inner is not None:
                await self._inner.close()
            extra = {} if self._authority is None else {"authority": self._authority}
            self._inner = ClientSecretCredential(self._tenant, self._client, secret, **extra)
            self._secret = secret
        return await self._inner.get_token(*scopes, **kwargs)

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()
            self._inner = None
        self._secret = None


def aws_keys(store: SecretStore, credential: StoredCredential) -> AccessKey:
    """The key pair, read now. The key id in Key Vault must be the one onboarded."""
    if credential.provider != AWS or len(credential.secret_names) != 2:
        raise ValueError("an AWS credential has a key id and a secret key")
    key_id = store.get(credential.secret_names[0]).strip()
    if key_id != credential.access_key_id:
        raise KeyMismatchError("the access key id in Key Vault is not the one onboarded")
    return AccessKey(key_id, store.get(credential.secret_names[1]).strip())


class KeyMismatchError(ValueError):
    """Key Vault holds a different access key id than the inventory records."""
