"""Run the ECB reference-rate connector (fixtures only, like every connector).

    python -m cloudops_connectors.ecb run --fixtures
    python -m cloudops_connectors.ecb schedule

The ECB file is public and needs no credential, but the connector still runs
only against recorded fixtures until the platform's egress to the ECB host is
agreed. See ``core/cli.py``.
"""

from __future__ import annotations

from datetime import timedelta

from cloudops_connectors.core.cli import main
from cloudops_connectors.ecb.client import EcbClient
from cloudops_connectors.ecb.fake import FIXTURE_HOST, FakeEcb
from cloudops_connectors.ecb.mapping import ENTITY, SOURCE
from cloudops_connectors.ecb.source import EcbSource


def _fixtures() -> EcbSource:
    return EcbSource(EcbClient(FIXTURE_HOST, transport=FakeEcb.from_fixtures().transport()))


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {ENTITY: timedelta(days=1)}, _fixtures))
