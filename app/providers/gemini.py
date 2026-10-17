"""Google Gemini provider, using the REST API directly via httpx.

Calling REST instead of the SDK keeps dependencies small and lets us map HTTP
status codes onto our transient/permanent error split precisely.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .base import JSONSchema, PermanentProviderError, TransientProviderError

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

# JSON Schema keywords Gemini's response schema understands. Others are dropped.
_SUPPORTED_KEYS = {"description", "enum", "required", "minItems", "maxItems", "nullable",
                   "minimum", "maximum", "format"}


def to_gemini_schema(schema: JSONSchema) -> dict[str, Any]:
    """Convert a plain JSON Schema into Gemini's OpenAPI-style schema
    (upper-case type names, explicit property ordering)."""
    out: dict[str, Any] = {k: v for k, v in schema.items() if k in _SUPPORTED_KEYS}
    out["type"] = str(schema["type"]).upper()
    if "properties" in schema:
        out["properties"] = {k: to_gemini_schema(v) for k, v in schema["properties"].items()}
        out["propertyOrdering"] = list(schema["properties"])
    if "items" in schema:
        out["items"] = to_gemini_schema(schema["items"])
    return out


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    try:
        return float(header) if header else None
    except ValueError:
        return None


class GeminiProvider:
    name = "gemini"

    def __init__(self, *, api_key: str, model: str, timeout: float = 60.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.model = model
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._headers = {"x-goog-api-key": api_key, "content-type": "application/json"}

    async def generate_json(self, *, system: str, prompt: str, schema: JSONSchema) -> Any:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": to_gemini_schema(schema),
            },
        }
        url = f"{API_ROOT}/models/{self.model}:generateContent"
        try:
            response = await self._client.post(url, headers=self._headers, json=body)
        except httpx.TimeoutException as exc:
            raise TransientProviderError("Gemini request timed out") from exc
        except httpx.TransportError as exc:
            raise TransientProviderError(f"Network error talking to Gemini: {exc}") from exc

        if response.status_code in RETRYABLE_STATUS:
            raise TransientProviderError(
                f"Gemini returned HTTP {response.status_code}",
                retry_after=_retry_after(response),
            )
        if response.status_code >= 400:
            # Never echo the request (it contains the key header); the error body is safe.
            try:
                detail = response.json().get("error", {}).get("message", "")
            except ValueError:
                detail = response.text[:200]
            raise PermanentProviderError(f"Gemini returned HTTP {response.status_code}: {detail}")

        payload = response.json()
        if block := payload.get("promptFeedback", {}).get("blockReason"):
            raise PermanentProviderError(f"Gemini blocked the request ({block})")
        candidates = payload.get("candidates") or []
        if not candidates:
            raise TransientProviderError("Gemini returned no candidates")
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise TransientProviderError(
                f"Gemini returned an empty answer (finishReason={candidate.get('finishReason')})"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            # Usually a truncated answer (MAX_TOKENS); a retry normally succeeds.
            raise TransientProviderError("Gemini returned invalid JSON") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
