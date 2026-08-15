import base64
import hashlib
import io
import json
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import certifi
import httpx
import pytest
import respx

from editorial_worker.comfyui import ComfyUICapabilityError, ComfyUIClient
from editorial_worker.media_activities import (
    _await_with_activity_heartbeat,
    _crossfade_wavs,
    _fixture_wav,
    _synchronize_media_timing,
)
from editorial_worker.voicebox import VoiceboxError, VoiceboxRESTClient, VoiceboxWSClient


@pytest.mark.asyncio
@respx.mock
async def test_comfy_inventory_requires_declared_nodes_and_models():
    respx.get("http://comfy:8188/object_info").mock(return_value=httpx.Response(200, json={"KSampler": {"input": {"required": {"ckpt_name": [["safe.safetensors"]]}}}}))
    client = ComfyUIClient("http://comfy:8188")
    result = await client.validate_inventory(required_nodes=[{"class_type": "KSampler", "version": "pinned"}], required_models=[{"name": "safe.safetensors", "sha256": "a" * 64}])
    assert "KSampler" in result["node_types"]
    with pytest.raises(ComfyUICapabilityError, match="nodes"):
        await client.validate_inventory(required_nodes=[{"class_type": "UnsafeNode", "version": "1"}], required_models=[])


@pytest.mark.asyncio
@respx.mock
async def test_voicebox_contract_decodes_audio():
    respx.post("http://voicebox:8000/synthesize").mock(return_value=httpx.Response(200, json={"audio_base64": base64.b64encode(b"RIFF").decode(), "duration_seconds": 1, "sample_rate": 48000, "engine": "fixture", "model_version": "1", "word_alignment": [], "warnings": []}))
    result = await VoiceboxRESTClient("http://voicebox:8000").synthesize({"text": "hello"})
    assert result.audio == b"RIFF"


@pytest.mark.asyncio
@respx.mock
async def test_voicebox_b1_generate_stream_contract_returns_wav_with_authorization():
    audio, _ = _fixture_wav("hallo welt", 1, 24_000)
    route = respx.post("http://voicebox:8000/generate/stream").mock(
        return_value=httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})
    )
    result = await VoiceboxRESTClient("http://voicebox:8000", api_key="test-token").synthesize(
        {
            "provider_contract": "b1_generate_stream",
            "voice_id": "voice-uuid",
            "text": "hallo welt",
            "language": "de",
            "engine": "remote_http",
            "model_version": "b1-local-voicebox",
            "accept": "audio/wav",
        }
    )

    assert result.audio == audio
    assert result.sample_rate == 24_000
    assert result.duration_seconds == 1
    assert result.engine == "remote_http"
    assert result.model_version == "b1-local-voicebox"
    assert result.word_alignment[0]["timing_source"] == "estimated_from_b1_stream_duration"
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-token"
    assert json.loads(request.content) == {
        "profile_id": "voice-uuid",
        "text": "hallo welt",
        "language": "de",
        "engine": "remote_http",
        "normalize": False,
        "effects_chain": [],
    }


@pytest.mark.asyncio
@respx.mock
async def test_voicebox_b1_generate_stream_retries_transient_admission_conflict():
    audio, _ = _fixture_wav("hallo welt", 1, 24_000)
    route = respx.post("http://voicebox:8000/generate/stream").mock(
        side_effect=[
            httpx.Response(
                409,
                json={"detail": {"message": "GPU scheduler lease is held by another owner"}},
                headers={"retry-after": "0"},
            ),
            httpx.Response(200, content=audio, headers={"content-type": "audio/wav"}),
        ]
    )

    result = await VoiceboxRESTClient("http://voicebox:8000").synthesize(
        {
            "provider_contract": "b1_generate_stream",
            "voice_id": "voice-uuid",
            "text": "hallo welt",
            "language": "de",
            "engine": "chatterbox",
            "accept": "audio/wav",
            "stream_generation_retry_attempts": 2,
            "stream_generation_retry_backoff_seconds": 0,
        }
    )

    assert result.audio == audio
    assert result.warnings == ["B1 stream generation admitted after 2 attempts over 0s"]
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_voicebox_b1_generate_stream_reports_cooling_retry_heartbeats():
    audio, _ = _fixture_wav("hallo welt", 1, 24_000)
    events: list[dict[str, object]] = []
    route = respx.post("http://voicebox:8000/generate/stream").mock(
        side_effect=[
            httpx.Response(503, json={"detail": "lan-p40-media runtime recover hook returned HTTP 503"}, headers={"retry-after": "0"}),
            httpx.Response(200, content=audio, headers={"content-type": "audio/wav"}),
        ]
    )

    result = await VoiceboxRESTClient(
        "http://voicebox:8000",
        retry_observer=events.append,
        retry_heartbeat_interval_seconds=0.01,
    ).synthesize(
        {
            "provider_contract": "b1_generate_stream",
            "voice_id": "voice-uuid",
            "text": "hallo welt",
            "language": "de",
            "engine": "chatterbox",
            "accept": "audio/wav",
            "stream_generation_retry_attempts": 2,
            "stream_generation_retry_backoff_seconds": 0,
        }
    )

    assert result.audio == audio
    assert route.call_count == 2
    assert any(event.get("state") == "b1_voicebox_cooling_retry" and event.get("status_code") == 503 for event in events)


