"""The ticket classifier: stage 2 of the remediation pipeline (HLD, Pipeline).

Small tier, no tools: it classifies, nothing else. Its input is a ServiceNow
ticket, which is untrusted (CLAUDE.md rule 4), so the ticket goes in a data
section, and every field that becomes a decision is a closed list. Planted
instructions can at most push it towards an allowed value, and the
deterministic guardrails in ``triage.py`` stand between that and any action.

``customer`` and ``affected_ci`` are what the ticket *says*, as written. The
platform never takes scope from them. The tenant is already known
server-side, and the CI is resolved by the platform within that tenant.
``customer`` is used only as a cross-check: a ticket naming another customer
goes to a human.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import Field

from cloudops_agents.base import AgentOutput, AgentSpec
from cloudops_agents.prompts import DataItem, PromptTemplate
from cloudops_gateway.providers import Tier

Intent = Literal[
    "service_down", "performance", "disk_space", "backup_failure", "certificate_expiry",
    "patching", "capacity_change", "access_request", "connectivity", "security_alert",
    "decommission", "information_request", "other",
]
ActionClass = Literal["A", "B", "C", "D", "E"]
Risk = Literal["low", "medium", "high"]


class TicketTriage(AgentOutput):
    intent: Intent
    affected_ci: str | None = Field(default=None, min_length=1, max_length=200)
    customer: str | None = Field(default=None, min_length=1, max_length=200)
    action_class: ActionClass
    risk: Risk
    rationale: str = Field(min_length=1, max_length=500)


CLASSIFIER: Final = AgentSpec(
    name="classify_ticket",
    prompt=PromptTemplate.load("ticket_classifier", "v2"),
    tier=Tier.SMALL,
    output=TicketTriage,
    confidence_threshold=0.7,
)

# The ticket fields the classifier sees: what a person reading it would see.
TICKET_FIELDS: Final = ("number", "short_description", "configuration_item", "priority",
                        "state", "close_notes", "company")


def ticket_data(ticket: Mapping[str, Any]) -> DataItem:
    """The ticket as the first data section; its number is the section's ref."""
    return DataItem(
        source="servicenow",
        ref=str(ticket.get("number") or ""),
        content={k: ticket.get(k) for k in TICKET_FIELDS if ticket.get(k) is not None},
    )
