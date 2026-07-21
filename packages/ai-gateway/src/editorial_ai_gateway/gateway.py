from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Awaitable, Callable, Literal, Protocol

import aiohttp
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from urllib.parse import urlsplit


_SENSITIVE_KEYS = (
    "secret",
    "password",
    "token",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d .()-]{7,}\d)(?!\w)")


class GatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    structured_inputs: dict[str, Any]
    references: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    response_schema: dict[str, Any]
    sensitivity: Literal["public", "internal", "sensitive", "restricted"]
    budget: dict[str, Any]
    deadline: datetime
    preferred_model_id: str | None = None
    correlation_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$")

    @field_validator("deadline")
    @classmethod
    def deadline_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline must include a timezone")
        return value


class ProviderRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str
    driver_type: Literal[
        "fake", "openai_compatible", "ollama", "anthropic", "gemini", "generic_rest"
    ]
    endpoint: str | None
    model_name: str
    location: Literal["local", "remote"]
    data_policy: Literal["local_only", "remote_allowed", "remote_after_redaction"]
    authentication_scheme: Literal["none", "bearer", "api_key", "oauth"]
    output_limit: int = Field(ge=64, le=1_000_000)

    @model_validator(mode="after")
    def validate_endpoint_and_auth(self) -> ProviderRoute:
        if self.driver_type == "fake":
            if self.endpoint is not None or self.authentication_scheme != "none":
                raise ValueError("fake routes cannot have an endpoint or credentials")
            return self
        if not self.endpoint:
            raise ValueError("non-fake routes require an endpoint")
        parts = urlsplit(self.endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "provider endpoint must be an absolute HTTP(S) URL without credentials or query"
            )
        if self.location == "remote" and parts.scheme != "https":
            raise ValueError("remote provider endpoints require HTTPS")
        return self


@dataclass(frozen=True)
class DriverResult:
    output: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    raw_response_bytes: int = 0


class AIDriver(Protocol):
    async def generate(
        self,
        *,
        route: ProviderRoute,
        system_instructions: str,
        rendered_prompt: str,
        response_schema: dict[str, Any],
        deadline: datetime,
        secret: str | None,
    ) -> DriverResult: ...


@dataclass(frozen=True)
class RedactionRecord:
    path: str
    category: str
    value_hash: str


@dataclass(frozen=True)
class GatewayResponse:
    output: dict[str, Any]
    request_hash: str
    response_hash: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    redactions: tuple[RedactionRecord, ...]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _redact_string(value: str, path: str) -> tuple[str, list[RedactionRecord]]:
    records: list[RedactionRecord] = []

    def replace_email(match: re.Match[str]) -> str:
        records.append(RedactionRecord(path, "email", _hash(match.group(0))))
        return "[REDACTED_EMAIL]"

    def replace_phone(match: re.Match[str]) -> str:
        records.append(RedactionRecord(path, "phone", _hash(match.group(0))))
        return "[REDACTED_PHONE]"

    return _PHONE.sub(replace_phone, _EMAIL.sub(replace_email, value)), records


def deterministic_redact(value: Any, path: str = "$") -> tuple[Any, list[RedactionRecord]]:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        records: list[RedactionRecord] = []
        for key in sorted(value):
            child_path = f"{path}.{key}"
            if any(part in str(key).casefold() for part in _SENSITIVE_KEYS):
                records.append(RedactionRecord(child_path, "sensitive_key", _hash(str(value[key]))))
                output[str(key)] = "[REDACTED]"
            else:
                output[str(key)], child_records = deterministic_redact(value[key], child_path)
                records.extend(child_records)
        return output, records
    if isinstance(value, list):
        output = []
        records: list[RedactionRecord] = []
        for index, item in enumerate(value):
            redacted, child_records = deterministic_redact(item, f"{path}[{index}]")
            output.append(redacted)
            records.extend(child_records)
        return output, records
    if isinstance(value, str):
        return _redact_string(value, path)
    return value, []


def _depth(value: Any, level: int = 0) -> int:
    if level > 40:
        return level
    if isinstance(value, dict):
        return max((_depth(item, level + 1) for item in value.values()), default=level)
    if isinstance(value, list):
        return max((_depth(item, level + 1) for item in value), default=level)
    return level


def _canonical_json(value: Any, *, maximum_bytes: int = 2_000_000) -> str:
    if _depth(value) > 40:
        raise ValueError("structured model input exceeds the nesting limit")
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(serialized.encode()) > maximum_bytes:
        raise ValueError("structured model input exceeds the byte limit")
    return serialized


def _validate_response_schema(schema: dict[str, Any]) -> None:
    """Validate locally without allowing schemas to trigger network retrieval."""

    _canonical_json(schema, maximum_bytes=250_000)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("response schema may use only local references")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError("response schema is not valid Draft 2020-12 JSON Schema") from exc


