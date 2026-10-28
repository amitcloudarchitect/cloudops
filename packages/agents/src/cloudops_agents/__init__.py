"""cloudops_agents — the agent layer, a few hundred lines with no framework.

An agent is a versioned prompt file, a model tier, a tool allowlist, a Pydantic
output schema and a confidence threshold (``base.AgentSpec``). Temporal owns
control flow; agents fill in content inside activities (``temporal``). See
README.md for adding one.
"""

from cloudops_agents.base import (
    Agent,
    AgentError,
    AgentEscalationError,
    AgentOutput,
    AgentOutputError,
    AgentResult,
    AgentSpec,
)
from cloudops_agents.prompts import DataItem, PromptTemplate, build_messages

__all__ = [
    "Agent",
    "AgentError",
    "AgentEscalationError",
    "AgentOutput",
    "AgentOutputError",
    "AgentResult",
    "AgentSpec",
    "DataItem",
    "PromptTemplate",
    "build_messages",
]
