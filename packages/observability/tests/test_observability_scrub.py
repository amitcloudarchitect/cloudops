"""Nothing customer-identifying or secret survives scrubbing (ADR 0022)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cloudops_gateway.redaction import redact
from cloudops_observability.scrub import EXTERNAL, scrub_attributes, scrub_text

TENANT = str(uuid4())



def _j(*parts: str) -> str:
    """Join fixture parts at runtime: no line of this file matches a secret
    pattern (CLAUDE.md, and the repository's secret scan)."""
    return "".join(parts)


# Invented, secret-shaped values the scrubber must remove.
SECRETS = [
    _j("-----BEGIN RSA PRIVATE", " KEY-----\nMIIEow\n-----END RSA PRIVATE", " KEY-----"),
    _j("DefaultEndpointsProtocol=https;AccountName=x;Account", "Key=", "abc123def456==",
       ";EndpointSuffix=net"),
    _j("Server=db;Database=x;User Id=a;Pass", "word=", "hunter2pass;"),
    _j("ey", "JhbGciOiJSUzI1NiJ9.", "ey", "JzdWIiOiIxMjM0NTY3ODkwIn0.",
       "c2lnbmF0dXJlc2lnbmF0dXJl"),
    _j("Authorization: Bear", "er ", "abcdefghijklmnopqrstuvwxyz012345"),
    _j("AKIA", "IOSFODNN7EXAMPLE"),
    _j("https://admin:", "s3cretpw", "@instance.example.com/api"),
    _j("client_", "secret=", "Zm9vYmFyYmF6cXV4"),
    _j("postgresql://app:", "pw@db.internal:5432/cloudops"),
]


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_are_removed(secret: str) -> None:
    out = scrub_text(f"failed with {secret} today")
    assert "REDACTED" in out or "[url]" in out  # a URL goes whole, credentials and all
    for fragment in ("hunter2pass", "abc123def456", "s3cretpw", "AKIAIOSFODNN7EXAMPLE",
                     "Zm9vYmFyYmF6cXV4", "c2lnbmF0dXJl", "MIIEow", "abcdefghijklmnop"):
        assert fragment not in out


@pytest.mark.parametrize("secret", SECRETS)
def test_parity_with_the_gateway(secret: str) -> None:
    """Whatever the AI gateway treats as a secret, telemetry does too."""
    assert redact(secret).count > 0
    assert scrub_text(secret) != secret


def test_uuids_and_emails_are_removed() -> None:
    out = scrub_text(f"no scope: tenant={TENANT} user=asha.patel@example.com")
    assert TENANT not in out
    assert "asha.patel" not in out
    assert "[id]" in out


def test_long_values_are_truncated() -> None:
    assert len(scrub_text("x" * 10_000)) <= 4097


def test_attribute_allowlist() -> None:
    attrs = scrub_attributes({
        "http.route": f"/v1/customers/{TENANT}",
        "http.request.method": "GET",
        "url.full": "https://acme.service-now.com/api/now/table/incident?sysparm_query=x",
        "url.path": f"/subscriptions/{TENANT}/resourceGroups/rg-acme-prod",
        "temporalWorkflowID": f"triage-{TENANT}-INC0012345",
        "tenant_id": TENANT,
        "cloudops.tenant": TENANT,
        "cloudops.customer_name": "Acme Ltd",
        "cloudops.principal": "oid",
        "cloudops.llm.task": "classify",
        "server.address": "acme.service-now.com",
        "db.statement": "SELECT 1 FROM cloudops.tenant WHERE id = %(id)s",
    })
    assert attrs == {
        "http.route": "/v1/customers/[id]",
        "http.request.method": "GET",
        "cloudops.llm.task": "classify",
        "server.address": EXTERNAL,
        "db.statement": "SELECT 1 FROM cloudops.tenant WHERE id = %(id)s",
    }


@pytest.mark.parametrize("host", ["management.azure.com", "aoai-cloudops-dev.openai.azure.com",
                                  "temporal-frontend.workflow.svc.cluster.local"])
def test_platform_hosts_are_kept(host: str) -> None:
    assert scrub_attributes({"server.address": host}) == {"server.address": host}


@pytest.mark.parametrize("host", ["acme.service-now.com", "stacmecosts.blob.core.windows.net",
                                  "acme-cur.s3.eu-west-1.amazonaws.com"])
def test_customer_hosts_become_external(host: str) -> None:
    assert scrub_attributes({"server.address": host}) == {"server.address": EXTERNAL}


def test_sequences_are_scrubbed_and_bytes_dropped() -> None:
    attrs = scrub_attributes({"cloudops.ids": [TENANT, "ok"], "cloudops.blob": b"raw"})
    assert attrs == {"cloudops.ids": ["[id]", "ok"], "cloudops.blob": b""}
