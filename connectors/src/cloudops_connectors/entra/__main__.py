"""Run the Entra ID connector (fixtures only until credentials exist).

    python -m cloudops_connectors.entra run --fixtures [--entity user]
    python -m cloudops_connectors.entra schedule

See ``core/cli.py``. The fixture directories include one nobody has mapped: it
is quarantined, never written.
"""

from __future__ import annotations

from datetime import timedelta

from cloudops_connectors.core.cli import main
from cloudops_connectors.entra.fake import FIXTURE_GRAPH, FakeEntra
from cloudops_connectors.entra.mapping import ROLE_ASSIGNMENT, SOURCE, USER
from cloudops_connectors.entra.source import EntraSource
from cloudops_connectors.graph.client import GraphClient


def _fixtures() -> EntraSource:
    fake = FakeEntra.from_fixtures()
    return EntraSource({d: GraphClient(FIXTURE_GRAPH, transport=fake.transport(d))
                        for d in fake.directories})


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {USER: timedelta(days=1), ROLE_ASSIGNMENT: timedelta(days=1)},
                          _fixtures))
