"""The Temporal connection helper (ADR 0010): a renewed client certificate stops
the worker gracefully, so the pod restarts with it; nothing else does."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cloudops_config import TemporalConfig, TemporalTls
from cloudops_config.temporal import run_worker


class FakeWorker:
    def __init__(self) -> None:
        self.stopped = asyncio.Event()
        self.shutdowns = 0

    async def run(self) -> None:
        await self.stopped.wait()

    async def shutdown(self) -> None:
        self.shutdowns += 1
        self.stopped.set()


def _tls(directory: Path) -> TemporalConfig:
    directory.mkdir(exist_ok=True)
    for name in ("ca.crt", "tls.crt", "tls.key"):
        (directory / name).write_text(f"{name} v1", encoding="utf-8")
    return TemporalConfig("temporal-frontend.temporal.svc.cluster.local:7233", "default",
                          TemporalTls(directory, "temporal-frontend.temporal.svc.cluster.local"))


async def test_a_renewed_certificate_shuts_the_worker_down(tmp_path: Path) -> None:
    config, worker = _tls(tmp_path / "tls"), FakeWorker()
    task = asyncio.create_task(run_worker(worker, config, seconds=0.01))
    await asyncio.sleep(0.05)
    assert not task.done()  # unchanged: keeps serving
    (tmp_path / "tls" / "tls.key").write_text("tls.key v2", encoding="utf-8")
    await asyncio.wait_for(task, 1)
    assert worker.shutdowns == 1


async def test_a_volume_mid_update_is_not_a_renewal(tmp_path: Path) -> None:
    config, worker = _tls(tmp_path / "tls"), FakeWorker()
    task = asyncio.create_task(run_worker(worker, config, seconds=0.01))
    await asyncio.sleep(0.02)  # the watch has its first fingerprint
    key = tmp_path / "tls" / "tls.key"
    key.unlink()
    await asyncio.sleep(0.05)
    assert not task.done()
    key.write_text("tls.key v1", encoding="utf-8")  # back as it was
    await asyncio.sleep(0.05)
    assert not task.done()
    await worker.shutdown()
    await asyncio.wait_for(task, 1)


async def test_without_tls_the_worker_just_runs() -> None:
    worker = FakeWorker()
    config = TemporalConfig("127.0.0.1:7233", "default", None)
    task = asyncio.create_task(run_worker(worker, config, seconds=0.01))
    await asyncio.sleep(0.05)
    assert not task.done()
    await worker.shutdown()
    await asyncio.wait_for(task, 1)
    assert worker.shutdowns == 1


async def test_the_watch_stops_with_the_worker(tmp_path: Path) -> None:
    config, worker = _tls(tmp_path / "tls"), FakeWorker()
    task = asyncio.create_task(run_worker(worker, config, seconds=0.01))
    await asyncio.sleep(0.02)
    await worker.shutdown()
    await asyncio.wait_for(task, 1)
    (tmp_path / "tls" / "tls.key").write_text("tls.key v2", encoding="utf-8")
    await asyncio.sleep(0.05)
    assert worker.shutdowns == 1  # no second shutdown from a leftover watch


@pytest.mark.parametrize("name", ["ca.crt", "tls.crt", "tls.key"])
def test_an_empty_file_is_refused(tmp_path: Path, name: str) -> None:
    config = _tls(tmp_path / "tls")
    (tmp_path / "tls" / name).write_text("  \n", encoding="utf-8")
    assert config.tls is not None
    with pytest.raises(Exception, match="empty"):
        config.tls.read()
