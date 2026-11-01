"""Contract tests for the Cloud Services audit entities (ADR 0038) against the
fixtures in ``azure/fixtures/audit``. No database, no Temporal server: the
normalisers, the configuration rules, the client's paging and target checks, and
the fixtures themselves.

The fixtures are in the shapes Resource Graph and ARM returned for the dev
subscription (fields checked live, 2026-11-01), with made-up resources on the
synthetic estate's subscriptions: nothing recorded from a customer.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from cloudops_connectors.azure import audit, mapping
from cloudops_connectors.azure.client import GRAPH_PATH, AzureClient
from cloudops_connectors.azure.fake import FIXTURE_HOST, FakeAzure, load_fixture
from cloudops_connectors.core.identity import TargetOutOfScopeError

SUB = "713eb261-d613-543b-bfa9-f71bb3e74b14"
OTHER = "89ef84c7-bf5f-5ef2-9bdf-3b4e0ce59a7f"


async def _no_sleep(_: float) -> None:
    return None


def _client(fake: FakeAzure, **kwargs: Any) -> AzureClient:
    return AzureClient(FIXTURE_HOST, transport=fake.transport(), sleep=_no_sleep, **kwargs)


def _rows(entity: str) -> list[Any]:
    doc = json.loads(load_fixture(f"audit/{entity}.json"))
    return list(doc["value"] if entity == audit.DEFENDER else doc["data"])


def _checks(entity: str) -> tuple[list[audit.Check], int]:
    out: list[audit.Check] = []
    rejected = 0
    for row in _rows(entity):
        try:
            out.extend(audit.defender(row, SUB) if entity == audit.DEFENDER
                       else audit.NORMALISE[entity](row))
        except mapping.MalformedRecordError:
            rejected += 1
    return out, rejected


# --- the fixtures ----------------------------------------------------------------------


@pytest.mark.parametrize("entity", sorted(audit.ENTITIES))
def test_every_entity_has_a_fixture_with_one_malformed_row(entity: str) -> None:
    checks, rejected = _checks(entity)
    assert checks
    assert rejected == (0 if entity in (audit.POLICY, audit.ADVISOR) else 1)


@pytest.mark.parametrize("entity", sorted(audit.GRAPH_ENTITIES))
def test_fixture_rows_carry_exactly_the_projected_columns(entity: str) -> None:
    """The fixtures are in the query's shape: no column the query does not ask for."""
    for row in _rows(entity):
        if row.get("id"):
            assert set(row) <= audit.MAPPED[entity]
            assert audit.EXPECTED[entity] <= set(row)


def test_the_fixtures_name_no_real_subscription() -> None:
    """Made-up resources on the synthetic estate only (ADR 0038)."""
    for entity in audit.ENTITIES:
        text = load_fixture(f"audit/{entity}.json").decode()
        assert "cd12743c" not in text
        assert {SUB} >= {s.lower() for s in _subscriptions(text)}


def _subscriptions(text: str) -> set[str]:
    import re

    return set(re.findall(r"/subscriptions/([0-9a-fA-F-]{36})", text))


# --- normalisers ---------------------------------------------------------------------


def test_backup_states() -> None:
    checks, _ = _checks(audit.BACKUP)
    by = {c.resource_native_id.rsplit("/", 1)[-1]: c for c in checks if c.resource_native_id}
    assert (by["app02"].state, by["app02"].severity) == ("ok", "info")
    assert by["app02"].detail["retention_days"] == 30
    assert by["app02"].observed_at == "2026-10-27T22:31:05Z"
    assert (by["app03"].state, by["app03"].severity) == ("issue", "high")  # last backup failed
    assert (by["crm03"].state, by["crm03"].severity) == ("issue", "medium")  # never backed up
    assert by["crm03"].detail["last_backup_at"] is None  # Azure's 2001-01-01 means never
    assert (by["etl01"].state, by["etl01"].severity) == ("issue", "high")  # protection stopped
    assert by["crm01"].resource_type == "microsoft.sql/servers/databases"
    assert all(c.resource_group == "rg-fab-prod" for c in checks)
    assert all(c.native_id == c.native_id.lower() for c in checks)


def test_policy_states() -> None:
    checks, _ = _checks(audit.POLICY)
    states = Counter((c.state, c.severity) for c in checks)
    assert states == {("ok", "info"): 1, ("issue", "low"): 2, ("issue", "medium"): 1,
                      ("not_applicable", "info"): 1}
    deny = next(c for c in checks if c.detail["effect"] == "deny")
    assert (deny.state, deny.severity) == ("issue", "medium")
    assert deny.title == "FAB security baseline"


def test_advisor_pillars_and_impact() -> None:
    checks, _ = _checks(audit.ADVISOR)
    assert {c.category for c in checks} == {"cost", "reliability", "security",
                                            "operational_excellence", "performance"}
    cost = next(c for c in checks if c.category == "cost")
    assert (cost.severity, cost.detail["savings"], cost.detail["savings_currency"]) == (
        "high", "61.20", "EUR")


