"""Create the blob containers Terraform creates in Azure. Local profile only.

    CLOUDOPS_ENV=local python -m cloudops_devdata.provision
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient

from cloudops_config import AuthMode, Component, ConfigurationError, load_settings

# Azurite understands this connection string natively; it carries Azurite's
# published development account, which is not a secret.
AZURITE = "UseDevelopmentStorage=true"


def main() -> int:
    try:
        settings = load_settings(os.environ, require={Component.BLOB})
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if settings.blob.auth is not AuthMode.AZURITE_DEV_ACCOUNT:
        print("error: provisioning is for Azurite only; Terraform owns Azure", file=sys.stderr)
        return 1
    service = BlobServiceClient.from_connection_string(AZURITE)
    for url in (settings.blob.bronze_container_url, settings.blob.reports_container_url):
        name = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        try:
            service.create_container(name)
            print(f"azurite: created container {name}")
        except ResourceExistsError:
            print(f"azurite: container {name} exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