def prepare_model_input(
    request: GatewayRequest, route: ProviderRoute
) -> tuple[dict[str, Any], tuple[RedactionRecord, ...]]:
    payload = {
        "task_type": request.task_type,
        "structured_inputs": request.structured_inputs,
        "references": request.references,
    }
    if route.location == "local":
        return payload, ()
    if route.data_policy == "local_only":
        raise ValueError("data policy forbids sending this task to a remote provider")
    if request.sensitivity == "restricted":
        raise ValueError("restricted inputs cannot be sent to a remote provider")
    if request.sensitivity == "sensitive" and route.data_policy != "remote_after_redaction":
        raise ValueError("sensitive inputs require deterministic redaction for remote providers")
    if route.data_policy == "remote_after_redaction":
        redacted, records = deterministic_redact(payload)
        return redacted, tuple(records)
    return payload, ()


def render_prompt(
    template: str,
    *,
    payload: dict[str, Any],
    response_schema: dict[str, Any],
) -> str:
    if "{{structured_input_json}}" not in template or "{{response_schema_json}}" not in template:
        raise ValueError("active prompt template is missing required structured placeholders")
    return template.replace("{{structured_input_json}}", _canonical_json(payload)).replace(
        "{{response_schema_json}}", _canonical_json(response_schema)
    )


class AIGateway:
    def __init__(
        self,
        drivers: dict[str, AIDriver],
    ) -> None:
        self.drivers = dict(drivers)

    async def execute(
        self,
        request: GatewayRequest,
        route: ProviderRoute,
        *,
        system_instructions: str,
        prompt_template: str,
        secret: str | None = None,
    ) -> GatewayResponse:
        remaining = (
            request.deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds()
        if remaining <= 0:
            raise TimeoutError("model request deadline has already elapsed")
        if request.preferred_model_id and request.preferred_model_id != route.model_id:
            raise ValueError("preferred model is not the selected visible route")
        driver = self.drivers.get(route.driver_type)
        if driver is None:
            raise ValueError("selected provider driver is unavailable")
        _validate_response_schema(request.response_schema)
        payload, redactions = prepare_model_input(request, route)
        rendered = render_prompt(
            prompt_template,
            payload=payload,
            response_schema=request.response_schema,
        )
        request_document = {
            "route": route.model_dump(exclude={"endpoint", "authentication_scheme"}),
            "system_instructions": system_instructions,
            "rendered_prompt": rendered,
            "response_schema": request.response_schema,
        }
        request_hash = _hash(_canonical_json(request_document))
        started = monotonic()
        try:
            result = await asyncio.wait_for(
                driver.generate(
                    route=route,
                    system_instructions=system_instructions,
                    rendered_prompt=rendered,
                    response_schema=request.response_schema,
                    deadline=request.deadline,
                    secret=secret,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError("model request deadline elapsed") from exc
        validator = Draft202012Validator(request.response_schema)
        errors = sorted(validator.iter_errors(result.output), key=lambda error: list(error.path))
        if errors:
            raise ValueError(f"model output failed response schema: {errors[0].message}")
        response_json = _canonical_json(result.output)
        return GatewayResponse(
            output=result.output,
            request_hash=request_hash,
            response_hash=_hash(response_json),
            input_tokens=max(0, result.input_tokens),
            output_tokens=max(0, result.output_tokens),
            latency_ms=max(0, round((monotonic() - started) * 1000)),
            redactions=redactions,
        )


class FunctionDriver:
    """Deterministic fake-provider boundary for CI and offline acceptance tests."""

    def __init__(
        self,
        function: Callable[[ProviderRoute, str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        self.function = function

    async def generate(
        self,
        *,
        route: ProviderRoute,
        system_instructions: str,
        rendered_prompt: str,
        response_schema: dict[str, Any],
        deadline: datetime,
        secret: str | None,
    ) -> DriverResult:
        if secret is not None:
            raise ValueError("fake provider cannot receive a secret")
        output = await self.function(route, rendered_prompt, response_schema)
        return DriverResult(
            output=output,
            input_tokens=max(1, len(rendered_prompt) // 4),
            output_tokens=max(1, len(_canonical_json(output)) // 4),
            raw_response_bytes=len(_canonical_json(output).encode()),
        )


class JSONHTTPDriver:
    """Base for fixed-endpoint drivers; response bodies are bounded after decompression."""

    async def post(
        self,
        endpoint: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        deadline: datetime,
        maximum_bytes: int = 2_000_000,
    ) -> dict[str, Any]:
        remaining = (deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("model request deadline elapsed")
        timeout = aiohttp.ClientTimeout(total=min(remaining, 120), connect=10, sock_read=90)
        async with aiohttp.ClientSession(
            timeout=timeout,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=True,
            headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
        ) as session:
            async with session.post(endpoint, json=body, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError(f"model provider returned HTTP {response.status}")
                raw = await response.content.read(maximum_bytes + 1)
                if len(raw) > maximum_bytes:
                    raise ValueError("model provider response exceeds the byte limit")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("model provider returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("model provider response root must be an object")
        return value
