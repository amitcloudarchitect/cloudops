"""The tool registry: what a model may ask the platform to do, and the checks
between the asking and the doing (LLD, Gateway controls).

Each tool declares its name, argument JSON Schema, the matrix permission it
needs (ADR 0002, D8), read or write, whether it is tenant-scoped, whether it
needs an idempotency key, and a timeout.

``invoke`` refuses, in this order and before any handler runs:

1. **Unknown tool.**
2. **Permission not held by the caller.** Checked before the arguments are
   even parsed.
3. **Write tool.** Writes route through the approval machine, which does not
   exist yet, so none run.
4. **Arguments that are not JSON, or do not validate against the schema.** An
   invalid argument is a refusal, never a best-effort call.
5. **Timeout.**

A handler receives a ``ToolContext``: the database behind packages/tenancy
and the conversation's server-resolved scope. Never a connection string, a
credential, or a scope taken from an argument.

Registration rejects any schema that offers a model a way to name a tenant, a
role, a principal or a subscription, so that cannot be added to a tool by
accident. Every schema must close itself (``additionalProperties: false``).
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from cloudops_gateway.gateway import CallContext
from cloudops_gateway.providers import ToolCallRequest, ToolSpec
from cloudops_tenancy import Database, TenantScope

log = logging.getLogger("cloudops.gateway.tools")

# Argument names a model must never be able to supply.
FORBIDDEN_ARGUMENTS: Final = frozenset({
    "tenant", "tenant_id", "tenant_ids", "tenants", "customer", "customer_id", "organisation",
    "organization", "org", "org_id", "scope", "role", "roles", "principal", "principal_id",
    "user", "user_id", "oid", "account_ids", "subscription", "subscription_id", "permissions",
})


class ToolKind(enum.StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ToolContext:
    """All a handler gets: the tenancy door and the scope to open it with."""

    db: Database
    scope: TenantScope
    permissions: frozenset[str]


@dataclass(frozen=True)
class Source:
    """Where a tool's figures came from, so an answer can cite them."""

    source_system: str
    ref: str
    as_of: datetime


@dataclass(frozen=True)
class ToolResult:
    data: Mapping[str, Any]
    sources: tuple[Source, ...] = ()


Handler = Callable[[ToolContext, Mapping[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: Mapping[str, Any]
    permission: str
    kind: ToolKind
    handler: Handler
    tenant_scoped: bool = True
    idempotency_key: bool = False
    timeout_seconds: float = 10.0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    name: str
    ok: bool
    content: str  # what the model is shown: a JSON result, or a refusal code
    refusal: str | None = None
    sources: tuple[Source, ...] = field(default_factory=tuple)

    def trace(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "refusal": self.refusal,
                "sources": [s.ref for s in self.sources]}


class RegistrationError(ValueError):
    pass


def _properties(schema: Any) -> Iterable[str]:
    """Every property name anywhere in a schema."""
    if isinstance(schema, Mapping):
        props = schema.get("properties")
        if isinstance(props, Mapping):
            yield from props
            for sub in props.values():
                yield from _properties(sub)
        for key in ("items", "additionalProperties", "not"):
            if key in schema:
                yield from _properties(schema[key])
        for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            for sub in schema.get(key, []) or []:
                yield from _properties(sub)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)  # money never becomes a float, even for a model
    if isinstance(value, datetime | date | UUID):
        return value.isoformat() if not isinstance(value, UUID) else str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


class ToolRegistry:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._tools: dict[str, Tool] = {}
        self._validators: dict[str, Draft202012Validator] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise RegistrationError(f"{tool.name} is already registered")
        try:
            Draft202012Validator.check_schema(tool.parameters)
        except SchemaError as exc:
            raise RegistrationError(f"{tool.name}: invalid schema: {exc.message}") from None
        if tool.parameters.get("type") != "object" or tool.parameters.get(
            "additionalProperties"
        ) is not False:
            raise RegistrationError(f"{tool.name}: schema must be an object closed with "
                                    "additionalProperties: false")
        leaked = sorted(set(_properties(tool.parameters)) & FORBIDDEN_ARGUMENTS)
        if leaked:
            raise RegistrationError(
                f"{tool.name}: arguments {leaked} would let a model choose scope; "
                "scope is resolved server-side"
            )
        if tool.kind is ToolKind.WRITE and not tool.idempotency_key:
            raise RegistrationError(f"{tool.name}: write tools must take an idempotency key")
        self._tools[tool.name] = tool
        self._validators[tool.name] = Draft202012Validator(tool.parameters)

    def specs(self, permissions: Iterable[str]) -> list[ToolSpec]:
        """Only the tools this caller may use are offered to the model at all."""
        held = set(permissions)
        return [t.spec for t in self._tools.values()
                if t.permission in held and t.kind is ToolKind.READ]

    def tool(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def invoke(self, call: ToolCallRequest, ctx: CallContext) -> ToolOutcome:
        def refuse(code: str) -> ToolOutcome:
            log.info("tool refused: %s %s principal=%s", call.name, code, ctx.principal_id)
            return ToolOutcome(call.id, call.name, False, json.dumps({"refused": code}), code)

        tool = self._tools.get(call.name)
        if tool is None:
            return refuse("unknown_tool")
        if tool.permission not in ctx.permissions:
            return refuse("forbidden")
        if tool.kind is ToolKind.WRITE:
            return refuse("write_requires_approval")
        try:
            args = json.loads(call.arguments) if call.arguments.strip() else {}
        except json.JSONDecodeError:
            return refuse("invalid_arguments")
        if not isinstance(args, dict) or not self._validators[tool.name].is_valid(args):
            return refuse("invalid_arguments")
        context = ToolContext(self._db, ctx.scope, ctx.permissions)
        try:
            result = await asyncio.wait_for(tool.handler(context, args), tool.timeout_seconds)
        except TimeoutError:
            return refuse("timeout")
        except Exception:
            # The model learns that the tool failed, never why: no stack, no SQL, no data.
            log.exception("tool %s failed", tool.name)
            return refuse("tool_failed")
        content = json.dumps(
            {"result": result.data,
             "sources": [{"source_system": s.source_system, "ref": s.ref,
                          "as_of": s.as_of.isoformat()} for s in result.sources]},
            default=_json_default, sort_keys=True,
        )
        return ToolOutcome(call.id, call.name, True, content, None, result.sources)
