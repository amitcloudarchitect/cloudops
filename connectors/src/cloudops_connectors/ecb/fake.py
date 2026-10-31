"""A fake ECB host serving the recorded history files through httpx.

    fake = FakeEcb.from_fixtures()
    client = EcbClient(FIXTURE_HOST, transport=fake.transport())
    fake.revise()          # the ECB has since revised a published rate

It answers the history paths with the recorded file, 404 elsewhere, and 503
on demand (``fail_next``).
"""

from __future__ import annotations

from importlib import resources
from typing import Final

import httpx

from cloudops_connectors.ecb.client import HISTORY_90D_PATH, HISTORY_PATH

FIXTURE_HOST: Final = "https://ecb.fixture.example"
HIST: Final = "eurofxref-hist.xml"
HIST_REVISED: Final = "eurofxref-hist-revised.xml"


def load_fixture(name: str) -> bytes:
    return resources.files("cloudops_connectors.ecb").joinpath(f"fixtures/{name}").read_bytes()


class FakeEcb:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.fail_next = 0
        self.requests: list[httpx.Request] = []

    @classmethod
    def from_fixtures(cls) -> FakeEcb:
        return cls(load_fixture(HIST))

    def revise(self) -> None:
        """The file as the ECB publishes it after revising a past rate."""
        self.body = load_fixture(HIST_REVISED)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(503, content=b"Service Unavailable")
        if request.method != "GET" or request.url.path not in (HISTORY_PATH, HISTORY_90D_PATH):
            return httpx.Response(404, content=b"Not Found")
        return httpx.Response(200, headers={"Content-Type": "text/xml"}, content=self.body)
