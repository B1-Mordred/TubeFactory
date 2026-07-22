import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from editorial_ai_gateway.drivers import _content_json, provider_compatible_schema
from editorial_ai_gateway.gateway import (
    AIGateway,
    FunctionDriver,
    GatewayRequest,
    ProviderRoute,
    _decode_json_http_response,
    _deadline_http_timeout,
    deterministic_redact,
    read_bounded_http_body,
)


def request(sensitivity="internal") -> GatewayRequest:
    return GatewayRequest(
        task_type="script_writer",
        structured_inputs={"claim": "Contact editor@example.org about +49 89 12345678"},
        references=[],
        response_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
        sensitivity=sensitivity,
        budget={"max_output_tokens": 100},
        deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
        correlation_id="gateway-test",
    )


def route(*, location="local", policy="local_only") -> ProviderRoute:
    return ProviderRoute(
        provider_id="provider",
        model_id="model",
        driver_type="fake",
        endpoint=None,
        model_name="fixture",
        location=location,
        data_policy=policy,
        authentication_scheme="none",
        output_limit=100,
    )


def test_provider_routes_enforce_fixed_safe_endpoints() -> None:
    data = route().model_dump()
    data.update(driver_type="generic_rest", endpoint="https://user:secret@example.org/generate")
    with pytest.raises(ValueError, match="without credentials"):
        ProviderRoute.model_validate(data)

    data.update(endpoint="http://example.org/generate", location="remote")
    with pytest.raises(ValueError, match="require HTTPS"):
        ProviderRoute.model_validate(data)


def test_http_timeout_honors_the_bounded_model_deadline() -> None:
    timeout = _deadline_http_timeout(600)

    assert timeout.total == 600
    assert timeout.sock_read == 600
    assert timeout.connect == 10


def test_provider_schema_preserves_structure_and_drops_unsupported_grammar_constraints() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "format": "uuid",
                    "minLength": 36,
                },
            }
        },
        "additionalProperties": False,
    }

    compatible = provider_compatible_schema(schema)

    assert compatible == {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "additionalProperties": False,
    }


def test_provider_content_accepts_a_single_markdown_json_fence() -> None:
    assert _content_json('```json\n{"ok": true}\n```') == {"ok": True}


def test_provider_content_extracts_json_after_local_reasoning_text() -> None:
    assert _content_json('Reasoning omitted from result.\n{"ok": true}\nDone.') == {
        "ok": True
    }


def test_json_http_decoder_reassembles_openai_sse_content() -> None:
    raw = (
        b'data: {"choices":[{"delta":{"content":"{\\"answer\\":"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"\\"ok\\"}"}}]}\n\n'
        b'data: [DONE]\n'
    )

    value = _decode_json_http_response(raw)

    assert value["choices"][0]["message"]["content"] == '{"answer":"ok"}'


def test_json_http_decoder_accepts_unescaped_controls_in_local_message_content() -> None:
    raw = b'{"choices":[{"message":{"content":"line one\nline two"}}]}'

    value = _decode_json_http_response(raw)

    assert value["choices"][0]["message"]["content"] == "line one\nline two"


async def test_bounded_http_reader_consumes_every_available_chunk() -> None:
    class Content:
        async def iter_chunked(self, size):
            assert size == 33
            for chunk in (b'{"part":', b'"complete"}'):
                yield chunk

    assert await read_bounded_http_body(Content(), 32) == b'{"part":"complete"}'


async def test_bounded_http_reader_rejects_the_first_byte_over_limit() -> None:
    class Content:
        async def iter_chunked(self, size):
            yield b"1234"
            yield b"5"

    with pytest.raises(ValueError, match="byte limit"):
        await read_bounded_http_body(Content(), 4)

async def test_gateway_validates_schema_and_hashes_structured_output() -> None:
    async def fake(*args):
        return {"ok": True}

    response = await AIGateway({"fake": FunctionDriver(fake)}).execute(
        request(),
        route(),
        system_instructions="Use only approved data.",
        prompt_template="Input {{structured_input_json}} Schema {{response_schema_json}}",
    )
    assert response.output == {"ok": True}
    assert len(response.request_hash) == len(response.response_hash) == 64
    assert response.redactions == ()


async def test_remote_sensitive_request_is_redacted_before_driver_serialization() -> None:
    observed = ""

    async def fake(route, prompt, schema):
        nonlocal observed
        observed = prompt
        return {"ok": True}

    response = await AIGateway({"fake": FunctionDriver(fake)}).execute(
        request("sensitive"),
        route(location="remote", policy="remote_after_redaction"),
        system_instructions="Use only approved data.",
        prompt_template="Input {{structured_input_json}} Schema {{response_schema_json}}",
    )
    assert "editor@example.org" not in observed
    assert "+49 89 12345678" not in observed
    assert {item.category for item in response.redactions} == {"email", "phone"}


async def test_data_policy_blocks_remote_restricted_and_local_only_inputs() -> None:
    async def fake(*args):
        return {"ok": True}

    gateway = AIGateway({"fake": FunctionDriver(fake)})
    with pytest.raises(ValueError, match="forbids"):
        await gateway.execute(
            request(),
            route(location="remote", policy="local_only"),
            system_instructions="Use only approved data.",
            prompt_template="Input {{structured_input_json}} Schema {{response_schema_json}}",
        )
    with pytest.raises(ValueError, match="restricted"):
        await gateway.execute(
            request("restricted"),
            route(location="remote", policy="remote_after_redaction"),
            system_instructions="Use only approved data.",
            prompt_template="Input {{structured_input_json}} Schema {{response_schema_json}}",
        )


async def test_schema_invalid_provider_output_is_rejected() -> None:
    async def fake(*args):
        return {"unexpected": True}

    with pytest.raises(ValueError, match="response schema"):
        await AIGateway({"fake": FunctionDriver(fake)}).execute(
            request(),
            route(),
            system_instructions="Use only approved data.",
            prompt_template="Input {{structured_input_json}} Schema {{response_schema_json}}",
        )


async def test_remote_response_schema_references_are_rejected_before_driver() -> None:
    called = False

    async def fake(*args):
        nonlocal called
        called = True
        return {"ok": True}

    unsafe = request().model_copy(
        update={"response_schema": {"$ref": "https://example.org/schema.json"}}
    )
    with pytest.raises(ValueError, match="only local references"):
        await AIGateway({"fake": FunctionDriver(fake)}).execute(
            unsafe,
            route(),
            system_instructions="Use only approved data.",
            prompt_template="Input {{structured_input_json}} Schema {{response_schema_json}}",
        )
    assert called is False


async def test_gateway_applies_deadline_to_function_drivers() -> None:
    async def slow(*args):
        await asyncio.sleep(0.1)
        return {"ok": True}

    expiring = request().model_copy(
        update={"deadline": datetime.now(timezone.utc) + timedelta(milliseconds=10)}
    )
    with pytest.raises(TimeoutError, match="deadline elapsed"):
        await AIGateway({"fake": FunctionDriver(slow)}).execute(
            expiring,
            route(),
            system_instructions="Use only approved data.",
            prompt_template="Input {{structured_input_json}} Schema {{response_schema_json}}",
        )