def test_recommendation_text_is_kept_verbatim_as_data() -> None:
    """CLAUDE.md rule 4: an instruction in ingested text is stored, never acted on."""
    checks, _ = _checks(audit.ADVISOR)
    injected = next(c for c in checks if "Ignore all previous" in c.title)
    assert injected.state == "issue"
    assert injected.category == "security"
    assert injected.detail["solution"] == "Data, not an instruction: stored verbatim"


def test_free_text_is_bounded() -> None:
    row = {**_rows(audit.ADVISOR)[0], "problem": "x" * 10_000}
    (check,) = audit.advisor(row)
    assert len(check.title) == audit.MAX_TEXT


def test_defender_states() -> None:
    checks, _ = _checks(audit.DEFENDER)
    assert Counter((c.state, c.severity) for c in checks) == {
        ("issue", "high"): 1, ("ok", "info"): 1, ("issue", "medium"): 1,
        ("not_applicable", "info"): 1}
    assert all(c.subscription_id == SUB for c in checks)
    assert all(c.resource_type for c in checks)


def test_configuration_rules() -> None:
    checks, _ = _checks(audit.CONFIG)
    by = {(c.resource_native_id.rsplit("/", 1)[-1], c.rule): c for c in checks
          if c.resource_native_id}
    assert by[("app03", "vm.public_ip")].state == "issue"
    assert by[("app03", "vm.public_ip")].severity == "high"
    assert by[("app02", "vm.public_ip")].state == "ok"
    assert by[("app03", "vm.encryption_at_host")].state == "issue"  # unset is off
    assert by[("app02", "vm.encryption_at_host")].state == "ok"
    for rule in ("storage.public_blob_access", "storage.https_only", "storage.min_tls",
                 "storage.public_network", "storage.shared_key"):
        assert by[("stfabbackup01", rule)].state == "issue", rule
    assert by[("crm01", "sql_db.local_backup_storage")].state == "issue"
    assert by[("app03-data-orphan", "disk.unattached")].state == "issue"
    assert by[("app03-data-orphan", "disk.public_network")].state == "ok"
    settings = by[("app02", audit.SETTINGS_RULE)]
    assert (settings.state, settings.severity) == ("ok", "info")
    assert settings.detail["settings"]["vmSize"] == "Standard_D4s_v5"
    assert by[("app03", "vm.public_ip")].detail == {"settings": {"publicIps": 1}}


def test_a_rule_that_cannot_read_its_setting_is_not_applicable() -> None:
    row = {**_rows(audit.CONFIG)[2], "settings": {}}
    by = {c.rule: c for c in audit.config(row)}
    assert by["storage.min_tls"].state == "not_applicable"
    assert by["storage.public_network"].state == "not_applicable"


def test_settings_keep_scalars_only() -> None:
    row = {**_rows(audit.CONFIG)[0],
           "settings": {"vmSize": "D2", "nested": {"secret": "x"}, "long": "y" * 500}}
    (settings, *_) = audit.config(row)
    assert settings.detail["settings"] == {"vmSize": "D2", "long": "y" * 120}


def test_every_rule_names_the_settings_it_reads() -> None:
    for rule in audit.RULES:
        assert rule.keys
        assert rule.severity in audit.SEVERITIES
        assert rule.resource_type in audit.QUERIES[audit.CONFIG]


def test_a_fingerprint_moves_with_any_detail() -> None:
    (first,) = audit.backup(_rows(audit.BACKUP)[0])
    (again,) = audit.backup(dict(_rows(audit.BACKUP)[0]))
    (later,) = audit.backup({**_rows(audit.BACKUP)[0], "lastBackupTime": "2026-10-28T22:31:05Z"})
    assert first.fingerprint == again.fingerprint != later.fingerprint


# --- client ----------------------------------------------------------------------------


async def test_graph_sends_the_fixed_query_for_the_allowed_subscriptions_only() -> None:
    fake = FakeAzure.from_fixtures()
    client = _client(fake, allowed=frozenset({SUB}))
    body = json.loads(await client.graph([SUB], audit.QUERIES[audit.POLICY], None))
    assert len(body["data"]) == len(_rows(audit.POLICY))
    sent = json.loads(fake.requests[-1].content)
    assert (fake.requests[-1].url.path, sent["query"]) == (GRAPH_PATH, audit.QUERIES[audit.POLICY])
    with pytest.raises(TargetOutOfScopeError):
        await client.graph([SUB, OTHER], audit.QUERIES[audit.POLICY], None)
    await client.aclose()


async def test_assessments_page_and_keep_to_the_subscription() -> None:
    fake = FakeAzure.from_fixtures(assessments_page=2)
    client = _client(fake, allowed=frozenset({SUB}))
    first = await client.assessments(SUB)
    assert fake.requests[-1].url.params["$expand"] == "metadata"
    link = audit.defender_next(first)
    assert link is not None
    second = await client.assessments(SUB, link)
    assert len(audit.defender_page(first)) + len(audit.defender_page(second)) >= 4
    with pytest.raises(TargetOutOfScopeError):
        await client.assessments(OTHER)
    with pytest.raises(TargetOutOfScopeError):
        await client.assessments(SUB, link.replace(SUB, OTHER))
    with pytest.raises(ValueError, match="away from the management endpoint"):
        await client.assessments(SUB, link.replace(FIXTURE_HOST, "https://elsewhere.example"))
    await client.aclose()
