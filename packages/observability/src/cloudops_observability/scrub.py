"""What may leave a process in telemetry, and what is removed first (ADR 0022).

Azure Monitor is platform-wide storage outside row-level security, so nothing
that identifies a customer goes there: no tenant id, no customer name, no
principal, no customer host. Not even a pseudonymous tenant key. Per-tenant
attribution (LLM prompt, retrieval set, tool calls, tokens, cost, user) stays in
``cloudops.llm_call`` and ``cloudops.agent_run`` under RLS, joined to a trace by
its ``trace_id``.

Two rules, applied to every span, span event and log record before export:

1. **Attribute allowlist.** A key is kept only if it is listed in ``ALLOWED`` or
   starts with ``cloudops.`` and names nothing personal or tenant-shaped
   (``DENIED_FRAGMENTS``). Everything else — including whatever a library adds
   in a later version — is dropped. ``server.address`` survives only for
   platform hosts; any other host (a customer's ServiceNow instance, a storage
   account named after a customer) becomes ``external``.
2. **Value scrubbing.** Every string that is kept is passed through
   ``scrub_text``: secrets first (keys, tokens, connection strings, private
   keys), then e-mail addresses and UUIDs. Tenant ids and Entra object ids are
   UUIDs, so a message that interpolates one loses it here.

This is defence in depth, not a licence: code still must not put a customer
name in a log message, because a name has no pattern. The collector applies its
own allowlist again (``infra/gitops/base/otel-collector``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from opentelemetry.util.types import AttributeValue

# Secrets first, longest forms first. Kept in step with the AI gateway's
# redaction rules (services/gateway/.../redaction.py); a parity test fails if a
# secret the gateway recognises gets through here.
_SECRETS: Final = (
    ("PRIVATE_KEY", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("CONNECTION_STRING", re.compile(
        r"(?i)\b(?:DefaultEndpointsProtocol|Server|Host|Data Source|InstrumentationKey)=[^\n]*?"
        r"(?:AccountKey|Password|Pwd|SharedAccessKey|InstrumentationKey)=[^;\s]+[^\n]*")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("BEARER_TOKEN", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("URL_CREDENTIALS", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    ("POSTGRES_DSN", re.compile(r"(?i)\bpostgres(?:ql)?(?:\+\w+)?://\S+")),
    ("SECRET", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|client[_-]?secret"
        r"|token|sas|sig|accountkey|sharedaccesskey)\b\s*[:=]\s*[\"']?[^\s\"',;&]{4,}")),
)
_EMAIL: Final = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_UUID: Final = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

MAX_STRING: Final = 4096


def scrub_text(value: str) -> str:
    """Secrets, e-mail addresses and UUIDs replaced by placeholders; truncated."""
    out = value
    for kind, pattern in _SECRETS:
        out = pattern.sub(f"[REDACTED:{kind}]", out)
    out = _EMAIL.sub("[REDACTED:EMAIL]", out)
    out = _UUID.sub("[id]", out)
    if len(out) > MAX_STRING:
        out = out[:MAX_STRING] + "…"
    return out


# OpenTelemetry semantic-convention keys that carry no customer data once
# scrubbed. url.path, url.full, url.query, http.target and http.url are absent
# on purpose: paths and queries carry subscription ids, resource group names and
# ticket numbers. The route template (http.route) says the same thing safely.
ALLOWED: Final = frozenset({
    # HTTP
    "http.request.method", "http.method", "http.response.status_code", "http.status_code",
    "http.route", "url.scheme", "server.address", "server.port", "network.protocol.version",
    "http.request.method_original",
    # Database: statements carry bind markers, never values (packages/tenancy).
    "db.system", "db.system.name", "db.operation", "db.operation.name", "db.statement",
    "db.query.text", "db.name", "db.namespace",
    # RPC / Temporal span kinds
    "rpc.system", "rpc.service", "rpc.method",
    # Errors
    "error.type", "exception.type", "exception.message", "exception.stacktrace",
    "exception.escaped", "otel.status_code", "otel.status_description",
    # Generative AI (model and usage only; prompts stay in the RLS'd trace)
    "gen_ai.system", "gen_ai.operation.name", "gen_ai.request.model", "gen_ai.response.model",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    # Structured log fields (logs.py)
    "logger", "level", "service", "security_event", "source", "code", "alert", "connector",
    "entity", "workflow_type", "activity_type", "task_queue", "status", "outcome", "route",
    "method", "status_code", "duration_ms", "request_id", "run_id", "period", "version",
    "kind", "reason", "drift", "window",
})

# A cloudops.* key containing one of these is refused even though the prefix is
# ours: the name itself says it would carry a tenant or a person.
DENIED_FRAGMENTS: Final = ("tenant", "customer", "principal", "user", "oid", "email", "name",
                           "company", "account", "subscription", "number", "prompt")

# Hosts that identify only the platform or a shared provider endpoint. Anything
# else may name a customer and is reported as ``external``.
_PLATFORM_HOST_SUFFIXES: Final = (
    ".svc.cluster.local", ".cloudops.internal",
    ".openai.azure.com", ".vault.azure.net",
)
_PLATFORM_HOSTS: Final = frozenset({
    "localhost", "127.0.0.1", "management.azure.com", "login.microsoftonline.com",
    "graph.microsoft.com", "data-api.ecb.europa.eu",
})
EXTERNAL: Final = "external"


def platform_host(host: str) -> bool:
    h = host.lower().rstrip(".")
    return h in _PLATFORM_HOSTS or h.endswith(_PLATFORM_HOST_SUFFIXES)


def allowed_key(key: str) -> bool:
    if key in ALLOWED:
        return True
    if key.startswith("cloudops."):
        lowered = key.lower()
        return not any(f in lowered for f in DENIED_FRAGMENTS)
    return False


def _value(value: AttributeValue) -> AttributeValue:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes | str):
        return [scrub_text(v) if isinstance(v, str) else v for v in value]
    if isinstance(value, bytes):
        return b""
    return value


def scrub_attributes(attributes: Mapping[str, AttributeValue] | None) -> dict[str, AttributeValue]:
    """The allowlisted subset of ``attributes``, every string scrubbed."""
    out: dict[str, AttributeValue] = {}
    for key, value in (attributes or {}).items():
        if not allowed_key(key):
            continue
        if key == "server.address" and isinstance(value, str) and not platform_host(value):
            out[key] = EXTERNAL
            continue
        out[key] = _value(value)
    return out


__all__ = ["ALLOWED", "EXTERNAL", "allowed_key", "platform_host", "scrub_attributes",
           "scrub_text"]
