"""AWS Signature Version 4, as an ``httpx.Auth``, for a customer's stored access key.

    keys = lambda: AccessKey(store.get(id_name), store.get(secret_name))
    ce = CostExplorerClient(account, auth=SigV4Auth(keys, "us-east-1", "ce", CE_HOST))

ADR 0003 (stored, read-only customer credentials until production): the key pair
is a Key Vault secret read through the ``SecretStore`` at each request, so a
rotated key is used from the next call without a restart. The key never appears
in an exception, a log line or bronze metadata; only the request carries it,
as the signature.

- **One service, one host.** Each instance signs for one region, one service and
  one origin (https, host, port). A request elsewhere is refused before it is
  signed, so a redirect cannot carry a signature away (as ``azure/auth.py``).
- Signs ``host``, ``x-amz-date``, and ``content-type`` and ``x-amz-target`` when
  present, over the SHA-256 of the body (the request body is read first).
- The canonical request follows the published algorithm; ``tests`` checks it
  against AWS's own worked example.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import parse_qsl, quote

import httpx

ALGORITHM: Final = "AWS4-HMAC-SHA256"
_SIGNED: Final = ("content-type", "host", "x-amz-date", "x-amz-target")


class ForeignHostError(ValueError):
    """A request for a host this signer is not for. Never signed, never sent."""


@dataclass(frozen=True, slots=True)
class AccessKey:
    key_id: str
    secret: str

    def __repr__(self) -> str:  # never the secret, in any traceback
        return f"AccessKey(key_id={self.key_id!r}, secret=<hidden>)"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _quote(value: str) -> str:
    return quote(value, safe="-_.~")


def canonical_query(raw: str) -> str:
    pairs = parse_qsl(raw, keep_blank_values=True)
    return "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(pairs))


def signature(key: AccessKey, request: httpx.Request, region: str, service: str,
              when: datetime) -> tuple[str, str]:
    """``(Authorization, x-amz-date)`` for the request as it stands."""
    stamp = when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    headers = {"host": request.url.netloc.decode(), "x-amz-date": stamp}
    for name in ("content-type", "x-amz-target"):
        if name in request.headers:
            headers[name] = " ".join(request.headers[name].split())
    names = sorted(h for h in headers if h in _SIGNED)
    canonical = "\n".join([
        request.method,
        quote(request.url.path or "/", safe="/-_.~"),
        canonical_query(request.url.query.decode()),
        "".join(f"{n}:{headers[n]}\n" for n in names),
        ";".join(names),
        hashlib.sha256(request.content).hexdigest(),
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, stamp, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    signing = _hmac(_hmac(_hmac(_hmac(f"AWS4{key.secret}".encode(), day), region), service),
                    "aws4_request")
    sig = hmac.new(signing, to_sign.encode(), hashlib.sha256).hexdigest()
    return (f"{ALGORITHM} Credential={key.key_id}/{scope}, SignedHeaders={';'.join(names)}, "
            f"Signature={sig}"), stamp


class SigV4Auth(httpx.Auth):
    requires_request_body = True

    def __init__(self, keys: Callable[[], AccessKey], region: str, service: str,
                 base_url: str, *, clock: Callable[[], datetime] | None = None) -> None:
        origin = httpx.URL(base_url)
        if origin.scheme != "https" or not origin.host:
            raise ValueError("a signed AWS request is only sent over https")
        self._keys = keys
        self._region = region
        self._service = service
        self._origin = (origin.scheme, origin.host, origin.port)
        self._clock = clock or (lambda: datetime.now(UTC))

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if (request.url.scheme, request.url.host, request.url.port) != self._origin:
            raise ForeignHostError("refusing to sign a request for another host")
        authorization, stamp = signature(self._keys(), request, self._region, self._service,
                                         self._clock())
        request.headers["x-amz-date"] = stamp
        request.headers["Authorization"] = authorization
        yield request
