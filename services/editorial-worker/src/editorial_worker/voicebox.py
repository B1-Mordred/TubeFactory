from __future__ import annotations

import base64
import asyncio
import hashlib
import io
import json
import ssl
import wave
from dataclasses import dataclass
from pathlib import Path
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

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        tls_verify: bool | str | ssl.SSLContext = True,
        ca_cert_bootstrap_url: str | None = None,
        ca_cert_sha256: str | None = None,
        ca_cert_path: str | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Voicebox endpoint must be a fixed HTTP(S) origin")
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.tls_verify = tls_verify
        self.ca_cert_bootstrap_url = ca_cert_bootstrap_url
        self.ca_cert_sha256 = ca_cert_sha256
        self.ca_cert_path = ca_cert_path or "/tmp/tubefactory-b1-ai-hub-caddy-root.crt"

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept, "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.endpoint,
            timeout=10,
            follow_redirects=False,
            verify=await self._verify(),
        ) as client:
            response = await client.get("/health", headers=self._headers())
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise VoiceboxError("Voicebox health response is invalid")
            return value

    async def synthesize(self, request: dict[str, Any]) -> VoiceboxResult:
        if request.get("provider_contract") == "b1_generate_stream":
            return await self._synthesize_b1_stream(request)
        async with httpx.AsyncClient(
            base_url=self.endpoint,
            timeout=180,
            follow_redirects=False,
            verify=await self._verify(),
        ) as client:
            response = await client.post("/synthesize", headers=self._headers(), json=request)
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

    async def _synthesize_b1_stream(self, request: dict[str, Any]) -> VoiceboxResult:
        payload = {
            "profile_id": request["voice_id"],
            "text": request["text"],
            "language": request.get("language") or "de",
            "engine": request.get("engine") or "chatterbox",
            "normalize": bool(request.get("normalize", False)),
            "effects_chain": list(request.get("effects_chain", [])),
        }
        async with httpx.AsyncClient(
            base_url=self.endpoint,
            timeout=180,
            follow_redirects=False,
            verify=await self._verify(),
        ) as client:
            response, attempts = await self._post_b1_stream_with_retry(
                client,
                "/generate/stream",
                headers=self._headers(accept=str(request.get("accept") or "audio/wav")),
                payload=payload,
                attempts=max(1, int(request.get("stream_generation_retry_attempts", 8))),
                backoff_seconds=max(0.0, float(request.get("stream_generation_retry_backoff_seconds", 5))),
                max_backoff_seconds=max(0.0, float(request.get("stream_generation_retry_max_backoff_seconds", 30))),
            )
            response.raise_for_status()
            audio = response.content
        if not audio or not audio[:12].startswith(b"RIFF") or audio[8:12] != b"WAVE":
            raise VoiceboxError("B1 Voicebox stream did not return a RIFF/WAVE response")
        duration_seconds, sample_rate = _wav_duration(audio)
        return VoiceboxResult(
            audio=audio,
            duration_seconds=duration_seconds,
            sample_rate=sample_rate,
            engine=str(payload["engine"]),
            model_version=str(request.get("model_version") or payload["engine"]),
            word_alignment=_estimated_word_alignment(str(payload["text"]), duration_seconds),
            warnings=[] if attempts == 1 else [f"B1 stream generation admitted after {attempts} attempts"],
        )

    async def _post_b1_stream_with_retry(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        attempts: int,
        backoff_seconds: float,
        max_backoff_seconds: float,
    ) -> tuple[httpx.Response, int]:
        retry_statuses = {409, 429, 502, 503, 504}
        response: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            response = await client.post(path, headers=headers, json=payload)
            if response.status_code not in retry_statuses or attempt == attempts:
                return response, attempt
            delay_seconds = self._retry_after_seconds(response)
            if delay_seconds is None:
                delay_seconds = min(backoff_seconds * (2 ** (attempt - 1)), max_backoff_seconds)
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
        assert response is not None
        return response, attempts

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        raw_value = response.headers.get("retry-after")
        if not raw_value:
            return None
        try:
            return max(0.0, float(raw_value))
        except ValueError:
            return None

    async def _verify(self) -> bool | ssl.SSLContext:
        if isinstance(self.tls_verify, bool) and not self.tls_verify:
            return False
        if isinstance(self.tls_verify, ssl.SSLContext):
            return self.tls_verify
        if isinstance(self.tls_verify, str) and self.tls_verify:
            return ssl.create_default_context(cafile=self.tls_verify)
        if not self.ca_cert_bootstrap_url:
            return True
        path = Path(self.ca_cert_path)
        if path.is_file() and self._matches_expected_sha256(path.read_bytes()):
            return ssl.create_default_context(cafile=str(path))
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, verify=False) as client:
            response = await client.get(
                self.ca_cert_bootstrap_url,
                headers={"Accept": "application/octet-stream,*/*;q=0.5"},
            )
            response.raise_for_status()
        certificate = response.content
        if not certificate or not self._matches_expected_sha256(certificate):
            raise VoiceboxError("B1 Voicebox CA bootstrap certificate hash does not match the configured profile")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(certificate)
        path.chmod(0o600)
        return ssl.create_default_context(cafile=str(path))

    def _matches_expected_sha256(self, body: bytes) -> bool:
        if not self.ca_cert_sha256:
            return True
        return hashlib.sha256(body).hexdigest() == self.ca_cert_sha256


def _wav_duration(audio: bytes) -> tuple[float, int]:
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            return reader.getnframes() / reader.getframerate(), reader.getframerate()
    except (wave.Error, EOFError, ValueError) as exc:
        raise VoiceboxError("Voicebox WAV response is invalid") from exc


def _estimated_word_alignment(text: str, duration_seconds: float) -> list[dict[str, Any]]:
    words = text.split()
    if not words:
        return []
    return [
        {
            "word": word,
            "start_seconds": round(duration_seconds * index / len(words), 3),
            "end_seconds": round(duration_seconds * (index + 1) / len(words), 3),
            "timing_source": "estimated_from_b1_stream_duration",
        }
        for index, word in enumerate(words)
    ]


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
