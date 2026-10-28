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
from sqlalchemy import text

from cloudops_tenancy import Database, TenantScope

READY_TIMEOUT_SECONDS = 2.0
_PROBE_PRINCIPAL = "probe:readyz"

PROBES = APIRouter(include_in_schema=False)


@PROBES.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@PROBES.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    db: Database = request.app.state.platform.db
    try:
        await asyncio.wait_for(_database_answers(db), READY_TIMEOUT_SECONDS)
    except Exception:  # any failure means not ready; the cause is never disclosed
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


async def _database_answers(db: Database) -> None:
    async with db.transaction(TenantScope.for_resolution(_PROBE_PRINCIPAL)) as conn:
        await conn.scalar(text("SELECT 1"))
