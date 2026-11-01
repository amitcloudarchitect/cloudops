"""Application factory.

    app = create_app(db=db, validator=validator)

Dependencies are passed in, never discovered: the factory reads no
environment. ``__main__`` wires them from the configuration layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from cloudops_api import (
    assistant,
    assurance,
    bff,
    cloud_onboarding,
    commercial,
    commercial_setup,
    connectors,
    home,
    knowledge,
    mapping,
    observability,
    operations,
    portal,
    project_prices,
    reports,
    secops,
)
from cloudops_api.access import ScopeResolver
from cloudops_api.assistant import AssistantRuntime
from cloudops_api.auth import TokenValidator
from cloudops_api.health import PROBES
from cloudops_api.pipeline import AuditedRoute, Platform
from cloudops_domain.assurance.pack import Signer, UnavailableSigner
from cloudops_domain.reporting.financial import CommercialSections
from cloudops_domain.reporting.sections import AvailabilitySource, FinancialSections
from cloudops_tenancy import Database


def create_app(
    *,
    db: Database,
    validator: TokenValidator,
    expose_openapi: bool = False,
    web_origins: tuple[str, ...] = (),
    signer: Signer | None = None,
    availability: AvailabilitySource | None = None,
    financial: FinancialSections | None = None,
    assistant_factory: AssistantFactory | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The assistant (ADR 0029) needs the AI gateway; without one configured its
        # endpoints answer 503 assistant_unavailable and everything else runs.
        async with AsyncExitStack() as stack:
            if assistant_factory is not None:
                runtime = await assistant_factory(stack)
                stack.push_async_callback(runtime.aclose)
                app.state.assistant = runtime
            yield

    app = FastAPI(
        title="CloudOps Platform API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
        # The schema drives generated TypeScript types; served locally only.
        openapi_url="/openapi.json" if expose_openapi else None,
    )
    app.state.platform = Platform(
        db=db, validator=validator, resolver=ScopeResolver(db),
        signer=signer or UnavailableSigner(), availability=availability,
        financial=financial or CommercialSections(),
    )
    for router in ROUTERS:
        check_router(router)
        app.include_router(router)
    # Unauthenticated and data-free by design (health.py); the only routes not audited.
    app.include_router(PROBES)
    if web_origins:
        # The console sends a bearer token from the browser (ADR 0014). Exact
        # origins only; no cookies, so credentials stay disallowed.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(web_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            expose_headers=["X-Request-Id"],
            max_age=600,
        )
    return app


AssistantFactory = Callable[[AsyncExitStack], Awaitable[AssistantRuntime]]

# Every router the API serves. Endpoints are added to these, never to the app.
ROUTERS: tuple[APIRouter, ...] = (
    bff.router, operations.router, mapping.router, connectors.router, portal.router,
    commercial.router, project_prices.router, knowledge.router, assurance.router,
    reports.router, assistant.router,
    cloud_onboarding.router, home.router, observability.router, commercial_setup.router,
    secops.router,
)


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
