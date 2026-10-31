"""Run the Qualys connector (fixtures only until credentials exist).

    python -m cloudops_connectors.qualys run --fixtures
    python -m cloudops_connectors.qualys schedule

See ``core/cli.py``.
"""

from __future__ import annotations

from datetime import timedelta

from cloudops_connectors.core.cli import main
from cloudops_connectors.qualys.client import QualysClient
from cloudops_connectors.qualys.fake import FIXTURE_HOST, FakeQualys
from cloudops_connectors.qualys.mapping import ENTITY, SOURCE
from cloudops_connectors.qualys.source import QualysSource


def _fixtures() -> QualysSource:
    fake = FakeQualys.from_fixtures()
    return QualysSource(QualysClient(FIXTURE_HOST, transport=fake.transport()))


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {ENTITY: timedelta(days=1)}, _fixtures))
