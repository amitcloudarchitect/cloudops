"""Azure OpenAI behind the gateway (ADR 0012).

    provider = AzureOpenAIProvider(endpoint, token=bearer_token)

One resource, reached over its private endpoint, authenticated with an Entra
token for the workload identity: ``token`` returns a bearer token for
``TOKEN_SCOPE`` (the worker passes azure-identity's, which caches and renews).
There is no API key; the account has key authentication turned off.

Deployments are the tier aliases (``cloudops-small``...), never model names.
Errors map onto the gateway's fallback: 429 is ``ProviderThrottledError``; a
5xx, a timeout, a dropped connection or a missing deployment is
``ProviderUnavailableError``; anything else (a content-filter refusal, a bad
request) is ``ProviderError``. Messages carry the HTTP status and Azure's error
code only, never prompt text, completion text or the token.

Shapes are pinned by contract tests against responses recorded from the dev
resource (tests/recorded/azure_openai).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from cloudops_gateway.providers import (
    EMBEDDING_DIMENSIONS,
    Completion,
    Message,
    ProviderError,
    ProviderThrottledError,
    ProviderUnavailableError,
    ToolCallRequest,
    ToolSpec,
)

TOKEN_SCOPE: Final = "https://cognitiveservices.azure.com/.default"  # noqa: S105 - a scope
API_VERSION: Final = "2024-10-21"  # GA; the recorded fixtures were made with it
TIMEOUT_SECONDS: Final = 60.0


def _message(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.role == "tool":
        out["tool_call_id"] = m.tool_call_id
    if m.tool_calls:
        out["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in m.tool_calls
        ]
    return out


def _tool(t: ToolSpec) -> dict[str, Any]:
    return {"type": "function",
            "function": {"name": t.name, "description": t.description,
                         "parameters": dict(t.parameters)}}


def _error_code(response: httpx.Response) -> str:
    try:
        body = json.loads(response.text)
    except ValueError:
        return "unparseable"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "unknown")[:64]
    return "unknown"


class AzureOpenAIProvider:
    def __init__(
        self,
        endpoint: str,
        *,
        token: Callable[[], Awaitable[str]],
        name: str = "azure-openai",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("the Azure OpenAI endpoint must be https")
        self.name = name
        self._base = f"https://{parts.netloc}/openai/deployments"
        self._token = token
        self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, deployment: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            response = await self._client.post(
                f"{self._base}/{deployment}/{path}", params={"api-version": API_VERSION},
                json=body, headers=headers,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailableError(f"{deployment}: {type(exc).__name__}") from None
        status = response.status_code
        if status == 200:
            data: dict[str, Any] = response.json()
            return data
        code = _error_code(response)
        detail = f"{deployment}: HTTP {status} ({code})"
        if status == 429:
            raise ProviderThrottledError(detail)
        if status >= 500 or status == 404:
            raise ProviderUnavailableError(detail)
        raise ProviderError(detail)

    async def complete(
        self, deployment: str, messages: Sequence[Message], tools: Sequence[ToolSpec],
        *, task: str, max_tokens: int,
    ) -> Completion:
        # These models refuse max_tokens; max_completion_tokens is the bound.
        body: dict[str, Any] = {"messages": [_message(m) for m in messages],
                                "max_completion_tokens": max_tokens}
        if tools:
            body["tools"] = [_tool(t) for t in tools]
        data = await self._post(deployment, "chat/completions", body)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            usage = data["usage"]
            calls = tuple(
                ToolCallRequest(c["id"], c["function"]["name"], c["function"]["arguments"])
                for c in message.get("tool_calls") or ()
            )
            return Completion(message.get("content") or "", calls,
                              int(usage["prompt_tokens"]), int(usage["completion_tokens"]))
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError(f"{deployment}: unexpected response shape") from None

    async def embed(self, deployment: str, texts: Sequence[str]) -> list[list[float]]:
        data = await self._post(deployment, "embeddings", {"input": list(texts)})
        try:
            rows = sorted(data["data"], key=lambda d: int(d["index"]))
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
        except (KeyError, TypeError, ValueError):
            raise ProviderError(f"{deployment}: unexpected response shape") from None
        if len(vectors) != len(texts):
            raise ProviderError(f"{deployment}: {len(vectors)} embeddings for {len(texts)} texts")
        # A different size would silently corrupt the vector(1536) index.
        if any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            raise ProviderError(
                f"{deployment}: embeddings are not {EMBEDDING_DIMENSIONS}-dimensional")
        return vectors
