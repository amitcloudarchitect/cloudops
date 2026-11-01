"""The Observability page's policy figures, kept equal to their sources: the
assistant's sub-budget is the gateway's (ADR 0029)."""

from __future__ import annotations

from decimal import Decimal

from cloudops_api import observability
from cloudops_gateway.gateway import Limits


def test_the_assistant_sub_budget_is_the_gateways() -> None:
    sub = Limits().sub_budgets[observability.ASSISTANT_PREFIX]
    assert Decimal(str(sub.tenant_monthly_fraction)) == observability.ASSISTANT_MONTHLY_SHARE
