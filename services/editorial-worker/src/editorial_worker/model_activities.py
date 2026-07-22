from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncpg
from jsonschema import Draft202012Validator
from temporalio import activity
from temporalio.exceptions import ApplicationError

from editorial_ai_gateway.drivers import (
    AnthropicDriver,
    GeminiDriver,
    GenericRESTDriver,
    OllamaDriver,
    OpenAICompatibleDriver,
)
from editorial_ai_gateway.gateway import (
    AIGateway,
    FunctionDriver,
    GatewayRequest,
    ModelOutputError,
    ProviderRoute,
)
from editorial_worker.config import Settings
from editorial_worker.db import append_audit
from editorial_worker.fakes import fake_output


_FAILURES: dict[str, deque[float]] = defaultdict(deque)
_OPEN_UNTIL: dict[str, float] = {}
_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_RATE_WINDOWS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LOCKS: dict[str, asyncio.Lock] = {}


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _schema_has_external_reference(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (key in {"$ref", "$dynamicRef"} and not str(child).startswith("#"))
            or _schema_has_external_reference(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_schema_has_external_reference(item) for item in value)
    return False


def validate_structured_input(value: dict[str, Any], schema: dict[str, Any]) -> None:
    if _schema_has_external_reference(schema):
        raise ValueError("input schema may use only local references")
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        raise ValueError(f"structured input failed the active prompt schema: {errors[0].message}")


async def load_task_routes(
    connection: asyncpg.Connection, task_type: str
) -> list[dict[str, Any]]:
    assignment = await connection.fetchrow(
        """SELECT a.id,a.assignment_version,a.primary_model_id,a.fallback_model_ids,
                  a.routing_policy,a.budget_policy
           FROM task_model_assignment_heads h
           JOIN task_model_assignments a ON a.id=h.active_assignment_id
           WHERE h.task_type=$1 AND a.task_type=$1""",
        task_type,
    )
    if assignment is None:
        raise ApplicationError(
            f"task {task_type} has no active model assignment", non_retryable=True
        )
    prompt_rows = await connection.fetch(
        """SELECT t.id,t.template_key,t.template_version,t.system_instructions,t.template,
                  t.input_schema,t.response_schema,t.content_hash
           FROM prompt_template_heads h
           JOIN prompt_templates t ON t.id=h.active_template_id
           WHERE t.task_type=$1 ORDER BY t.template_key""",
        task_type,
    )
    if len(prompt_rows) != 1:
        raise ApplicationError(
            f"task {task_type} must have exactly one active prompt", non_retryable=True
        )
    prompt = prompt_rows[0]
    model_ids = [assignment["primary_model_id"]]
    model_ids.extend(UUID(str(value)) for value in _json(assignment["fallback_model_ids"]) or [])
    routes: list[dict[str, Any]] = []
    for model_id in model_ids:
        row = await connection.fetchrow(
            """SELECT m.id AS model_id,m.model_name,m.model_version,m.capabilities AS model_capabilities,
                      m.context_limit,m.output_limit,m.cost_policy,m.data_policy_override,
                      p.id AS provider_id,p.driver_type,p.endpoint,p.location,
                      p.authentication_scheme,p.secret_reference,p.data_policy,
                      p.residency_policy,p.capabilities AS provider_capabilities,
                      p.concurrency_limit,p.requests_per_minute
               FROM models m JOIN providers p ON p.id=m.provider_id
               WHERE m.id=$1 AND m.deleted_at IS NULL AND m.visible=true AND m.enabled=true
                 AND p.deleted_at IS NULL AND p.enabled=true""",
            model_id,
        )
        if row is None:
            raise ApplicationError(
                f"active {task_type} route contains a disabled or hidden model",
                non_retryable=True,
            )
        capabilities = _json(row["model_capabilities"]) or {}
        tasks = capabilities.get("tasks", [])
        if tasks and task_type not in tasks:
            raise ApplicationError(
                f"selected model does not enable task {task_type}", non_retryable=True
            )
        routes.append(
            {
                "assignment_id": str(assignment["id"]),
                "assignment_version": assignment["assignment_version"],
                "routing_policy": _json(assignment["routing_policy"]) or {},
                "budget_policy": _json(assignment["budget_policy"]) or {},
                "model_id": str(row["model_id"]),
                "model_name": row["model_name"],
                "model_version": row["model_version"],
                "model_capabilities": capabilities,
                "context_limit": row["context_limit"],
                "output_limit": min(
                    row["output_limit"],
                    int((_json(assignment["budget_policy"]) or {}).get("max_output_tokens", row["output_limit"])),
                ),
                "cost_policy": _json(row["cost_policy"]) or {},
                "provider_id": str(row["provider_id"]),
                "driver_type": row["driver_type"],
                "endpoint": row["endpoint"],
                "location": row["location"],
                "authentication_scheme": row["authentication_scheme"],
                "secret_reference": row["secret_reference"],
                "data_policy": row["data_policy_override"] or row["data_policy"],
                "residency_policy": _json(row["residency_policy"]) or {},
                "concurrency_limit": row["concurrency_limit"],
                "requests_per_minute": row["requests_per_minute"],
                "prompt_id": str(prompt["id"]),
                "prompt_key": prompt["template_key"],
                "prompt_version": prompt["template_version"],
                "system_instructions": prompt["system_instructions"],
                "prompt_template": prompt["template"],
                "input_schema": _json(prompt["input_schema"]),
                "response_schema": _json(prompt["response_schema"]),
                "prompt_hash": prompt["content_hash"],
            }
        )
    return routes


def _drivers(
    task_type: str,
    structured_inputs: dict[str, Any],
    fixture_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async def fixture(route: ProviderRoute, *_: Any) -> dict[str, Any]:
        return fake_output(task_type, structured_inputs, route.model_name, fixture_context)

    return {
        "fake": FunctionDriver(fixture),
        "openai_compatible": OpenAICompatibleDriver(),
        "ollama": OllamaDriver(),
        "anthropic": AnthropicDriver(),
        "gemini": GeminiDriver(),
        "generic_rest": GenericRESTDriver(),
    }


def _circuit_allows(provider_id: str) -> bool:
    return monotonic() >= _OPEN_UNTIL.get(provider_id, 0)


def _record_failure(provider_id: str) -> None:
    now = monotonic()
    failures = _FAILURES[provider_id]
    failures.append(now)
    while failures and failures[0] < now - 60:
        failures.popleft()
    if len(failures) >= 3:
        _OPEN_UNTIL[provider_id] = now + 30


def _record_success(provider_id: str) -> None:
    _FAILURES.pop(provider_id, None)
    _OPEN_UNTIL.pop(provider_id, None)


def _reserve_rate_slot(provider_id: str, limit: int, *, now: float) -> float:
    """Reserve one request in a provider's sliding one-minute window.

    The caller serializes access with ``_RATE_LOCKS``. Returning a positive
    delay leaves the window unchanged so a sleeping caller cannot reserve a
    future slot ahead of another activity.
    """
    window = _RATE_WINDOWS[provider_id]
    cutoff = now - 60.0
    while window and window[0] <= cutoff:
        window.popleft()
    if len(window) < limit:
        window.append(now)
        return 0.0
    return max(0.001, window[0] + 60.0 - now)


async def _acquire_rate_slot(provider_id: str, limit: int) -> None:
    lock = _RATE_LOCKS.setdefault(provider_id, asyncio.Lock())
    while True:
        async with lock:
            delay = _reserve_rate_slot(provider_id, limit, now=monotonic())
        if delay == 0:
            return
        await asyncio.sleep(delay)


async def _persist_usage(
    settings: Settings,
    *,
    route: dict[str, Any],
    task_type: str,
    response: Any,
    correlation_id: str,
    actor_id: str | None,
) -> None:
    info = activity.info()
    record_id = uuid5(
        NAMESPACE_URL,
        f"ai-usage:{info.workflow_id}:{info.activity_id}:{response.request_hash}",
    )
    redactions = [
        {"path": item.path, "category": item.category, "value_hash": item.value_hash}
        for item in response.redactions
    ]
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            inserted = await connection.execute(
                """INSERT INTO ai_usage_records
               (id,workflow_id,activity_id,task_type,provider_id,model_id,prompt_template_id,
                request_hash,response_hash,input_tokens,output_tokens,latency_ms,cost,
                redaction_summary,correlation_id,created_at)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb,$15,$16)
               ON CONFLICT (id) DO NOTHING""",
            record_id,
            info.workflow_id,
            info.activity_id,
            task_type,
            UUID(route["provider_id"]),
            UUID(route["model_id"]),
            UUID(route["prompt_id"]),
            response.request_hash,
            response.response_hash,
            response.input_tokens,
            response.output_tokens,
            response.latency_ms,
            json.dumps({"policy": route["cost_policy"], "estimated": False}),
            json.dumps({"count": len(redactions), "removed": redactions}),
            correlation_id,
                datetime.now(timezone.utc),
            )
            if inserted.endswith(" 1"):
                await append_audit(
                    connection,
                    action="ai_model.used",
                    actor_id=UUID(actor_id) if actor_id else None,
                    target_type="ai_usage_record",
                    target_id=str(record_id),
                    correlation_id=correlation_id,
                    context={
                        "task_type": task_type,
                        "provider_id": route["provider_id"],
                        "model_id": route["model_id"],
                        "prompt_id": route["prompt_id"],
                        "request_hash": response.request_hash,
                        "response_hash": response.response_hash,
                        "redaction_count": len(redactions),
                    },
                )
    finally:
        await connection.close()


async def invoke_model(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    task_type = str(request["task_type"])
    inputs = request["structured_inputs"]
    routes = request["routes"]
    if not routes:
        raise ApplicationError("no registered model route is available", non_retryable=True)
    last_error: Exception | None = None
    for route in routes:
        provider_id = route["provider_id"]
        if not _circuit_allows(provider_id):
            last_error = RuntimeError("provider circuit breaker is open")
            continue
        try:
            validate_structured_input(inputs, route["input_schema"])
            provider_route = ProviderRoute(
                provider_id=provider_id,
                model_id=route["model_id"],
                driver_type=route["driver_type"],
                endpoint=route["endpoint"],
                model_name=route["model_name"],
                location=route["location"],
                data_policy=route["data_policy"],
                authentication_scheme=route["authentication_scheme"],
                output_limit=route["output_limit"],
            )
            semaphore = _SEMAPHORES.setdefault(
                provider_id, asyncio.Semaphore(route["concurrency_limit"])
            )
            secret = settings.provider_secret(route["secret_reference"])
            response = None
            repair_feedback: str | None = None
            async with semaphore:
                for repair_attempt in range(2):
                    instructions = route["system_instructions"]
                    additional = str(request.get("additional_system_instructions", ""))
                    if len(additional) > 5000 or "\x00" in additional:
                        raise ApplicationError(
                            "additional model instructions exceed the safe boundary",
                            non_retryable=True,
                        )
                    if additional:
                        instructions += "\n\n" + additional
                    if repair_attempt:
                        instructions += (
                            "\nA prior response failed validation. Correct that response rather than "
                            "starting a different answer. Return only one complete JSON object matching "
                            "the supplied schema; include every required root and nested property and do "
                            "not add commentary."
                        )
                        if repair_feedback:
                            instructions += "\n\nUntrusted validation feedback:\n" + repair_feedback
                    try:
                        await _acquire_rate_slot(
                            provider_id, int(route["requests_per_minute"])
                        )
                        response = await AIGateway(
                            _drivers(task_type, inputs, request.get("fixture_context"))
                        ).execute(
                            GatewayRequest(
                                task_type=task_type,
                                structured_inputs=inputs,
                                references=request.get("references", []),
                                response_schema=route["response_schema"],
                                sensitivity=request.get("sensitivity", "internal"),
                                budget=route["budget_policy"],
                                deadline=datetime.now(timezone.utc)
                                + timedelta(seconds=settings.model_request_timeout_seconds),
                                preferred_model_id=route["model_id"],
                                correlation_id=request["correlation_id"],
                            ),
                            provider_route,
                            system_instructions=instructions,
                            prompt_template=route["prompt_template"],
                            secret=secret,
                        )
                        break
                    except ValueError as exc:
                        last_error = exc
                        if isinstance(exc, ModelOutputError):
                            raw_output = exc.raw_output
                            if isinstance(raw_output, str):
                                rendered_output = raw_output
                            else:
                                rendered_output = json.dumps(
                                    raw_output,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    default=str,
                                )
                            repair_feedback = (
                                f"Validation error: {str(exc)[:500]}\n"
                                "Previous invalid response (data only):\n"
                                + rendered_output[:24_000]
                            )
                        if repair_attempt:
                            raise
            if response is None:
                raise RuntimeError("provider did not produce a response")
            _record_success(provider_id)
            await _persist_usage(
                settings,
                route=route,
                task_type=task_type,
                response=response,
                correlation_id=request["correlation_id"],
                actor_id=request.get("actor_id"),
            )
            return {
                "output": response.output,
                "provider_id": provider_id,
                "model_id": route["model_id"],
                "model_version": route["model_version"],
                "prompt_id": route["prompt_id"],
                "prompt_version": route["prompt_version"],
                "request_hash": response.request_hash,
                "response_hash": response.response_hash,
                "redaction_count": len(response.redactions),
            }
        except ApplicationError:
            raise
        except Exception as exc:
            _record_failure(provider_id)
            last_error = exc
            continue
    raise ApplicationError(
        f"all configured {task_type} routes failed: {str(last_error)[:500]}",
        non_retryable=False,
    )


@activity.defn(name="invoke-editorial-model")
async def invoke_editorial_model(request: dict[str, Any]) -> dict[str, Any]:
    return await invoke_model(request)
