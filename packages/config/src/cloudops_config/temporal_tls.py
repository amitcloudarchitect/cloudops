"""Client certificates for mutual TLS to Temporal (ADR 0010).

cert-manager issues each worker a short-lived client certificate into a
kubernetes.io/tls Secret, mounted as a directory of files (never environment
variables): ``ca.crt``, ``tls.crt``, ``tls.key``. This module only reads them.
Standard library only, like the rest of the package; connecting is in
``cloudops_config.temporal``.

The private key is held in memory for as long as the client needs it and is
never part of a repr, a log line or an exception message.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from cloudops_config.errors import ConfigurationError

CA_FILE: Final = "ca.crt"
CERT_FILE: Final = "tls.crt"
KEY_FILE: Final = "tls.key"
_FILES: Final = (CA_FILE, CERT_FILE, KEY_FILE)


@dataclass(frozen=True, slots=True)
class TlsMaterial:
    ca: bytes
    cert: bytes
    key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class TemporalTls:
    """Where the mounted certificate lives, and the name the server must prove."""

    directory: Path
    server_name: str

    def read(self) -> TlsMaterial:
        contents = {}
        for name in _FILES:
            try:
                data = (self.directory / name).read_bytes()
            except OSError:
                raise ConfigurationError(
                    f"the Temporal client certificate is incomplete: {name} is missing "
                    f"from {self.directory}"
                ) from None
            if not data.strip():
                raise ConfigurationError(f"the Temporal client certificate's {name} is empty")
            contents[name] = data
        return TlsMaterial(contents[CA_FILE], contents[CERT_FILE], contents[KEY_FILE])

    def fingerprint(self) -> str:
        """Changes when cert-manager renews the certificate (the key included)."""
        digest = hashlib.sha256()
        for name in _FILES:
            digest.update((self.directory / name).read_bytes())
        return digest.hexdigest()