@pytest.mark.asyncio
@respx.mock
async def test_voicebox_b1_generate_stream_exhausted_cooling_is_retryable_error():
    route = respx.post("http://voicebox:8000/generate/stream").mock(
        return_value=httpx.Response(
            503,
            json={"detail": "lan-p40-media runtime recover hook returned HTTP 503"},
            headers={"retry-after": "0"},
        )
    )

    with pytest.raises(VoiceboxError, match="still cooling down or unavailable"):
        await VoiceboxRESTClient("http://voicebox:8000").synthesize(
            {
                "provider_contract": "b1_generate_stream",
                "voice_id": "voice-uuid",
                "text": "hallo welt",
                "language": "de",
                "engine": "chatterbox",
                "accept": "audio/wav",
                "stream_generation_retry_attempts": 2,
                "stream_generation_retry_backoff_seconds": 0,
            }
        )

    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_voicebox_b1_generate_stream_can_bootstrap_profile_ca(tmp_path):
    audio, _ = _fixture_wav("hallo welt", 1, 24_000)
    certificate = Path(certifi.where()).read_bytes()
    ca_path = tmp_path / "b1-root.crt"
    respx.get("http://ca.local/root.crt").mock(return_value=httpx.Response(200, content=certificate))
    respx.post("http://voicebox:8000/generate/stream").mock(
        return_value=httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})
    )

    result = await VoiceboxRESTClient(
        "http://voicebox:8000",
        ca_cert_bootstrap_url="http://ca.local/root.crt",
        ca_cert_sha256=hashlib.sha256(certificate).hexdigest(),
        ca_cert_path=str(ca_path),
    ).synthesize(
        {
            "provider_contract": "b1_generate_stream",
            "voice_id": "voice-uuid",
            "text": "hallo welt",
            "language": "de",
            "engine": "chatterbox",
            "accept": "audio/wav",
        }
    )

    assert result.audio == audio
    assert ca_path.read_bytes() == certificate


@pytest.mark.asyncio
async def test_voicebox_websocket_collects_streamed_audio_and_metadata():
    socket = AsyncMock()
    socket.__aiter__.return_value = iter([
        '{"type":"audio","audio_base64":"UklGRg=="}',
        '{"type":"complete","response":{"duration_seconds":1,"sample_rate":48000,"engine":"fixture","model_version":"1","word_alignment":[],"warnings":[]}}',
    ])
    context = AsyncMock()
    context.__aenter__.return_value = socket
    with patch("editorial_worker.voicebox.connect", return_value=context):
        result = await VoiceboxWSClient("ws://voicebox:8000/synthesis").synthesize({"text": "hello"})
    assert result.audio == b"RIFF"
    socket.send.assert_awaited_once()


def test_voice_chunks_are_crossfaded_into_valid_mono_wav():
    first, _ = _fixture_wav("first phrase", 1, 48_000)
    second, _ = _fixture_wav("second phrase", 1, 48_000)
    combined = _crossfade_wavs([first, second], 0.02)
    with wave.open(io.BytesIO(combined), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getframerate() == 48_000
        assert reader.getnframes() == 95_040


@pytest.mark.asyncio
async def test_long_media_wait_emits_repeated_temporal_heartbeats():
    with patch("editorial_worker.media_activities.activity.heartbeat") as heartbeat:
        result = await _await_with_activity_heartbeat(
            __import__("asyncio").sleep(0.035, result="finished"),
            "waiting_for_test_render",
            interval_seconds=0.01,
        )

    assert result == "finished"
    assert heartbeat.call_count >= 3
    heartbeat.assert_any_call({"state": "waiting_for_test_render"})


def test_mastered_narration_defines_scene_and_chapter_timing_without_changing_scene_spec():
    context = {
        "segments": [
            {"id": "segment-1", "duration_seconds": 8.0},
            {"id": "segment-2", "duration_seconds": 7.0},
        ],
        "scenes": [
            {"id": "scene-1", "duration_seconds": 8.0, "scene_spec": {"purpose": "First", "duration": 8.0, "narration_segment_ids": ["segment-1"]}},
            {"id": "scene-2", "duration_seconds": 7.0, "scene_spec": {"purpose": "Second", "duration": 7.0, "narration_segment_ids": ["segment-2"]}},
        ],
    }
    narration = {
        "narration": [
            {"script_segment_id": "segment-1", "duration_seconds": 10.5},
            {"script_segment_id": "segment-2", "duration_seconds": 6.25},
        ],
        "duration_seconds": 16.75,
        "chapters": [],
    }

    timed_context, timed_narration = _synchronize_media_timing(context, narration)

    assert [item["duration_seconds"] for item in timed_context["scenes"]] == [10.5, 6.25]
    assert timed_context["scenes"][0]["scene_spec"]["duration"] == 8.0
    assert timed_context["scenes"][0]["storyboard_duration_seconds"] == 8.0
    assert timed_narration["chapters"][1]["timecode_seconds"] == 10.5
    assert timed_context["production_duration_seconds"] == 16.75


def test_mastered_narration_must_be_assigned_exactly_once():
    context = {
        "segments": [{"id": "segment-1", "duration_seconds": 8.0}],
        "scenes": [
            {"id": "scene-1", "duration_seconds": 8.0, "scene_spec": {"purpose": "First", "narration_segment_ids": ["segment-1"]}},
            {"id": "scene-2", "duration_seconds": 8.0, "scene_spec": {"purpose": "Duplicate", "narration_segment_ids": ["segment-1"]}},
        ],
    }
    narration = {"narration": [{"script_segment_id": "segment-1", "duration_seconds": 8.5}]}

    with pytest.raises(ValueError, match="exactly one"):
        _synchronize_media_timing(context, narration)
