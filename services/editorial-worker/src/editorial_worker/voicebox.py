from __future__ import annotations

import base64
import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect


class VoiceboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceboxResult:
    audio: bytes
    duration_seconds: float
    sample_rate: int
    engine: str
    model_version: str
    word_alignment: list[dict[str, Any]]
    warnings: list[str]


class VoiceboxRESTClient:
    """Provider-neutral REST boundary; endpoint is profile-controlled, never request-controlled."""

    def __init__(self, endpoint: str) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Voicebox endpoint must be a fixed HTTP(S) origin")
        self.endpoint = endpoint.rstrip("/")

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.endpoint, timeout=10, follow_redirects=False) as client:
            response = await client.get("/health")
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise VoiceboxError("Voicebox health response is invalid")
            return value

    async def synthesize(self, request: dict[str, Any]) -> VoiceboxResult:
        async with httpx.AsyncClient(base_url=self.endpoint, timeout=180, follow_redirects=False) as client:
            response = await client.post("/synthesize", json=request)
            response.raise_for_status()
            value = response.json()
        try:
            audio = base64.b64decode(value["audio_base64"], validate=True)
            return VoiceboxResult(
                audio=audio,
                duration_seconds=float(value["duration_seconds"]),
                sample_rate=int(value["sample_rate"]),
                engine=str(value["engine"]),
                model_version=str(value["model_version"]),
                word_alignment=list(value.get("word_alignment", [])),
                warnings=[str(item) for item in value.get("warnings", [])],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceboxError("Voicebox response violates the provider-neutral contract") from exc


def _result(value: dict[str, Any], audio: bytes | None = None) -> VoiceboxResult:
    try:
        body = audio if audio is not None else base64.b64decode(value["audio_base64"], validate=True)
        if not body:
            raise ValueError("empty audio")
        return VoiceboxResult(
            audio=body,
            duration_seconds=float(value["duration_seconds"]),
            sample_rate=int(value["sample_rate"]),
            engine=str(value["engine"]),
            model_version=str(value["model_version"]),
            word_alignment=list(value.get("word_alignment", [])),
            warnings=[str(item) for item in value.get("warnings", [])],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise VoiceboxError("Voicebox response violates the provider-neutral contract") from exc


class VoiceboxWSClient:
    """Fixed-origin streaming Voicebox boundary using the provider-neutral envelope."""

    def __init__(self, endpoint: str) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username:
            raise ValueError("Voicebox endpoint must be a fixed WebSocket origin")
        self.endpoint = endpoint

    async def health(self) -> dict[str, Any]:
        async with asyncio.timeout(10):
            async with connect(self.endpoint, max_size=1_048_576, ping_interval=20, ping_timeout=20) as socket:
                await socket.send(json.dumps({"type": "health"}))
                value = json.loads(await socket.recv())
        if not isinstance(value, dict) or value.get("type") not in {"health", "ready"}:
            raise VoiceboxError("Voicebox WebSocket health response is invalid")
        return value

    async def synthesize(self, request: dict[str, Any]) -> VoiceboxResult:
        chunks: list[bytes] = []
        metadata: dict[str, Any] | None = None
        async with asyncio.timeout(180):
            async with connect(self.endpoint, max_size=16_777_216, ping_interval=20, ping_timeout=20) as socket:
                await socket.send(json.dumps({"type": "synthesize", "request": request}, separators=(",", ":")))
                async for raw in socket:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise VoiceboxError("Voicebox WebSocket message is invalid")
                    if value.get("type") == "audio":
                        chunks.append(base64.b64decode(value["audio_base64"], validate=True))
                    elif value.get("type") in {"result", "complete"}:
                        metadata = value.get("response", value)
                        if "audio_base64" in metadata and not chunks:
                            chunks.append(base64.b64decode(metadata["audio_base64"], validate=True))
                        break
                    elif value.get("type") == "error":
                        raise VoiceboxError(str(value.get("message", "Voicebox WebSocket synthesis failed")))
        if metadata is None:
            raise VoiceboxError("Voicebox WebSocket closed without a result")
        return _result(metadata, b"".join(chunks))
