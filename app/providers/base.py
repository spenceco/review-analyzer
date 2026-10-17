"""The provider interface. Anything that can turn a prompt plus a JSON Schema into
JSON can power the analyzer: Gemini today, Claude or OpenAI with one new file."""

from __future__ import annotations

from typing import Any, Protocol

JSONSchema = dict[str, Any]


class ProviderError(Exception):
    """Base class for errors raised by a provider."""


class TransientProviderError(ProviderError):
    """A failure worth retrying: rate limits, 5xx responses, timeouts, malformed output."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentProviderError(ProviderError):
    """A failure retrying won't fix: bad API key, invalid request, blocked content."""


class LLMProvider(Protocol):
    """A model that returns structured JSON.

    Implementations must:
      * return the parsed JSON value (not a string) matching ``schema`` as closely
        as the model allows; the caller validates it again with pydantic;
      * raise ``TransientProviderError`` for retryable failures and
        ``PermanentProviderError`` for everything else.
    Retries, batching and the daily call budget are handled by the caller, so a
    provider makes exactly one HTTP request per call.
    """

    name: str

    async def generate_json(self, *, system: str, prompt: str, schema: JSONSchema) -> Any: ...

    async def aclose(self) -> None: ...
