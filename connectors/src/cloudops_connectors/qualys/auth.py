"""HTTP Basic authentication for the Qualys API, as the API user (ADR 0028).

    auth = ApiUser(base_url, username, read_password)
    client = QualysClient(base_url, auth=auth)

Qualys' v2 API takes the API user's name and password on every request (with
``X-Requested-With``, which the client sends). ``read_password`` is called for each
request, so a password rotated in Key Vault (re-mounted by the CSI driver) is used
at once. The credential is sent only over https and only to the configured API
server: a request anywhere else is refused before anything is attached. Neither
value is logged or put in an exception message.

The API user is read-only by its Qualys role (Reader, API access only); the client
calls ``action=list`` on the detection and KnowledgeBase paths and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable, Generator

import httpx


class ForeignHostError(ValueError):
    """A request for a host the credential is not for. Never sent."""


class ApiUser(httpx.Auth):
    def __init__(self, base_url: str, username: str, read_password: Callable[[], str]) -> None:
        origin = httpx.URL(base_url)
        if origin.scheme != "https" or not origin.host:
            raise ValueError("the Qualys credential is only sent over https")
        self._origin = (origin.scheme, origin.host, origin.port)
        self._username = username
        self._read_password = read_password

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if (request.url.scheme, request.url.host, request.url.port) != self._origin:
            raise ForeignHostError("refusing to send the Qualys credential to another host")
        basic = httpx.BasicAuth(self._username, self._read_password())
        yield from basic.auth_flow(request)
