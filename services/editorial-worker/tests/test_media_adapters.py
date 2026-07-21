import base64
import io
import wave

import httpx
import pytest
import respx
from unittest.mock import AsyncMock, patch

from editorial_worker.comfyui import ComfyUICapabilityError, ComfyUIClient
from editorial_worker.media_activities import _crossfade_wavs, _fixture_wav
from editorial_worker.voicebox import VoiceboxRESTClient, VoiceboxWSClient


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
