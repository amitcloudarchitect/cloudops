"""Token validation: every way a token can lie, refused with its own code.

No database. Keys are generated per session; tokens are minted with the same
helper the local dev issuer uses, then tampered with.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from cloudops_api.auth import RemoteKeys, StaticKeys, TokenError, TokenValidator
from cloudops_config import AuthConfig
from cloudops_devdata.tokens import mint, new_signing_key

AUTH = AuthConfig(
    issuer="https://login.example.test/11111111-1111-4111-8111-111111111111/v2.0",
    audience="api://cloudops-test",
    tenant_id="11111111-1111-4111-8111-111111111111",
    required_scope="access_as_user",
    jwks_url=None,
    jwks_secret=None,
)
OID = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture(scope="module")
def key() -> tuple[dict[str, Any], dict[str, Any]]:
    return new_signing_key("k1")


@pytest.fixture(scope="module")
def validator(key: tuple[dict[str, Any], dict[str, Any]]) -> TokenValidator:
    return TokenValidator(AUTH, StaticKeys(key[1]))


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


async def _code(validator: TokenValidator, token: str) -> str:
    with pytest.raises(TokenError) as info:
        await validator.validate(token)
    return info.value.code


async def test_a_good_token_yields_only_the_object_id(
    validator: TokenValidator, key: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    token = mint(
        key[0], AUTH, OID,
        claims={"roles": ["platform_admin"], "tenant_id": str(uuid4()), "organisation": "x"},
    )
    identity = await validator.validate(token)
    assert identity.oid == OID
    assert identity.principal_id == f"user:{OID}"
    assert set(vars(identity) if hasattr(identity, "__dict__") else identity.__slots__) == {"oid"}


async def test_unsigned_token_is_refused(
    validator: TokenValidator, key: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    claims = jwt.decode(mint(key[0], AUTH, OID), options={"verify_signature": False})
    token = f"{_b64({'alg': 'none', 'kid': 'k1'})}.{_b64(claims)}."
    assert await _code(validator, token) == "unsupported_algorithm"


async def test_hmac_with_the_public_key_as_secret_is_refused(
    validator: TokenValidator, key: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    """The classic algorithm-confusion attack: sign HS256 with the public key."""
    public = jwt.PyJWK(key[1]["keys"][0]).key
    pem = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    claims = jwt.decode(mint(key[0], AUTH, OID), options={"verify_signature": False})
    # PyJWT will not create this token, which is the point; build it by hand.
    signing_input = f"{_b64({'alg': 'HS256', 'typ': 'JWT', 'kid': 'k1'})}.{_b64(claims)}"
    mac = hmac.new(pem, signing_input.encode(), hashlib.sha256).digest()
    forged = f"{signing_input}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    assert await _code(validator, forged) == "unsupported_algorithm"


async def test_signature_by_another_key_with_the_same_kid_is_refused(
    validator: TokenValidator,
) -> None:
    attacker, _ = new_signing_key("k1")
    assert await _code(validator, mint(attacker, AUTH, OID)) == "invalid_token"


async def test_tampered_payload_fails_the_signature(
    validator: TokenValidator, key: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    head, _, sig = mint(key[0], AUTH, OID).split(".")
    claims = jwt.decode(mint(key[0], AUTH, OID), options={"verify_signature": False})
    claims["oid"] = str(uuid4())
    assert await _code(validator, f"{head}.{_b64(claims)}.{sig}") == "invalid_token"


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"kid": "nope"}, "unknown_key"),
        ({"kid": ""}, "missing_key_id"),
        ({"jwk": {"kty": "RSA"}}, "embedded_key"),
        ({"jku": "https://evil.example/keys"}, "embedded_key"),
        ({"x5u": "https://evil.example/cert"}, "embedded_key"),
    ],
)
async def test_key_selection_is_never_the_tokens_choice(
    validator: TokenValidator, key: tuple[dict[str, Any], dict[str, Any]],
    headers: dict[str, Any], code: str,
) -> None:
    assert await _code(validator, mint(key[0], AUTH, OID, headers=headers)) == code


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"iss": "https://login.microsoftonline.com/evil/v2.0"}, "wrong_issuer"),
        ({"aud": "api://someone-else"}, "wrong_audience"),
        ({"tid": "33333333-3333-4333-8333-333333333333"}, "wrong_directory"),
        ({"scp": "User.Read"}, "missing_scope"),
        ({"scp": "access_as_user_and_more"}, "missing_scope"),
        ({"scp": ["access_as_user"]}, "missing_scope"),
        ({"oid": "not-a-uuid"}, "invalid_subject"),
    ],
)
async def test_claims_must_match_exactly(
    validator: TokenValidator, key: tuple[dict[str, Any], dict[str, Any]],
    claims: dict[str, Any], code: str,
) -> None:
    assert await _code(validator, mint(key[0], AUTH, OID, claims=claims)) == code


@pytest.mark.parametrize("missing", ["exp", "nbf", "iat", "iss", "aud", "oid", "tid", "scp"])
async def test_required_claims_cannot_be_omitted(
    validator: TokenValidator, key: tuple[dict[str, Any], dict[str, Any]], missing: str
) -> None:
    await _code(validator, mint(key[0], AUTH, OID, remove=[missing]))


async def test_expiry_and_not_before_allow_one_minute_of_skew(
    validator: TokenValidator, key: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    now = int(time.time())
    expired = mint(key[0], AUTH, OID, now=now - 700, lifetime=600)  # 100 s ago
    assert await _code(validator, expired) == "token_expired"
    future = mint(key[0], AUTH, OID, now=now + 300)
    assert await _code(validator, future) == "token_not_yet_valid"
    skewed = mint(key[0], AUTH, OID, now=now - 630, lifetime=600)  # 30 s ago
    assert (await validator.validate(skewed)).oid == OID


@pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c", "Bearer x.y.z"])
async def test_garbage_is_malformed(validator: TokenValidator, token: str) -> None:
    assert await _code(validator, token) in {"malformed_token", "invalid_token"}


def test_jwks_entries_that_are_not_rsa_signing_keys_are_ignored(
    key: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    enc = {**key[1]["keys"][0], "kid": "enc", "use": "enc"}
    oct_key = {"kty": "oct", "kid": "sym", "k": "c2VjcmV0"}
    keys = StaticKeys({"keys": [enc, oct_key, key[1]["keys"][0]]})
    assert keys._keys.keys() == {"k1"}


def test_remote_keys_must_be_https() -> None:
    with pytest.raises(ValueError, match="https"):
        RemoteKeys("http://login.microsoftonline.com/x/discovery/v2.0/keys")


async def test_unknown_kids_cannot_make_us_refetch_in_a_loop(
    key: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    fetches = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal fetches
        fetches += 1
        return httpx.Response(200, json=key[1])

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    keys = RemoteKeys("https://keys.example.test/jwks", client=client, min_refresh=300)
    assert await keys.key("k1") is not None
    for i in range(20):
        assert await keys.key(f"invented-{i}") is None
    assert fetches == 1
