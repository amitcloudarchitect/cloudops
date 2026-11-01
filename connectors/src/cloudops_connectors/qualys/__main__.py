"""Run the Qualys connector: recorded fixtures, or live as the Qualys API user.

    python -m cloudops_connectors.qualys run --fixtures
    python -m cloudops_connectors.qualys worker            # Azure profile: the live path
    python -m cloudops_connectors.qualys schedule

See ``core/cli.py``. The live path (ADR 0028) reads the platform's Qualys
subscription as its API user (``CLOUDOPS_QUALYS_URL``, ``CLOUDOPS_QUALYS_USERNAME``,
the password ``qualys-api-password`` from the secret store). Customers come from
the hosts' asset tags, through the customer mapping.

``CLOUDOPS_QUALYS_SOURCE=simulation`` (dev only) is not served from here: the
simulated estate is ``cloudops_devdata``'s, and its worker is
``python -m cloudops_devdata.simulation.qualys_live``, which runs this same
connector code over it.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Final

from cloudops_config import Component, ConfigurationError, QualysSource, Settings
from cloudops_connectors.core.cli import Live, main
from cloudops_connectors.core.sync import CUSTOMER_SYNC
from cloudops_connectors.qualys import source
from cloudops_connectors.qualys.auth import ApiUser
from cloudops_connectors.qualys.client import QualysClient
from cloudops_connectors.qualys.fake import FIXTURE_HOST, FakeQualys
from cloudops_connectors.qualys.mapping import ENTITY, SOURCE


def _fixtures() -> source.QualysSource:
    fake = FakeQualys.from_fixtures()
    return source.QualysSource(QualysClient(FIXTURE_HOST, transport=fake.transport()))


def live_entities(settings: Settings) -> frozenset[str]:
    return frozenset({ENTITY})


def live_source(settings: Settings, stack: AsyncExitStack) -> source.QualysSource:
    config = settings.qualys
    if config.source is not QualysSource.LIVE:
        raise ConfigurationError(
            "CLOUDOPS_QUALYS_SOURCE=simulation is served by "
            "python -m cloudops_devdata.simulation.qualys_live, not this entry point")
    if config.api_url is None or config.username is None or config.password_secret is None:
        raise ConfigurationError("live Qualys needs its URL, API user and password")
    secret = config.password_secret
    client = QualysClient(config.api_url, auth=ApiUser(
        config.api_url, config.username, lambda: settings.secrets.get(secret)))
    stack.push_async_callback(client.aclose)
    return source.QualysSource(client)


LIVE: Final = Live(frozenset({Component.QUALYS}), live_entities, live_source,
                   schedule_requires=frozenset())


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {ENTITY: CUSTOMER_SYNC}, _fixtures, live=LIVE))
