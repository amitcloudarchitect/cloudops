"""Bearer token validation (request lifecycle, stage 2).

The token proves *who* is calling, and nothing more. What we take from it:

    oid     the Entra object id — the key into access data (ADR 0002, D1)

What we check before trusting even that:

    signature  RS256 only, by a key the issuer publishes, chosen by ``kid``.
               No ``none``, no HMAC (an RSA public key must never become an
               HMAC secret), no key embedded in the token.
    iss        exactly the configured issuer
    aud        exactly the API's audience
    tid        exactly the Getronics directory — B2B guests are issued there too
    exp/nbf/iat  present and current, 60 s leeway for clock skew
    scp        the delegated scope the API exposes (``access_as_user``)

Every other claim — roles, groups, an organisation or tenant claim, any
extension attribute — is ignored. Scope comes from the database (access.py).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Final, Protocol
from uuid import UUID

import httpx
import jwt
from jwt import PyJWK

from cloudops_config import AuthConfig

ALGORITHM: Final = "RS256"
LEEWAY_SECONDS: Final = 60
REQUIRED_CLAIMS: Final = ["exp", "nbf", "iat", "iss", "aud", "oid", "tid"]


class TokenError(Exception):
    """The token cannot be trusted. ``code`` is safe to return to the client."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Identity:
    oid: UUID

    @property
    def principal_id(self) -> str:
        return f"user:{self.oid}"


class KeySource(Protocol):
    async def key(self, kid: str) -> Any | None:
        """The verification key for ``kid``, or None if the issuer has no such key."""
        ...


def _keys_from_jwks(document: dict[str, Any]) -> dict[str, Any]:
    keys: dict[str, Any] = {}
    for entry in document.get("keys", []):
        # Only RSA signing keys. A JWKS entry for another type or use is skipped,
        # so it can never be selected for RS256 verification.
        if entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig":
            continue
        kid = entry.get("kid")
        if isinstance(kid, str) and kid:
            keys[kid] = PyJWK(entry, algorithm=ALGORITHM).key
    return keys


class StaticKeys:
    """A fixed JWKS: the local dev issuer, and tests."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        self._keys = _keys_from_jwks(jwks)

    @classmethod
    def from_json(cls, text: str) -> StaticKeys:
        return cls(json.loads(text))

    async def key(self, kid: str) -> Any | None:
        return self._keys.get(kid)


class RemoteKeys:
    """Entra's published JWKS, cached. An unknown ``kid`` triggers one refetch
    (keys roll over), at most once per ``min_refresh`` seconds so a stream of
    tokens with invented kids cannot turn the API into a fetch amplifier."""

    def __init__(
        self, url: str, *, ttl: float = 3600.0, min_refresh: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not url.startswith("https://"):
            raise ValueError("JWKS must be fetched over https")
        self._url = url
        self._ttl = ttl
        self._min_refresh = min_refresh
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._keys: dict[str, Any] = {}
        # None until the first fetch. Not 0.0: the monotonic clock starts at boot,
        # so on a node up for less than min_refresh a zero would read as "fetched
        # just now" and the keys would never be fetched at all.
        self._fetched: float | None = None
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        response = await self._client.get(self._url)
        response.raise_for_status()
        self._keys = _keys_from_jwks(response.json())
        self._fetched = time.monotonic()

    async def key(self, kid: str) -> Any | None:
        async with self._lock:
            if self._fetched is None:
                await self._refresh()
                return self._keys.get(kid)
            age = time.monotonic() - self._fetched
            if age > self._ttl or (kid not in self._keys and age > self._min_refresh):
                await self._refresh()
            return self._keys.get(kid)


class TokenValidator:
    def __init__(self, config: AuthConfig, keys: KeySource) -> None:
        self._config = config
        self._keys = keys

    async def validate(self, token: str) -> Identity:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenError("malformed_token") from None
        # Checked before any key lookup: the header is attacker-controlled.
        if header.get("alg") != ALGORITHM:
            raise TokenError("unsupported_algorithm")
        if any(h in header for h in ("jwk", "jku", "x5u", "x5c")):
            raise TokenError("embedded_key")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenError("missing_key_id")
        key = await self._keys.key(kid)
        if key is None:
            raise TokenError("unknown_key")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": REQUIRED_CLAIMS, "verify_signature": True},
            )
        except jwt.ExpiredSignatureError:
            raise TokenError("token_expired") from None
        except jwt.ImmatureSignatureError:
            raise TokenError("token_not_yet_valid") from None
        except jwt.InvalidAudienceError:
            raise TokenError("wrong_audience") from None
        except jwt.InvalidIssuerError:
            raise TokenError("wrong_issuer") from None
        except jwt.PyJWTError:
            raise TokenError("invalid_token") from None
        if claims.get("tid") != self._config.tenant_id:
            raise TokenError("wrong_directory")
        scopes = claims.get("scp")
        if not isinstance(scopes, str) or self._config.required_scope not in scopes.split(" "):
            raise TokenError("missing_scope")
        try:
            oid = UUID(str(claims["oid"]))
        except ValueError:
            raise TokenError("invalid_subject") from None
        return Identity(oid)
