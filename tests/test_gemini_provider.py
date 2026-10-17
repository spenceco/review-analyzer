"""The Gemini provider against a mocked HTTP transport (no network, no key needed)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.prompts import CLASSIFY_SCHEMA
from app.providers import PermanentProviderError, TransientProviderError
from app.providers.gemini import GeminiProvider, to_gemini_schema

Handler = Callable[[httpx.Request], httpx.Response]


def call(handler: Handler) -> Any:
    async def go() -> Any:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GeminiProvider(api_key="test-key", model="gemini-test", client=client)
        try:
            return await provider.generate_json(system="sys", prompt="hi", schema=CLASSIFY_SCHEMA)
        finally:
            await provider.aclose()

    return asyncio.run(go())


def answer(text: str) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]},
                                                     "finishReason": "STOP"}]})


def test_success_sends_key_in_header_and_schema() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return answer('{"reviews": []}')

    assert call(handler) == {"reviews": []}
    assert seen["url"].endswith("/models/gemini-test:generateContent")
    assert "test-key" not in seen["url"] and seen["key"] == "test-key"
    config = seen["body"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"]["type"] == "OBJECT"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "sys"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_status_codes(status: int) -> None:
    with pytest.raises(TransientProviderError) as info:
        call(lambda r: httpx.Response(status, headers={"retry-after": "3"}))
    assert info.value.retry_after == 3


def test_client_errors_are_permanent() -> None:
    body = {"error": {"message": "API key not valid"}}
    with pytest.raises(PermanentProviderError, match="API key not valid"):
        call(lambda r: httpx.Response(400, json=body))


def test_invalid_json_is_transient() -> None:
    with pytest.raises(TransientProviderError, match="invalid JSON"):
        call(lambda r: answer('{"reviews": [ truncated'))


def test_blocked_prompt_is_permanent() -> None:
    with pytest.raises(PermanentProviderError, match="SAFETY"):
        call(lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))


def test_network_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(TransientProviderError):
        call(handler)


def test_schema_conversion() -> None:
    converted = to_gemini_schema(CLASSIFY_SCHEMA)
    item = converted["properties"]["reviews"]["items"]
    assert item["type"] == "OBJECT"
    assert item["propertyOrdering"] == ["id", "sentiment", "topic", "summary"]
    assert item["properties"]["sentiment"]["enum"] == ["positive", "neutral", "negative"]
    assert item["properties"]["id"]["type"] == "INTEGER"
