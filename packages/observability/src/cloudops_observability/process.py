"""One call at the top of each service's ``main``.

    telemetry = start("worker-triage", os.environ)
    temporal = await connect(settings.temporal, **temporal_options(telemetry))
    try: ... finally: telemetry.shutdown()

Logging, traces and metrics (``setup.init``) and client spans on every
outbound httpx call. Database spans come from ``packages/tenancy`` itself, which
owns the engine. Needs the ``httpx`` extra; ``temporal_options`` needs the
``temporal`` extra.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cloudops_observability.setup import Telemetry, init

_started: Telemetry | None = None


def start(service: str, environ: Mapping[str, str]) -> Telemetry:
    global _started
    telemetry = init(service, environ)
    _started = telemetry
    if telemetry.enabled:
        from cloudops_observability.http import instrument_httpx

        instrument_httpx()
    return telemetry


def temporal_options(telemetry: Telemetry | None = None) -> dict[str, Any]:
    """Keyword arguments for ``cloudops_config.temporal.connect``: for
    ``telemetry``, else the one ``start`` began; none if nothing started."""
    from cloudops_observability.temporal import interceptors, runtime

    t = telemetry or _started
    if t is None:
        return {}
    return {"interceptors": interceptors(t), "runtime": runtime(t)}


__all__ = ["start", "temporal_options"]
