"""Run the AWS connector over recorded fixtures. A customer's own account, read with
its stored access key (ADR 0027), is ``cloudops_connectors.customer``'s.

    python -m cloudops_connectors.aws run --fixtures [--entity config|cur|cost_total|invoice]
    python -m cloudops_connectors.aws schedule

See ``core/cli.py``. The billing fixtures were recorded on ``BILLING_DAY``, so
the fixture source runs its billing entities as of that day.
"""

from __future__ import annotations

from datetime import date, timedelta

from cloudops_connectors.aws.billing_source import PayerBilling
from cloudops_connectors.aws.client import (
    ConfigClient,
    CostExplorerClient,
    InvoicingClient,
    S3Client,
)
from cloudops_connectors.aws.fake import (
    AGGREGATOR,
    BILLING_DAY,
    CE_HOST,
    CONFIG_HOST,
    INVOICING_HOST,
    REGION,
    S3_HOST,
    FakeConfig,
    FakeCostExplorer,
    FakeInvoicing,
    FakeS3,
)
from cloudops_connectors.aws.mapping import CONFIG, COST_TOTAL, CUR, INVOICE, SOURCE
from cloudops_connectors.aws.source import AwsSource
from cloudops_connectors.core.cli import main

FIXTURE_PAYERS = ("900000000001", "900000000002")


def _billing(payer: str) -> PayerBilling:
    return PayerBilling(
        payer,
        CostExplorerClient(payer, base_url=CE_HOST,
                           transport=FakeCostExplorer.from_fixtures(payer).transport()),
        InvoicingClient(payer, base_url=INVOICING_HOST,
                        transport=FakeInvoicing.from_fixtures(payer).transport()),
    )


def _fixtures() -> AwsSource:
    s3 = FakeS3.from_fixtures()
    return AwsSource(
        ConfigClient(REGION, AGGREGATOR, base_url=CONFIG_HOST,
                     transport=FakeConfig.from_fixtures().transport()),
        S3Client(s3.bucket, REGION, base_url=S3_HOST, transport=s3.transport()),
        s3.reports,
        billing=[_billing(p) for p in FIXTURE_PAYERS],
        today=lambda: date.fromisoformat(BILLING_DAY),
    )


if __name__ == "__main__":
    raise SystemExit(main(SOURCE, {CONFIG: timedelta(minutes=15), CUR: timedelta(days=1),
                                   COST_TOTAL: timedelta(days=1), INVOICE: timedelta(days=1)},
                          _fixtures))
