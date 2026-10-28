"""Replayed model responses ("cassettes"), so the evaluation suite is
deterministic in CI.

``ReplayProvider`` answers each gateway call from ``evals/cassettes/*.jsonl``,
keyed by the case (the ticket number in the prompt's data section) and the
agent's task. Its replies are consumed in order: a tool call, then a final
answer. A case with no cassette for a task fails loudly; it never falls back
to a canned answer.

``RecordingProvider`` wraps a real provider and writes what it said in the
same format. Re-recording the golden set against the tier models is how a
prompt or model change gets measured.

**Today's cassettes are authored, not recorded.** No tier model exists yet
(LLD open item L12), so they are written to behave like a capable but
fallible model: mostly right, sometimes wrong, and in the injection cases
sometimes obeying the planted text. Until real recordings replace them, the
gate measures the pipeline (schemas, guardrails, routing, isolation), not
model quality.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cloudops_config.llm import deterministic_embedding
from cloudops_gateway.providers import (
    Completion,
    Message,
    Provider,
    ProviderUnavailableError,
    ToolCallRequest,
    ToolSpec,
    estimate_tokens,
)

_REF = re.compile(r'<data id="[0-9a-f]+" index="1" source="servicenow" ref="([^"]+)">')
_TICKET = re.compile(
    r'<data id="([0-9a-f]+)" index="1" source="servicenow" ref="[^"]+">\n(.*?)\n</data id="\1">',
    re.S)


def _default_enrichment(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """For a case that reached enrichment although its golden entry expected it
    to stop earlier (say, because a guardrail was removed): look the CI up, then
    return a plain bundle that cites nothing. This lets such a case run to its
    end and be scored, instead of crashing the suite."""
    ci = None
    for m in messages:
        found = _TICKET.search(m.content)
        if found:
            try:
                ci = json.loads(found.group(2)).get("configuration_item")
            except ValueError:
                ci = None
            break
    return [
        {"tool_calls": [{"id": "d1", "name": "get_resource_context",
                         "arguments": {"resource": str(ci or "unknown")}}]},
        {"text": {"resource_found": True, "resource_summary": None, "related_tickets": [],
                  "prior_resolutions": [], "runbook_candidates": [],
                  "missing_context": ["recent_changes", "telemetry"],
                  "summary": "Default replay bundle.", "confidence": 0.8}},
    ]


def case_key(messages: Sequence[Message]) -> str | None:
    for m in messages:
        found = _REF.search(m.content)
        if found:
            return found.group(1)
    return None


def _completion(reply: dict[str, Any], messages: Sequence[Message]) -> Completion:
    calls = tuple(
        ToolCallRequest(c.get("id", f"t{i}"), c["name"], json.dumps(c.get("arguments", {})))
        for i, c in enumerate(reply.get("tool_calls", []))
    )
    text = reply.get("text", "")
    if not isinstance(text, str):
        text = json.dumps(text)
    tokens_in = sum(estimate_tokens(m.content) for m in messages)
    return Completion(text, calls, tokens_in, estimate_tokens(text) or 1)


@dataclass
class ReplayProvider:
    name: str
    replies: dict[tuple[str, str], deque[dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def from_directory(cls, directory: Path, name: str = "mock") -> ReplayProvider:
        replies: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
        for path in sorted(directory.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    replies[(entry["ticket"], entry["task"])].extend(entry["replies"])
        return cls(name, dict(replies))

    async def complete(self, deployment: str, messages: Sequence[Message],
                       tools: Sequence[ToolSpec], *, task: str, max_tokens: int) -> Completion:
        key = case_key(messages)
        queue = self.replies.get((key or "", task))
        if queue is None and task == "enrich_ticket" and key is not None:
            queue = self.replies[(key, task)] = deque(_default_enrichment(messages))
        if not queue:
            raise ProviderUnavailableError(f"no cassette reply for {key}/{task}")
        return _completion(queue.popleft(), messages)

    async def embed(self, deployment: str, texts: Sequence[str]) -> list[list[float]]:
        return [deterministic_embedding(t) for t in texts]


@dataclass
class RecordingProvider:
    """Wraps a real provider; appends every reply to ``out`` as cassette lines."""

    inner: Provider
    out: Path

    @property
    def name(self) -> str:
        return self.inner.name

    async def complete(self, deployment: str, messages: Sequence[Message],
                       tools: Sequence[ToolSpec], *, task: str, max_tokens: int) -> Completion:
        result = await self.inner.complete(deployment, messages, tools, task=task,
                                           max_tokens=max_tokens)
        entry = {"ticket": case_key(messages), "task": task, "replies": [{
            "text": result.text,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": json.loads(c.arguments or "{}")}
                for c in result.tool_calls
            ],
        }]}
        with self.out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        return result

    async def embed(self, deployment: str, texts: Sequence[str]) -> list[list[float]]:
        return await self.inner.embed(deployment, texts)
