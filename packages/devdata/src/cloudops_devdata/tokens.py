"""Dev tokens for the local issuer. Local profile and tests only.

    CLOUDOPS_ENV=local python -m cloudops_devdata.tokens --oid <uuid>

Mints an RS256 access token shaped like Entra's v2 tokens, signed by the dev
key in .env.local (``api-dev-signing-jwk``). The local API trusts that key
because its public half is ``api-dev-jwks``; Entra never issues for the dev
issuer, tenant or audience, so these tokens are worthless anywhere else.

Lives here, not in cloudops_api, so production code contains no way to sign
a token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from cloudops_config import AuthConfig, Component, ConfigurationError, Environment, load_settings

DEV_KID = "cloudops-dev-1"


def new_signing_key(kid: str = DEV_KID) -> tuple[dict[str, Any], dict[str, Any]]:
    """A fresh RSA key: (private JWK, public JWKS)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key))
    public: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    private.update(kid=kid, alg="RS256", use="sig")
    public.update(kid=kid, alg="RS256", use="sig")
    return private, {"keys": [public]}


def mint(
    private_jwk: Mapping[str, Any],
    auth: AuthConfig,
    oid: UUID | str,
    *,
    lifetime: int = 600,
    now: int | None = None,
    claims: Mapping[str, Any] | None = None,
    headers: Mapping[str, Any] | None = None,
    remove: Iterable[str] = (),
) -> str:
    """An access token for ``oid``. ``claims``/``headers``/``remove`` let tests
    tamper with it."""
    issued = int(time.time()) if now is None else now
    body: dict[str, Any] = {
        "iss": auth.issuer,
        "aud": auth.audience,
        "tid": auth.tenant_id,
        "oid": str(oid),
        "sub": str(oid),
        "scp": auth.required_scope,
        "iat": issued,
        "nbf": issued,
        "exp": issued + lifetime,
        "ver": "2.0",
    }
    body.update(claims or {})
    for name in remove:
        body.pop(name, None)
    key = jwt.PyJWK(dict(private_jwk), algorithm="RS256").key
    head = {"kid": private_jwk.get("kid", DEV_KID), **(headers or {})}
    return jwt.encode(body, key, algorithm="RS256", headers=head)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_devdata.tokens")
    parser.add_argument("--oid", type=UUID, required=True, help="Entra object id to act as")
    parser.add_argument("--lifetime", type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(os.environ, require={Component.AUTH})
        if settings.environment is not Environment.LOCAL:
            raise ConfigurationError("dev tokens are local-only")
        private = json.loads(settings.secrets.get("api-dev-signing-jwk"))
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(mint(private, settings.auth, args.oid, lifetime=args.lifetime))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
