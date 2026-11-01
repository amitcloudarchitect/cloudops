"""The home dashboard's policy figures (ADR 0030), kept equal to their sources: the
classifier's confidence threshold and the gateway's tenant quota and warning share.
The API image carries neither package, so these tests are what keeps them in step."""

from __future__ import annotations

from decimal import Decimal

from cloudops_agents import classifier
from cloudops_api import home
from cloudops_gateway.gateway import Limits


def test_the_confidence_threshold_is_the_classifiers() -> None:
    assert Decimal(str(classifier.CLASSIFIER.confidence_threshold)) == home.CONFIDENCE_THRESHOLD


def test_the_quota_and_warning_are_the_gateways() -> None:
    limits = Limits()
    assert limits.tenant_monthly_tokens == home.TENANT_MONTHLY_TOKENS
    assert Decimal(str(limits.alert_fraction)) == home.ALERT_SHARE


def test_every_priority_but_planning_has_a_modelled_target() -> None:
    assert set(home.SLA_TARGET_HOURS) == set(home.PRIORITIES) - {"5"}
    targets = [home.SLA_TARGET_HOURS[p] for p in sorted(home.SLA_TARGET_HOURS)]
    assert targets == sorted(targets)  # a higher priority never has a longer target
