"""Connecting to Temporal, with mutual TLS wherever the profile says so (ADR 0010).

    temporal = await connect(settings.temporal)
    ...
    await run_worker(worker, settings.temporal)

Needs ``temporalio`` (the ``temporal`` extra); only workers import this module.

The SDK cannot swap TLS material on a live client, so ``run_worker`` shuts the
worker down gracefully when cert-manager renews the mounted certificate and the
process exits; Kubernetes restarts the container, which connects with the new
one. Renewal is weeks before expiry, so a missed check is harmless.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Sequence
from typing import Final, Protocol

from temporalio.client import Client, Interceptor, TLSConfig
from temporalio.runtime import Runtime

from cloudops_config.settings import TemporalConfig

ROTATION_CHECK_SECONDS: Final = 300.0


async def connect(config: TemporalConfig, *, interceptors: Sequence[Interceptor] = (),
                  runtime: Runtime | None = None) -> Client:
    """``interceptors`` and ``runtime`` carry tracing and the SDK's metrics
    (``cloudops_observability.temporal``); both empty when telemetry is off."""
    tls: TLSConfig | bool = False
    if config.tls is not None:
        material = config.tls.read()
        tls = TLSConfig(
            server_root_ca_cert=material.ca,
            client_cert=material.cert,
            client_private_key=material.key,
            domain=config.tls.server_name,
        )
    return await Client.connect(config.address, namespace=config.namespace, tls=tls,
                                interceptors=interceptors, runtime=runtime)


class Runnable(Protocol):
    async def run(self) -> None: ...
    async def shutdown(self) -> None: ...


async def _shutdown_on_rotation(config: TemporalConfig, worker: Runnable,
                                seconds: float) -> None:
    if config.tls is None:
        return
    loaded = config.tls.fingerprint()
    while True:
        await asyncio.sleep(seconds)
        try:
            current = config.tls.fingerprint()
        except OSError:
            continue  # the volume is mid-update; look again next time
        if current != loaded:
            print("temporal client certificate renewed; stopping to reload it", file=sys.stderr)
            await worker.shutdown()
            return


async def run_worker(worker: Runnable, config: TemporalConfig, *,
                     seconds: float = ROTATION_CHECK_SECONDS) -> None:
    """Run until stopped, or until the client certificate is renewed."""
    watch = asyncio.create_task(_shutdown_on_rotation(config, worker, seconds))
    try:
        await worker.run()
    finally:
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch
