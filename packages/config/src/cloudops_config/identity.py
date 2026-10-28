"""The Azure workload identity a pod runs as.

AKS's workload identity webhook injects three variables into a pod whose
service account is federated to a managed identity (LLD, Compute and Identity).
Every Azure backend authenticates with it, so the Azure profile validates it
before anything else and refuses to start without it. There is deliberately no
other Azure credential path: no client secret, no connection string, no
``DefaultAzureCredential`` chain that might find a developer's ``az login``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cloudops_config.errors import MissingCredentialError

CLIENT_ID: Final = "AZURE_CLIENT_ID"
TENANT_ID: Final = "AZURE_TENANT_ID"
TOKEN_FILE: Final = "AZURE_FEDERATED_TOKEN_FILE"  # noqa: S105 - a variable name
AUTHORITY_HOST: Final = "AZURE_AUTHORITY_HOST"

WORKLOAD_IDENTITY_VARS: Final = (CLIENT_ID, TENANT_ID, TOKEN_FILE)


@dataclass(frozen=True, slots=True)
class WorkloadIdentity:
    client_id: str
    tenant_id: str
    token_file: Path
    authority_host: str | None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> WorkloadIdentity:
        missing = [v for v in WORKLOAD_IDENTITY_VARS if not environ.get(v, "").strip()]
        if missing:
            raise MissingCredentialError(
                "Azure workload identity not present (missing "
                + ", ".join(missing)
                + "); is the pod's service account federated? "
                "Refusing to start rather than run without a credential"
            )
        token_file = Path(environ[TOKEN_FILE])
        try:
            if not token_file.read_text(encoding="utf-8").strip():
                raise MissingCredentialError(f"{TOKEN_FILE} points at an empty file")
        except OSError as exc:
            raise MissingCredentialError(
                f"{TOKEN_FILE} is not readable ({type(exc).__name__})"
            ) from None
        return cls(
            client_id=environ[CLIENT_ID].strip(),
            tenant_id=environ[TENANT_ID].strip(),
            token_file=token_file,
            authority_host=environ.get(AUTHORITY_HOST) or None,
        )
