from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import aiohttp
import asyncpg
from temporalio import activity
from temporalio.exceptions import ApplicationError

from editorial_ai_gateway.gateway import read_bounded_http_body
from editorial_worker.config import Settings


def _discovery_url(driver_type: str, endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if driver_type == "openai_compatible":
        return f"{base}/models"
    if driver_type == "ollama":
        return f"{base}/api/tags"
    raise ValueError("provider driver does not expose supported model discovery")


def _model_names(driver_type: str, payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        raise ValueError("provider model inventory must be a JSON object")
    items = payload.get("data") if driver_type == "openai_compatible" else payload.get("models")
    if not isinstance(items, list):
        raise ValueError("provider model inventory is missing its model list")
    key = "id" if driver_type == "openai_compatible" else "name"
    names = {
        str(item.get(key, "")).strip()
        for item in items
        if isinstance(item, dict) and str(item.get(key, "")).strip()
    }
    return sorted(names, key=str.casefold)


@activity.defn(name="discover-provider-models")
async def discover_provider_models(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        provider = await connection.fetchrow(
            """SELECT id,name,driver_type,endpoint,authentication_scheme,secret_reference,
                      enabled,deleted_at
               FROM providers WHERE id=$1""",
            UUID(str(request["provider_id"])),
        )
    finally:
        await connection.close()
    if provider is None or provider["deleted_at"] is not None or not provider["enabled"]:
        raise ApplicationError("provider is not active", non_retryable=True)
    if not provider["endpoint"]:
        raise ApplicationError("provider has no discovery endpoint", non_retryable=True)
    try:
        url = _discovery_url(provider["driver_type"], provider["endpoint"])
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    secret = settings.provider_secret(provider["secret_reference"])
    scheme = provider["authentication_scheme"]
    headers: dict[str, str] = {"Accept": "application/json"}
    if scheme in {"bearer", "oauth"} and secret:
        headers["Authorization"] = f"Bearer {secret}"
    elif scheme == "api_key" and secret:
        headers["X-API-Key"] = secret
    timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=True,
            headers=headers,
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError(f"provider inventory returned HTTP {response.status}")
                raw = await read_bounded_http_body(response.content, 1_000_000)
        payload = json.loads(raw)
        names = _model_names(provider["driver_type"], payload)
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        raise ApplicationError(
            f"provider model discovery failed: {str(exc)[:500]}", non_retryable=True
        ) from exc
    return {
        "provider_id": str(provider["id"]),
        "provider_name": provider["name"],
        "driver_type": provider["driver_type"],
        "models": names,
        "model_count": len(names),
    }
