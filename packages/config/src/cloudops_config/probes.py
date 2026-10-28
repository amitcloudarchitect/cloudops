"""Liveness and readiness probes for processes that serve no HTTP of their own.

    server = await serve_probes({"database": db.ping, "temporal": temporal_ready(client)})

``GET /healthz`` answers 200 whenever the event loop does, and nothing else:
it must stay cheap, because a slow liveness probe restarts healthy pods.
``GET /readyz`` runs every dependency check at once, each with a timeout, and
answers 503 naming the checks that failed. It never includes an exception's
text, which can carry hosts or connection details.

Standard library only, so every service can use it without new dependencies.
The port is for the kubelet, never for the ingress.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

PROBE_PORT: Final = 8080
CHECK_TIMEOUT_SECONDS: Final = 2.0
_MAX_REQUEST_BYTES: Final = 8192

Check = Callable[[], Awaitable[None]]
"""Returns when the dependency is usable; raises (anything) when it is not."""


async def _run(check: Check, seconds: float) -> bool:
    try:
        await asyncio.wait_for(check(), seconds)
    except Exception:  # any failure means not ready; the cause is never disclosed
        return False
    return True


async def readiness(checks: Mapping[str, Check],
                    seconds: float = CHECK_TIMEOUT_SECONDS) -> list[str]:
    """The names of the checks that failed, sorted; empty when ready."""
    names = sorted(checks)
    results = await asyncio.gather(*(_run(checks[n], seconds) for n in names))
    return [n for n, ok in zip(names, results, strict=True) if not ok]


def _response(status: int, body: Mapping[str, object]) -> bytes:
    payload = json.dumps(body).encode()
    reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed",
              503: "Service Unavailable"}[status]
    head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n")
    return head.encode() + payload


async def _answer(request_line: str, checks: Mapping[str, Check], seconds: float) -> bytes:
    parts = request_line.split()
    if len(parts) != 3:
        return _response(404, {"status": "not_found"})
    method, path = parts[0], parts[1]
    if path not in ("/healthz", "/readyz"):
        return _response(404, {"status": "not_found"})
    if method != "GET":
        return _response(405, {"status": "method_not_allowed"})
    if path == "/healthz":
        return _response(200, {"status": "ok"})
    failed = await readiness(checks, seconds)
    if failed:
        return _response(503, {"status": "unavailable", "failed": failed})
    return _response(200, {"status": "ok"})


async def serve_probes(
    checks: Mapping[str, Check],
    *,
    host: str = "127.0.0.1",
    port: int = PROBE_PORT,
    seconds: float = CHECK_TIMEOUT_SECONDS,
) -> asyncio.Server:
    """Start the probe listener; close the returned server to stop it."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), seconds)
            if len(head) > _MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            line = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
            writer.write(await _answer(line, checks, seconds))
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError,
                ValueError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port, limit=_MAX_REQUEST_BYTES)
