"""OAuth 2.0 client credentials for the ServiceNow Table API (ADR 0009).

    auth = ClientCredentials(token_url, client_id, read_secret)
    client = ServiceNowClient(instance_url, auth=auth)

The token request goes to the instance's ``/oauth_token.do``. The token is
cached until a minute before it expires; a 401 on a data request refreshes it
once and retries, so a token revoked or rotated server-side recovers without a
restart. ``read_secret`` is called on every token request, so a rotated client
secret (Key Vault, re-mounted by the CSI driver) is picked up the same way.

Neither the secret nor the token is ever logged, put in an exception message,
or kept anywhere but this object: errors carry the HTTP status and ServiceNow's
error code only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Final

import httpx

REFRESH_MARGIN_SECONDS: Final = 60.0


class TokenError(Exception):
    """The instance refused to issue a token. Carries no credential."""


class ClientCredentials(httpx.Auth):
    def __init__(
        self,
        token_url: str,
        client_id: str,
        read_secret: Callable[[], str],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token_url.startswith("https://"):
            raise ValueError("the token endpoint must be https")
        self._token_url = token_url
        self._client_id = client_id
        self._read_secret = read_secret
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def _token_request(self) -> httpx.Request:
        return httpx.Request("POST", self._token_url, data={
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._read_secret(),
        })

    def _accept(self, response: httpx.Response) -> None:
        if response.status_code != 200:
            try:
                code = str(response.json().get("error", "unknown"))[:64]
            except ValueError:
                code = "unparseable"
            raise TokenError(f"token request refused: HTTP {response.status_code} ({code})")
        body = response.json()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise TokenError("token response carried no access_token")
        lifetime = float(body.get("expires_in", 0) or 0)
        self._token = token
        self._expires_at = self._clock() + max(0.0, lifetime - REFRESH_MARGIN_SECONDS)

    def _fresh(self) -> bool:
        return self._token is not None and self._clock() < self._expires_at

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # Overriding the async flow means httpx does not read the token response's
        # body for us (requires_response_body only applies to auth_flow): over a real
        # connection it arrives streamed, so read it before parsing.
        async with self._lock:
            if not self._fresh():
                token_response = yield self._token_request()
                await token_response.aread()
                self._accept(token_response)
            request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code == 401:
            async with self._lock:
                token_response = yield self._token_request()
                await token_response.aread()
                self._accept(token_response)
                request.headers["Authorization"] = f"Bearer {self._token}"
            yield request

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        # httpx's default would send the request with no credentials at all.
        raise RuntimeError("ClientCredentials is async only")
