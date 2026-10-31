"""The collector's allowlist and the services' agree (ADR 0021): the collector
must not drop what a service is allowed to send, and every ``cloudops.*``
attribute the code sets must be both allowed by the services and listed there."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from cloudops_observability.scrub import ALLOWED, allowed_key

REPO = Path(__file__).resolve().parents[3]
COLLECTOR = REPO / "infra/gitops/base/observability/collector.yaml"
# Service code (devdata is a seeding tool and names tables in the same shape).
SOURCES = [p for root in ("packages", "services", "connectors")
           for p in (REPO / root).rglob("src/**/*.py") if "cloudops_devdata" not in p.parts]
# A cloudops.* attribute key as code sets one: a dict key, or set_attribute's first
# argument. (Table names and logger names appear in other positions.)
KEY = re.compile(r'(?:"(cloudops\.[a-z_.]+)":|set_attribute\("(cloudops\.[a-z_.]+)")')


def _collector() -> dict[str, object]:
    return dict(yaml.safe_load(COLLECTOR.read_text(encoding="utf-8")))


def _allowed_by_collector() -> set[str]:
    return set(_collector()["processors"]["redaction"]["allowed_keys"])  # type: ignore[index]


def _attribute_keys_in_code() -> set[str]:
    keys: set[str] = set()
    for path in SOURCES:
        for pair in KEY.findall(path.read_text(encoding="utf-8")):
            keys |= {k for k in pair if k}
    return keys


def test_the_collector_keeps_everything_a_service_may_send() -> None:
    assert ALLOWED - _allowed_by_collector() == set()


def test_every_attribute_in_code_is_allowed_on_both_sides() -> None:
    found = _attribute_keys_in_code()
    assert "cloudops.llm.task" in found, "the pattern still finds attribute keys"
    assert {k for k in found if not allowed_key(k)} == set()
    assert found - _allowed_by_collector() == set()


def test_sampling_follows_the_lld() -> None:
    policies = {p["name"]: p for p in
                _collector()["processors"]["tail_sampling"]["policies"]}  # type: ignore[index]
    assert policies["errors"]["status_code"]["status_codes"] == ["ERROR"]
    assert policies["execution-spans"]["boolean_attribute"]["key"] == "cloudops.execution"
    assert policies["baseline"]["probabilistic"]["sampling_percentage"] == 10
    assert set(policies) == {"errors", "execution-spans", "execution-workflows", "baseline"}


def test_traces_and_logs_pass_the_redaction_before_export() -> None:
    pipelines = _collector()["service"]["pipelines"]  # type: ignore[index]
    for name in ("traces", "logs"):
        procs = pipelines[name]["processors"]
        assert "redaction" in procs
        assert procs.index("redaction") < procs.index("batch")
    assert pipelines["traces"]["processors"].index("redaction") < \
        pipelines["traces"]["processors"].index("tail_sampling")
