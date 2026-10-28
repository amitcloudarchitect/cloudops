"""Application factory.

    app = create_app(db=db, validator=validator)

Dependencies are passed in, never discovered: the factory reads no
environment. ``__main__`` wires them from the configuration layer.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from cloudops_api import bff
from cloudops_api.access import ScopeResolver
from cloudops_api.auth import TokenValidator
from cloudops_api.health import PROBES
from cloudops_api.pipeline import AuditedRoute, Platform
from cloudops_tenancy import Database


def create_app(*, db: Database, validator: TokenValidator, expose_openapi: bool = False) -> FastAPI:
    app = FastAPI(
        title="CloudOps Platform API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        # The schema drives generated TypeScript types; served locally only.
        openapi_url="/openapi.json" if expose_openapi else None,
    )
    app.state.platform = Platform(db=db, validator=validator, resolver=ScopeResolver(db))
    for router in ROUTERS:
        check_router(router)
        app.include_router(router)
    # Unauthenticated and data-free by design (health.py); the only routes not audited.
    app.include_router(PROBES)
    return app


# Every router the API serves. Endpoints are added to these, never to the app.
ROUTERS: tuple[APIRouter, ...] = (bff.router,)


def check_router(router: APIRouter) -> None:
    """Refuse to serve a route that would skip the audited pipeline.

    Checked on the router itself, before inclusion: how FastAPI represents
    included routes internally is not ours to depend on.
    """
    if router.route_class is not AuditedRoute:
        raise RuntimeError("router does not use AuditedRoute")
    for route in router.routes:
        if not isinstance(route, AuditedRoute):
            path = route.path if isinstance(route, APIRoute) else repr(route)
            raise RuntimeError(f"{path} bypasses the audited pipeline")
