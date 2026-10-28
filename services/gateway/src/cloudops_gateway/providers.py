"""Model providers behind the gateway. Application code never sees these.

A ``Provider`` serves deployments by alias (``cloudops-small``…); the gateway
maps a tier to an ordered list of deployments across providers (primary, then
secondary) and handles fallback. Providers raise ``ProviderThrottledError`` or
``ProviderUnavailableError``; anything else is a bug and propagates.

``MockProvider`` is the local and test provider: canned completions by task,
deterministic embeddings, and a script for tests (tool calls, throttling,
outages). It records exactly what it was sent, so tests can prove that only
redacted text ever reached a model.

The Azure OpenAI provider is not built: the model per tier is LLD open item
L12, and it is reached by private endpoint with managed identity, which only
exists in Azure.
"""

from __future__ import annotations

import enum
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from cloudops_config.llm import EMBEDDING_DIMENSIONS, MockLlmGateway, deterministic_embedding


class Tier(enum.StrEnum):
    SMALL = "small"
    LARGE = "large"
    EMBED = "embed"


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """A tool call as the model emits it. ``arguments`` is the model's raw JSON
    text: untrusted until the registry has validated it."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What a model is told about a tool: name, purpose, argument schema."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    tool_calls: tuple[ToolCallRequest, ...]
    tokens_in: int
    tokens_out: int


class ProviderError(Exception):
    pass


class ProviderThrottledError(ProviderError):
    """429 or quota: try the next deployment."""


class ProviderUnavailableError(ProviderError):
    """Down or not deployed: try the next deployment."""


class Provider(Protocol):
    name: str

    async def complete(
        self, deployment: str, messages: Sequence[Message], tools: Sequence[ToolSpec],
        *, task: str, max_tokens: int,
    ) -> Completion: ...

    async def embed(self, deployment: str, texts: Sequence[str]) -> list[list[float]]: ...


def estimate_tokens(text: str) -> int:
    """Roughly four characters a token. Real providers report exact counts."""
    return max(1, math.ceil(len(text) / 4)) if text else 0


@dataclass
class MockProvider:
    """Canned and scripted responses. ``script`` maps a task to a queue of
    ``Completion``s or exceptions, consumed in order; unscripted tasks get the
    canned response. ``down`` lists deployments that are unavailable and
    ``throttled`` deployments that answer 429."""

    name: str = "mock"
    canned: Mapping[str, str] = field(
        default_factory=lambda: MockLlmGateway.with_packaged_responses().canned
    )
    script: dict[str, deque[Completion | Exception]] = field(default_factory=dict)
    down: set[str] = field(default_factory=set)
    throttled: set[str] = field(default_factory=set)
    sent: list[tuple[str, tuple[Message, ...]]] = field(default_factory=list)
    embedded: list[str] = field(default_factory=list)

    def queue(self, task: str, *items: Completion | Exception) -> None:
        self.script.setdefault(task, deque()).extend(items)

    def _check(self, deployment: str) -> None:
        if deployment in self.down:
            raise ProviderUnavailableError(deployment)
        if deployment in self.throttled:
            raise ProviderThrottledError(deployment)

    async def complete(
        self, deployment: str, messages: Sequence[Message], tools: Sequence[ToolSpec],
        *, task: str, max_tokens: int,
    ) -> Completion:
        self._check(deployment)
        self.sent.append((deployment, tuple(messages)))
        tokens_in = sum(estimate_tokens(m.content) for m in messages)
        queued = self.script.get(task)
        if queued:
            item = queued.popleft()
            if isinstance(item, Exception):
                raise item
            return Completion(item.text, item.tool_calls, tokens_in, item.tokens_out)
        text = self.canned.get(task)
        if text is None:
            raise ProviderUnavailableError(f"no canned response for task {task!r}")
        return Completion(text, (), tokens_in, estimate_tokens(text))

    async def embed(self, deployment: str, texts: Sequence[str]) -> list[list[float]]:
        self._check(deployment)
        self.embedded.extend(texts)
        return [deterministic_embedding(t) for t in texts]


__all__ = [
    "EMBEDDING_DIMENSIONS",
    "Completion",
    "Message",
    "MockProvider",
    "Provider",
    "ProviderError",
    "ProviderThrottledError",
    "ProviderUnavailableError",
    "Tier",
    "ToolCallRequest",
    "ToolSpec",
    "estimate_tokens",
]
