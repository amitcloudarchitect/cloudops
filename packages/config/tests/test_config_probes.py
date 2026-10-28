"""The probe listener: liveness is unconditional, readiness is the checks, and
neither ever discloses why a dependency failed."""

from __future__ import annotations

import asyncio

import pytest

from cloudops_config.probes import readiness, serve_probes


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("postgresql://user:hunter2@db.internal:5432 refused")


async def _hangs() -> None:
    await asyncio.sleep(30)


async def _get(port: int, raw: bytes) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body.decode()


async def _serve(checks: dict[str, object]) -> tuple[asyncio.Server, int]:
    server = await serve_probes(checks, port=0, seconds=0.2)  # type: ignore[arg-type]
    return server, server.sockets[0].getsockname()[1]


async def test_ready_when_every_check_passes() -> None:
    server, port = await _serve({"database": _ok, "temporal": _ok})
    async with server:
        assert await _get(port, b"GET /readyz HTTP/1.1\r\nHost: x\r\n\r\n") == (
            200, '{"status": "ok"}')


async def test_not_ready_names_the_failures_and_hides_the_cause() -> None:
    server, port = await _serve({"database": _down, "temporal": _ok, "bronze": _hangs})
    async with server:
        status, body = await _get(port, b"GET /readyz HTTP/1.1\r\n\r\n")
    assert status == 503
    assert body == '{"status": "unavailable", "failed": ["bronze", "database"]}'
    assert "hunter2" not in body
    assert "db.internal" not in body


async def test_liveness_never_runs_the_checks() -> None:
    calls: list[str] = []

    async def counted() -> None:
        calls.append("x")
        raise ConnectionError

    server, port = await _serve({"database": counted})
    async with server:
        assert (await _get(port, b"GET /healthz HTTP/1.1\r\n\r\n"))[0] == 200
    assert calls == []


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b"GET /metrics HTTP/1.1\r\n\r\n", 404),
        (b"POST /readyz HTTP/1.1\r\n\r\n", 405),
        (b"garbage\r\n\r\n", 404),
    ],
)
async def test_anything_else_is_refused(raw: bytes, status: int) -> None:
    server, port = await _serve({"database": _ok})
    async with server:
        assert (await _get(port, raw))[0] == status


async def test_no_checks_means_ready_and_order_is_stable() -> None:
    assert await readiness({}) == []
    assert await readiness({"b": _down, "a": _down}, seconds=0.1) == ["a", "b"]
