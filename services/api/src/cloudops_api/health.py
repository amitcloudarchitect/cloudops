"""Probes: ``/healthz`` (liveness, Front Door's origin probe) and ``/readyz``.

The only routes outside the audited pipeline, and deliberately data-free: no
token, no scope, no tenant data, and a fixed body whatever happens. ``/readyz``
proves the database answers, under an empty scope that sees no tenant rows.
They are served from ``PROBES``, never from ``ROUTERS``, so ``check_router``
still holds for every route that touches data.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cloudops_tenancy import Database

READY_TIMEOUT_SECONDS = 2.0

PROBES = APIRouter(include_in_schema=False)


@PROBES.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@PROBES.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    db: Database = request.app.state.platform.db
    try:
        await asyncio.wait_for(db.ping(), READY_TIMEOUT_SECONDS)
    except Exception:  # any failure means not ready; the cause is never disclosed
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})

