"""Run the CoreStack connector: recorded fixtures, or live with an access key pair.

    python -m cloudops_connectors.corestack run --fixtures
    python -m cloudops_connectors.corestack worker            # Azure profile: the live path
    python -m cloudops_connectors.corestack schedule

See ``core/cli.py``. The live path (ADR 0028) reads the CoreStack tenants in
``CLOUDOPS_CORESTACK_TENANTS`` at ``CLOUDOPS_CORESTACK_URL``, with the key pair
``corestack-access-key`` and ``corestack-secret-key`` from the secret store (read
for each token request). The client can send nothing but
the token request and the two recommendation GETs (``client.ALLOWED``).
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Final

from cloudops_config import Component, Settings
from cloudops_connectors.core.cli import Live, main
from cloudops_connectors.core.sync import CUSTOMER_SYNC
from cloudops_connectors.corestack.client import CoreStackClient
from cloudops_connectors.corestack.fake import (
    FIXTURE_ACCESS_KEY,
    FIXTURE_HOST,
    FIXTURE_SECRET,
    FakeCoreStack,
    load_fixture,
)
from cloudops_connectors.corestack.mapping import ENTITY, SOURCE
from cloudops_connectors.corestack.source import CoreStackSource


def _fixtures() -> CoreStackSource:
    fake = FakeCoreStack.from_fixtures()
    client = CoreStackClient(FIXTURE_HOST, lambda: FIXTURE_ACCESS_KEY, lambda: FIXTURE_SECRET,
                             transport=fake.transport(), min_interval=0.0)
    return CoreStackSource(client, tuple(t["id"] for t in load_fixture()["tenants"]))


def live_entities(settings: Settings) -> frozenset[str]:
    return frozenset({ENTITY})


def live_source(settings: Settings, stack: AsyncExitStack) -> CoreStackSource:
    config = settings.corestack
    key_id, secret = config.access_key, config.secret_key
    client = CoreStackClient(config.api_url, lambda: settings.secrets.get(key_id),
                             lambda: settings.secrets.get(secret))
    stack.push_async_callback(client.aclose)
    return CoreStackSource(client, config.tenants)


# The schedule job mounts no secret, and the entity needs none of the configuration.
LIVE: Final = Live(frozenset({Component.CORESTACK}), live_entities, live_source,
                   schedule_requires=frozenset())


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {ENTITY: CUSTOMER_SYNC}, _fixtures, live=LIVE))
