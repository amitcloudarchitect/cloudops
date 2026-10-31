"""The ECB's published reference-rate files.

    client = EcbClient(transport=...)
    body = await client.history()

The body comes back untouched; the caller stores it in bronze before anything
parses it. The file is public: there is no credential at all, so none is
reached for (CLAUDE.md rule 2). Only the configured path on the ECB host is
requested; a redirect is not followed. Anything non-2xx raises, and
Temporal's retry policy owns the retries (a five-attempt exponential backoff).
"""

from __future__ import annotations

from typing import Final

import httpx

ECB: Final = "https://www.ecb.europa.eu"
HISTORY_PATH: Final = "/stats/eurofxref/eurofxref-hist.xml"
HISTORY_90D_PATH: Final = "/stats/eurofxref/eurofxref-hist-90d.xml"


class EcbClient:
    def __init__(
        self,
        base_url: str = ECB,
        *,
        path: str = HISTORY_PATH,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not path.startswith("/") or ".." in path:
            raise ValueError("path is an absolute path on the ECB host")
        self.base_url = base_url.rstrip("/")
        self.path = path
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport,
                                       timeout=120.0, follow_redirects=False,
                                       headers={"Accept": "application/xml"})

    async def history(self) -> bytes:
        response = await self._http.get(self.path)
        response.raise_for_status()
        return response.content

    async def aclose(self) -> None:
        await self._http.aclose()
