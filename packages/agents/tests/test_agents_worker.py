"""The worker's Azure wiring builds, without Azure: azure-identity's async
credential needs an HTTP transport (aiohttp) that nothing else declared, and the
first Azure deployment crash-looped on it (ADR 0012)."""

from __future__ import annotations

from pathlib import Path

from azure.identity.aio import WorkloadIdentityCredential


async def test_the_async_workload_identity_credential_can_be_built(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("federated-token", encoding="utf-8")
    credential = WorkloadIdentityCredential(
        tenant_id="00000000-0000-0000-0000-000000000001",
        client_id="00000000-0000-0000-0000-000000000002",
        token_file_path=str(token_file),
    )
    await credential.close()


def test_the_worker_package_declares_the_transport_itself() -> None:
    """The shared dev environment has aiohttp through another package, so the test
    above passes either way there; the worker's own image has only what this
    package declares."""
    from importlib.metadata import requires

    declared = requires("cloudops-agents") or []
    assert any(r.startswith("azure-core[aio]") for r in declared), declared
