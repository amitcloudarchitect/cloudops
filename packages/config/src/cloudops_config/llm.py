"""The LLM gateway interface, and the mock that serves it locally.

Application code names a tier, never a model (LLD, AI platform configuration).
The real gateway — routing, budgets, redaction, tool authorisation — is built
in step 06 against this mock, and may reshape the interface when it is.

The mock returns canned responses keyed by task. It never looks at the prompt,
so it cannot follow instructions planted in one; an unknown task is an error,
not an empty string, so a missing canned response fails the test that needs it.
"""

from __future__ import annotations

import enum
import hashlib
import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Final, Protocol

from cloudops_config.errors import ConfigurationError

# knowledge_chunk.embedding is vector(1536) (db/migrations 0001).
EMBEDDING_DIMENSIONS: Final = 1536


class Tier(enum.StrEnum):
    SMALL = "small"
    LARGE = "large"
    EMBED = "embed"


class UnknownTaskError(ConfigurationError):
    """The mock has no canned response for this task."""


class LlmGateway(Protocol):
    async def complete(self, *, tier: Tier, task: str, prompt: str) -> str:
        """Text (often JSON for the caller to schema-validate) for one bounded task."""
        ...

    async def embed(self, text: str) -> list[float]:
        """A unit-length embedding of ``EMBEDDING_DIMENSIONS`` floats."""
        ...


@dataclass(frozen=True, slots=True)
class Call:
    tier: Tier
    task: str
    prompt: str


@dataclass(slots=True)
class MockLlmGateway:
    """Canned completions and deterministic embeddings. Records every call."""

    canned: Mapping[str, str]
    calls: list[Call] = field(default_factory=list)

    @classmethod
    def with_packaged_responses(cls) -> MockLlmGateway:
        path = resources.files("cloudops_config").joinpath("canned_responses.json")
        raw = path.read_text("utf-8")
        data = json.loads(raw)
        canned = {task: json.dumps(body) if not isinstance(body, str) else body
                  for task, body in data["tasks"].items()}
        return cls(canned=canned)

    async def complete(self, *, tier: Tier, task: str, prompt: str) -> str:
        if type(tier) is not Tier or tier is Tier.EMBED:
            raise ConfigurationError("complete() takes the small or large tier")
        self.calls.append(Call(tier, task, prompt))
        try:
            return self.canned[task]
        except KeyError:
            raise UnknownTaskError(f"no canned response for task {task!r}") from None

    async def embed(self, text: str) -> list[float]:
        self.calls.append(Call(Tier.EMBED, "embed", text))
        return deterministic_embedding(text)


def deterministic_embedding(text: str) -> list[float]:
    """Same text, same vector; different texts, near-orthogonal vectors."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)  # noqa: S311 - test data, not cryptography
    vec = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIMENSIONS)]
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]
