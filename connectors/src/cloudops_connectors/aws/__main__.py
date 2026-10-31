"""Run the AWS connector (fixtures only until credentials exist).

    python -m cloudops_connectors.aws run --fixtures [--entity config|cur]
    python -m cloudops_connectors.aws schedule

See ``core/cli.py``.
"""

from __future__ import annotations

from datetime import timedelta

from cloudops_connectors.aws.client import ConfigClient, S3Client
from cloudops_connectors.aws.fake import (
    AGGREGATOR,
    CONFIG_HOST,
    REGION,
    S3_HOST,
    FakeConfig,
    FakeS3,
)
from cloudops_connectors.aws.mapping import CONFIG, CUR, SOURCE
from cloudops_connectors.aws.source import AwsSource
from cloudops_connectors.core.cli import main


def _fixtures() -> AwsSource:
    s3 = FakeS3.from_fixtures()
    return AwsSource(
        ConfigClient(REGION, AGGREGATOR, base_url=CONFIG_HOST,
                     transport=FakeConfig.from_fixtures().transport()),
        S3Client(s3.bucket, REGION, base_url=S3_HOST, transport=s3.transport()),
        s3.reports,
    )


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {CONFIG: timedelta(minutes=15), CUR: timedelta(days=1)},
                          _fixtures))
