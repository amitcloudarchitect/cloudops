"""Run the CoreStack connector (fixtures only until credentials exist).

    python -m cloudops_connectors.corestack run --fixtures
    python -m cloudops_connectors.corestack schedule

See ``core/cli.py``.
"""

from __future__ import annotations

from datetime import timedelta

from cloudops_connectors.core.cli import main
from cloudops_connectors.corestack.client import CoreStackClient
from cloudops_connectors.corestack.fake import FIXTURE_HOST, FakeCoreStack
from cloudops_connectors.corestack.mapping import ENTITY, SOURCE
from cloudops_connectors.corestack.source import CoreStackSource


def _fixtures() -> CoreStackSource:
    fake = FakeCoreStack.from_fixtures()
    return CoreStackSource(CoreStackClient(FIXTURE_HOST, transport=fake.transport()))


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {ENTITY: timedelta(days=1)}, _fixtures))
