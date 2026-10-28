"""The ticket classifier: the first agent on the base, small tier, no tools.

Input is a ServiceNow ticket, which is untrusted text (CLAUDE.md rule 4): it
goes in a data section, and the output schema is closed, so instructions
planted in a ticket can at most lower the classifier's confidence. They can
never add a field, an action or a category that does not exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import Field

from cloudops_agents.base import AgentOutput, AgentSpec
from cloudops_agents.prompts import DataItem, PromptTemplate
from cloudops_gateway.providers import Tier

Category = Literal[
    "compute.vm", "compute.function", "database.sql", "storage.account", "web.app",
    "network", "identity", "backup", "security", "other",
]
Priority = Literal["1 - Critical", "2 - High", "3 - Moderate", "4 - Low"]


class TicketClassification(AgentOutput):
    category: Category
    subcategory: str = Field(pattern=r"^[a-z][a-z_]{1,48}$")
    priority_suggestion: Priority
    rationale: str = Field(min_length=1, max_length=500)


CLASSIFIER: Final = AgentSpec(
    name="classify_ticket",
    prompt=PromptTemplate.load("ticket_classifier", "v1"),
    tier=Tier.SMALL,
    output=TicketClassification,
    confidence_threshold=0.6,
)

# The ticket fields the classifier sees: what a person reading it would see.
TICKET_FIELDS: Final = ("number", "short_description", "configuration_item", "priority",
                        "state", "close_notes")


def ticket_data(ticket: Mapping[str, Any]) -> DataItem:
    return DataItem(
        source="servicenow",
        ref=str(ticket.get("number") or ""),
        content={k: ticket.get(k) for k in TICKET_FIELDS if ticket.get(k) is not None},
    )
