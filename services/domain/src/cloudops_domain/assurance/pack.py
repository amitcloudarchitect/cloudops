"""The signed evidence pack, per customer and framework (HLD M15).

    manifest = build_manifest(...)
    signed = signer.sign(manifest)        # SignedPack: manifest, digest, signature
    verify(signed, public_key)            # anyone holding the public key

The manifest is canonical JSON (sorted keys, no insignificant whitespace,
UTF-8): the assessment, the library version, every response with its
approval, every evidence row with its query, raw value, retrieval timestamp
and digest, and the score. The signature is over the SHA-256 of exactly
those bytes, so a recipient can check the pack has not changed since export.

**Which key.** The LLD names no evidence-signing key (its Key Vault Premium
keys are runbook, transcript, database and token signing). Until one is
decided (ADR 0019), the local profile signs with an Ed25519 key from the local
secrets file, and every other environment refuses to export: an unsigned or
self-asserted pack would be worse than none.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from cloudops_domain.assurance.evidence import canonical
from cloudops_domain.assurance.scoring import Score

FORMAT: Final = "cloudops.evidence-pack/1"
ALGORITHM: Final = "Ed25519"


class SigningUnavailableError(Exception):
    """No evidence-signing key is configured here. Safe to report."""


@dataclass(frozen=True)
class SignedPack:
    manifest: dict[str, Any]
    manifest_sha256: str
    signature: str  # base64
    algorithm: str
    key_id: str

    def as_document(self) -> dict[str, Any]:
        return {"manifest": self.manifest, "manifest_sha256": self.manifest_sha256,
                "signature": {"algorithm": self.algorithm, "key_id": self.key_id,
                              "value": self.signature}}


class Signer(Protocol):
    key_id: str

    def sign(self, manifest: Mapping[str, Any]) -> SignedPack: ...


def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return canonical(manifest).encode("utf-8")


class Ed25519Signer:
    """Signs with an Ed25519 private key held in memory (local profile only)."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        public = key.public_key().public_bytes(serialization.Encoding.Raw,
                                               serialization.PublicFormat.Raw)
        self.key_id = "ed25519:" + hashlib.sha256(public).hexdigest()[:16]

    @classmethod
    def from_pem(cls, private_pem: bytes) -> Ed25519Signer:
        key = serialization.load_pem_private_key(private_pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("the evidence-signing key must be Ed25519")
        return cls(key)

    @classmethod
    def from_seed(cls, seed_b64: str) -> Ed25519Signer:
        """The local secrets file holds the 32-byte seed, base64 encoded."""
        seed = base64.b64decode(seed_b64.strip(), validate=True)
        if len(seed) != 32:
            raise ValueError("an Ed25519 seed is 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

    def sign(self, manifest: Mapping[str, Any]) -> SignedPack:
        data = manifest_bytes(manifest)
        digest = hashlib.sha256(data).hexdigest()
        signature = base64.b64encode(self._key.sign(digest.encode("ascii"))).decode("ascii")
        return SignedPack(dict(manifest), digest, signature, ALGORITHM, self.key_id)


class UnavailableSigner:
    """Every environment without a decided key: refuses, never signs."""

    key_id = "none"

    def sign(self, manifest: Mapping[str, Any]) -> SignedPack:
        raise SigningUnavailableError("no evidence-signing key is configured")


def verify(document: Mapping[str, Any], public_key: Ed25519PublicKey) -> bool:
    """Check a pack as exported: the digest matches the manifest, and the
    signature matches the digest."""
    manifest = document.get("manifest")
    signature = document.get("signature")
    if not isinstance(manifest, Mapping) or not isinstance(signature, Mapping):
        return False
    digest = hashlib.sha256(manifest_bytes(manifest)).hexdigest()
    if digest != document.get("manifest_sha256"):
        return False
    try:
        public_key.verify(base64.b64decode(str(signature.get("value", ""))),
                          digest.encode("ascii"))
    except (InvalidSignature, ValueError):
        return False
    return True


def build_manifest(*, assessment: Mapping[str, Any], responses: Sequence[Mapping[str, Any]],
                   evidence: Sequence[Mapping[str, Any]], score: Score, exported_at: datetime,
                   exported_by: UUID) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "exported_at": exported_at.isoformat(),
        "exported_by": str(exported_by),
        "assessment": dict(assessment),
        "score": {
            "percent": None if score.percent is None else str(score.percent),
            "points": score.points, "scorable": score.scorable, "counts": score.counts,
            "pending": score.pending, "excluded": score.excluded,
            "rule": "compliant 100, partially compliant 50, non-compliant 0; manual and "
                    "not applicable excluded; unapproved responses pending, not scored",
        },
        "responses": [dict(r) for r in responses],
        "evidence": [dict(e) for e in evidence],
    }
