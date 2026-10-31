"""The request pipeline every endpoint runs through (HLD, Request lifecycle).

    token -> validate -> resolve scope -> permission matrix -> handler -> audit

``AuditedRoute`` is the only route class the app accepts (``create_app``
refuses any other), and an endpoint without a declared permission fails at
startup. So no endpoint can skip authentication, scope, the matrix or the
audit write by being written slightly differently.

Errors are structured codes. They never include a tenant id, a customer name
or anything from the database (CLAUDE.md, Code conventions).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from typing import Any, Final, TypeVar
from uuid import UUID

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import text

from cloudops_api.access import Access, NoScopeError, ScopeDefectError, ScopeResolver
from cloudops_api.auth import TokenError, TokenValidator
from cloudops_domain.assurance.pack import Signer, UnavailableSigner
from cloudops_domain.reporting.financial import CommercialSections
from cloudops_domain.reporting.sections import AvailabilitySource, FinancialSections
from cloudops_tenancy import Database

log = logging.getLogger("cloudops.api")

REQUIRED_PERMISSION: Final = "__cloudops_permission__"
# Authenticated and scoped, but no matrix permission needed (e.g. "who am I").
AUTHENTICATED: Final = "authenticated"

F = TypeVar("F", bound=Callable[..., Any])


def requires(permission: str) -> Callable[[F], F]:
    """Declare the matrix permission an endpoint needs (lifecycle stage 3)."""

    def mark(fn: F) -> F:
        setattr(fn, REQUIRED_PERMISSION, permission)
        return fn

    return mark


class ApiError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def error_response(status: int, code: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "request_id": request_id}},
        status_code=status,
        headers={"X-Request-Id": request_id},
    )


_AUDIT: Final = text(
    "INSERT INTO audit.action_log"
    " (tenant_id, actor, actor_is_agent, action, target_kind, target_ref, outcome)"
    " VALUES (:tenant_id, :actor, false, :action, :target_kind, :target_ref, :outcome)"
)


@dataclass(frozen=True, slots=True)
class Platform:
    db: Database
    validator: TokenValidator
    resolver: ScopeResolver
    # Step 17. Evidence packs are signed by ``signer`` (refuses until a key is
    # decided, ADR 0020); availability comes from the telemetry store when it
    # is connected; the financial sections come from the commercial engine (step 16).
    signer: Signer = field(default_factory=UnavailableSigner)
    availability: AvailabilitySource | None = None
    financial: FinancialSections = field(default_factory=CommercialSections)


def platform(request: Request) -> Platform:
    value = request.app.state.platform
    if not isinstance(value, Platform):
        raise RuntimeError("app.state.platform is not configured")
    return value


def current_access(request: Request) -> Access:
    """FastAPI dependency: the resolved access for this request."""
    access = getattr(request.state, "access", None)
    if not isinstance(access, Access):
        raise ApiError(500, "no_access_context")
    return access


def audit_only(request: Request, tenant_ids: Iterable[UUID]) -> None:
    """Narrow the audit rows to the tenants actually touched (default: all in scope)."""
    request.state.audit_tenants = frozenset(tenant_ids)


def _outcome(status: int) -> str:
    if status == 403:
        return "blocked"
    return "succeeded" if status < 400 else "failed"


async def _audit(
    db: Database, access: Access, request: Request, route: APIRoute, status: int
) -> None:
    """Lifecycle stage 7: one immutable row per tenant the request could reach.

    action_log carries a tenant_id, so a request is logged under each tenant
    in scope, or only the ones the handler says it touched. Written in its
    own transaction, under the caller's scope, after the handler: a handler
    that rolled back still leaves its access on record.
    """
    narrowed = getattr(request.state, "audit_tenants", None)
    tenants = access.scope.tenant_ids if narrowed is None else narrowed & access.scope.tenant_ids
    if not tenants:
        tenants = access.scope.tenant_ids
    path_params = ",".join(f"{k}={v}" for k, v in sorted(request.path_params.items()))
    rows = [
        {
            "tenant_id": t,
            "actor": access.identity.principal_id,
            "action": f"{request.method} {route.path_format}",
            "target_kind": route.name,
            "target_ref": path_params or None,
            "outcome": _outcome(status),
        }
        for t in sorted(tenants, key=str)
    ]
    async with db.transaction(access.scope) as conn:
        await conn.execute(_AUDIT, rows)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise TokenError("missing_token")
    return token.strip()


class AuditedRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        permission = getattr(self.endpoint, REQUIRED_PERMISSION, None)
        if not isinstance(permission, str):
            raise RuntimeError(f"{self.path} declares no permission; use @requires(...)")
        inner = super().get_route_handler()
        route = self

        async def handler(request: Request) -> Response:
            request_id = str(uuid.uuid4())
            p = platform(request)
            try:
                identity = await p.validator.validate(_bearer(request))
            except TokenError as exc:
                log.warning("token rejected: %s request=%s", exc.code, request_id)
                return error_response(401, exc.code, request_id)
            try:
                access = await p.resolver.resolve(identity)
            except NoScopeError as exc:
                log.warning("no scope: %s oid=%s request=%s", exc, identity.oid, request_id)
                return error_response(403, "no_scope", request_id)
            except ScopeDefectError as exc:
                log.error("SCOPE DEFECT: %s oid=%s request=%s", exc, identity.oid, request_id)
                return error_response(500, "scope_defect", request_id)
            request.state.access = access

            if permission != AUTHENTICATED and not access.allows(permission):
                response: Response = error_response(403, "forbidden", request_id)
            else:
                try:
                    response = await inner(request)
                except ApiError as exc:
                    response = error_response(exc.status, exc.code, request_id)
                except RequestValidationError:
                    response = error_response(422, "invalid_request", request_id)
                except HTTPException as exc:
                    response = error_response(exc.status_code, "http_error", request_id)
                except Exception:
                    log.exception("handler failed request=%s", request_id)
                    response = error_response(500, "internal_error", request_id)
            try:
                await _audit(p.db, access, request, route, response.status_code)
            except Exception:
                # No access without a record of it: the response is withheld.
                log.exception("AUDIT WRITE FAILED request=%s", request_id)
                return error_response(500, "audit_failed", request_id)
            response.headers["X-Request-Id"] = request_id
            return response

        return handler
