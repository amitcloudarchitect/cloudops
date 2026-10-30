"""The Azure OpenAI provider against responses recorded from the dev resource
(ADR 0012; recorded 2026-10-30 through the private endpoint as the AI workload
identity). Each fixture is replayed exactly; only embeddings were shortened when
recorded, so they are padded back to their recorded size here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cloudops_gateway.azure_openai import API_VERSION, AzureOpenAIProvider
from cloudops_gateway.providers import (
    EMBEDDING_DIMENSIONS,
    Message,
    ProviderError,
    ProviderThrottledError,
    ProviderUnavailableError,
    ToolCallRequest,
    ToolSpec,
)

RECORDED = Path(__file__).parent / "recorded" / "azure_openai"
ENDPOINT = "https://oai-cloudops-dev-weu.openai.azure.com/"
TOKEN = "fake-bearer-for-tests-never-shown"
PROMPT = "customer ACME's disk on web-01 is full"
LOOKUP = ToolSpec("lookup_ci", "Look up a configuration item by name.", {
    "type": "object", "properties": {"name": {"type": "string"}},
    "required": ["name"], "additionalProperties": False})


def _fixture(case: str) -> tuple[int, Any]:
    data = json.loads((RECORDED / f"{case}.json").read_text(encoding="utf-8"))
    body = data["body"]
    if case == "embed":
        for row in body["data"]:
            row["embedding"] = [0.01] * row.pop("dimensions_seen")
    if isinstance(body, dict) and set(body) == {"text"}:
        return data["status"], body["text"]  # recorded as a non-JSON content type
    return data["status"], body


class Recorder:
    """Serves one recorded case and keeps what the provider sent."""

    def __init__(self, case: str | None = None, *, status: int = 200, body: Any = None,
                 error: Exception | None = None) -> None:
        if case is not None:
            status, body = _fixture(case)
        self.status, self.body, self.error = status, body, error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if isinstance(self.body, str):
            return httpx.Response(self.status, text=self.body)
        return httpx.Response(self.status, json=self.body)

    def sent(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.requests[-1].content)
        return body


async def _token() -> str:
    return TOKEN


def _provider(recorder: Recorder) -> AzureOpenAIProvider:
    return AzureOpenAIProvider(ENDPOINT, token=_token, transport=httpx.MockTransport(recorder))


async def test_a_completion_returns_text_and_the_reported_tokens() -> None:
    recorder = Recorder("chat_small")
    completion = await _provider(recorder).complete(
        "cloudops-small", [Message("system", "Reply with JSON only."), Message("user", PROMPT)],
        [], task="classify", max_tokens=200)
    assert completion.text == '{"ok":true}'
    assert (completion.tokens_in, completion.tokens_out) == (21, 8)  # Azure's, not estimated
    assert completion.tool_calls == ()
    request = recorder.requests[0]
    assert request.url.path == "/openai/deployments/cloudops-small/chat/completions"
    assert request.url.params["api-version"] == API_VERSION
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    sent = recorder.sent()
    assert sent["max_completion_tokens"] == 200
    assert "max_tokens" not in sent  # these models refuse it (chat_max_tokens_param)
    assert sent["messages"][1] == {"role": "user", "content": PROMPT}


async def test_a_tool_call_is_returned_as_untrusted_arguments() -> None:
    recorder = Recorder("chat_tool_call")
    completion = await _provider(recorder).complete(
        "cloudops-small", [Message("user", "Look up web-01.")], [LOOKUP],
        task="enrich", max_tokens=200)
    (call,) = completion.tool_calls
    assert (call.name, call.arguments) == ("lookup_ci", '{"name":"web-01"}')
    assert completion.text == ""
    assert recorder.sent()["tools"] == [{"type": "function", "function": {
        "name": "lookup_ci", "description": LOOKUP.description,
        "parameters": dict(LOOKUP.parameters)}}]


async def test_a_tool_result_is_sent_back_against_its_call() -> None:
    recorder = Recorder("chat_small")
    call = ToolCallRequest("call_1", "lookup_ci", '{"name":"web-01"}')
    await _provider(recorder).complete("cloudops-small", [
        Message("assistant", "", tool_calls=(call,)),
        Message("tool", '{"ci":"web-01"}', tool_call_id="call_1"),
    ], [LOOKUP], task="enrich", max_tokens=200)
    assistant, tool = recorder.sent()["messages"]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert tool == {"role": "tool", "content": '{"ci":"web-01"}', "tool_call_id": "call_1"}


async def test_embeddings_come_back_in_order_and_at_the_index_size() -> None:
    recorder = Recorder("embed")
    vectors = await _provider(recorder).embed("cloudops-embed", ["a", "b"])
    assert [len(v) for v in vectors] == [EMBEDDING_DIMENSIONS, EMBEDDING_DIMENSIONS]
    assert recorder.requests[0].url.path == "/openai/deployments/cloudops-embed/embeddings"


async def test_an_embedding_of_another_size_is_refused() -> None:
    _, body = _fixture("embed")
    for row in body["data"]:
        row["embedding"] = row["embedding"][:768]  # e.g. a model swapped for a smaller one
    with pytest.raises(ProviderError, match="1536"):
        await _provider(Recorder(body=body)).embed("cloudops-embed", ["a", "b"])


@pytest.mark.parametrize(("recorder", "expected"), [
    (Recorder("missing_deployment"), ProviderUnavailableError),
    (Recorder(status=429, body={"error": {"code": "429", "message": "Rate limit"}}),
     ProviderThrottledError),
    (Recorder(status=503, body={"error": {"code": "ServiceUnavailable"}}),
     ProviderUnavailableError),
    (Recorder(error=httpx.ConnectTimeout("timed out")), ProviderUnavailableError),
    (Recorder(error=httpx.ConnectError("refused")), ProviderUnavailableError),
    (Recorder("chat_max_tokens_param"), ProviderError),
    (Recorder(status=400, body={"error": {"code": "content_filter",
                                          "message": f"The prompt '{PROMPT}' was filtered"}}),
     ProviderError),
], ids=["404", "429", "503", "timeout", "connect", "400-param", "400-filter"])
async def test_errors_map_to_fallback_and_never_carry_content(
    recorder: Recorder, expected: type[ProviderError],
) -> None:
    with pytest.raises(ProviderError) as info:
        await _provider(recorder).complete("cloudops-large", [Message("user", PROMPT)], [],
                                           task="plan", max_tokens=200)
    assert type(info.value) is expected
    message = str(info.value)
    assert PROMPT not in message
    assert "ACME" not in message
    assert TOKEN not in message


async def test_an_unexpected_shape_is_an_error_not_a_guess() -> None:
    recorder = Recorder(body={"choices": []})
    with pytest.raises(ProviderError, match="shape"):
        await _provider(recorder).complete("cloudops-small", [Message("user", "hi")], [],
                                           task="t", max_tokens=10)


def test_the_endpoint_must_be_https() -> None:
    with pytest.raises(ValueError, match="https"):
        AzureOpenAIProvider("http://oai-cloudops-dev-weu.openai.azure.com", token=_token)
