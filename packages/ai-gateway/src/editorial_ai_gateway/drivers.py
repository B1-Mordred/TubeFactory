from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote

from editorial_ai_gateway.gateway import DriverResult, JSONHTTPDriver, ProviderRoute


_GRAMMAR_UNSUPPORTED_SCHEMA_KEYWORDS = {
    "$id",
    "$schema",
    "title",
    "description",
    "default",
    "examples",
    "format",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
}


def provider_compatible_schema(value: Any) -> Any:
    """Keep structural grammar while local validation enforces every constraint."""

    if isinstance(value, dict):
        return {
            key: provider_compatible_schema(child)
            for key, child in value.items()
            if key not in _GRAMMAR_UNSUPPORTED_SCHEMA_KEYWORDS
        }
    if isinstance(value, list):
        return [provider_compatible_schema(item) for item in value]
    return value


def _content_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError("model provider omitted structured output")
    candidate = value.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        first_newline = candidate.find("\n")
        if first_newline != -1:
            candidate = candidate[first_newline + 1 : -3].strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("model provider content was not JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("model provider content root must be an object")
    return parsed


def _auth_headers(route: ProviderRoute, secret: str | None) -> dict[str, str]:
    if route.authentication_scheme == "none":
        if secret is not None:
            raise ValueError("credential-free route unexpectedly received a secret")
        return {}
    if not secret:
        raise ValueError("configured provider credential is unavailable")
    if route.authentication_scheme in {"bearer", "oauth"}:
        return {"Authorization": f"Bearer {secret}"}
    return {"X-API-Key": secret}


class OpenAICompatibleDriver(JSONHTTPDriver):
    async def generate(self, *, route, system_instructions, rendered_prompt, response_schema, deadline, secret):
        payload = await self.post(
            f"{route.endpoint.rstrip('/')}/chat/completions",
            headers=_auth_headers(route, secret),
            body={
                "model": route.model_name,
                "messages": [
                    {"role": "system", "content": system_instructions},
                    {"role": "user", "content": rendered_prompt},
                ],
                "response_format": {"type": "json_schema", "json_schema": {"name": "editorial_output", "strict": True, "schema": provider_compatible_schema(response_schema)}},
                "temperature": 0,
                "max_tokens": route.output_limit,
            },
            deadline=deadline,
        )
        try:
            output = _content_json(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("OpenAI-compatible response omitted message content") from exc
        usage = payload.get("usage", {})
        return DriverResult(output, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))


class OllamaDriver(JSONHTTPDriver):
    async def generate(self, *, route, system_instructions, rendered_prompt, response_schema, deadline, secret):
        payload = await self.post(
            f"{route.endpoint.rstrip('/')}/api/chat",
            headers=_auth_headers(route, secret),
            body={
                "model": route.model_name,
                "messages": [
                    {"role": "system", "content": system_instructions},
                    {"role": "user", "content": rendered_prompt},
                ],
                "format": provider_compatible_schema(response_schema),
                "stream": False,
                "options": {"temperature": 0, "num_predict": route.output_limit},
            },
            deadline=deadline,
        )
        try:
            output = _content_json(payload["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise ValueError("Ollama response omitted message content") from exc
        return DriverResult(output, int(payload.get("prompt_eval_count", 0)), int(payload.get("eval_count", 0)))


class AnthropicDriver(JSONHTTPDriver):
    async def generate(self, *, route, system_instructions, rendered_prompt, response_schema, deadline, secret):
        headers = _auth_headers(route, secret)
        if "X-API-Key" in headers:
            headers["x-api-key"] = headers.pop("X-API-Key")
        headers["anthropic-version"] = "2023-06-01"
        payload = await self.post(
            f"{route.endpoint.rstrip('/')}/v1/messages",
            headers=headers,
            body={
                "model": route.model_name,
                "system": system_instructions,
                "messages": [{"role": "user", "content": rendered_prompt}],
                "max_tokens": route.output_limit,
                "temperature": 0,
            },
            deadline=deadline,
        )
        try:
            text = next(item["text"] for item in payload["content"] if item.get("type") == "text")
            output = _content_json(text)
        except (KeyError, StopIteration, TypeError) as exc:
            raise ValueError("Anthropic response omitted text content") from exc
        usage = payload.get("usage", {})
        return DriverResult(output, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))


class GeminiDriver(JSONHTTPDriver):
    async def generate(self, *, route, system_instructions, rendered_prompt, response_schema, deadline, secret):
        headers = _auth_headers(route, secret)
        if "X-API-Key" in headers:
            headers["x-goog-api-key"] = headers.pop("X-API-Key")
        payload = await self.post(
            f"{route.endpoint.rstrip('/')}/v1beta/models/{quote(route.model_name, safe='')}:generateContent",
            headers=headers,
            body={
                "systemInstruction": {"parts": [{"text": system_instructions}]},
                "contents": [{"role": "user", "parts": [{"text": rendered_prompt}]}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": route.output_limit,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": response_schema,
                },
            },
            deadline=deadline,
        )
        try:
            output = _content_json(payload["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Gemini response omitted candidate content") from exc
        usage = payload.get("usageMetadata", {})
        return DriverResult(output, int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0)))


class GenericRESTDriver(JSONHTTPDriver):
    async def generate(self, *, route, system_instructions, rendered_prompt, response_schema, deadline, secret):
        payload = await self.post(
            route.endpoint,
            headers=_auth_headers(route, secret),
            body={
                "model": route.model_name,
                "system": system_instructions,
                "input": rendered_prompt,
                "response_schema": response_schema,
                "max_output_tokens": route.output_limit,
            },
            deadline=deadline,
        )
        output = _content_json(payload.get("output", payload))
        usage = payload.get("usage", {})
        return DriverResult(output, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))
