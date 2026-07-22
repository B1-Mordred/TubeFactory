from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import re
import struct
import subprocess
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import asyncpg
import httpx
from minio import Minio
from PIL import Image, ImageDraw
from temporalio import activity
from temporalio.exceptions import ApplicationError

from editorial_core.branding import normalize_brand_kit
from editorial_core.media import TypedWorkflowInput, canonical_hash, stable_voice_chunks
from editorial_worker.comfyui import ComfyUIClient
from editorial_worker.config import Settings
from editorial_worker.db import append_audit
from editorial_worker.voicebox import VoiceboxRESTClient, VoiceboxWSClient


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _asset_id(workflow_id: str, key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"evidence-studio:{workflow_id}:asset:{key}")


def _production_id(workflow_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"evidence-studio:{workflow_id}:production")


def _minio(settings: Settings) -> Minio:
    return Minio(settings.minio_endpoint, access_key=settings.minio_access_key, secret_key=settings.minio_secret_key, secure=False)


async def _put(client: Minio, bucket: str, key: str, body: bytes, content_type: str) -> None:
    await asyncio.to_thread(client.put_object, bucket, key, io.BytesIO(body), len(body), content_type=content_type)


async def _cached_media(settings: Settings, cache_key: str) -> tuple[bytes, str] | None:
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        row = await connection.fetchrow(
            "SELECT object_key,mime_type FROM media_assets WHERE cache_key=$1 ORDER BY created_at LIMIT 1",
            cache_key,
        )
    finally:
        await connection.close()
    if row is None:
        return None
    return await asyncio.to_thread(_get, _minio(settings), settings.minio_bucket, row["object_key"]), row["mime_type"]


async def _cached_narration(settings: Settings, cache_key: str) -> dict[str, Any] | None:
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        row = await connection.fetchrow(
            """SELECT ma.object_key,ns.word_alignment,ns.response
               FROM media_assets ma JOIN narration_segments ns ON ns.original_asset_id=ma.id
               WHERE ma.cache_key=$1 ORDER BY ns.created_at LIMIT 1""",
            cache_key,
        )
    finally:
        await connection.close()
    if row is None:
        return None
    response = _json(row["response"])
    alignment = _json(row["word_alignment"])
    base = min((float(item["start_seconds"]) for item in alignment), default=0.0)
    return {
        "audio": await asyncio.to_thread(_get, _minio(settings), settings.minio_bucket, row["object_key"]),
        "alignment": [{**item, "start_seconds": float(item["start_seconds"]) - base, "end_seconds": float(item["end_seconds"]) - base} for item in alignment],
        "engine": response["engine"],
        "model_version": response["model_version"],
        "warnings": [*response.get("warnings", []), "deterministic voice cache hit"],
    }


def _get(client: Minio, bucket: str, key: str) -> bytes:
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def _asset(
    *, workflow_id: str, key: str, production_id: UUID, kind: str, body: bytes,
    mime: str, object_key: str, scene_version_id: str | None = None,
    width: int | None = None, height: int | None = None,
    duration: float | None = None, licence: dict[str, Any], provenance: dict[str, Any],
    cache_key: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(_asset_id(workflow_id, key)), "production_id": str(production_id),
        "scene_version_id": scene_version_id, "asset_kind": kind, "object_key": object_key,
        "content_hash": hashlib.sha256(body).hexdigest(), "mime_type": mime,
        "byte_size": len(body), "width": width, "height": height,
        "duration_seconds": duration, "licence": licence,
        "generation_provenance": provenance, "cache_key": cache_key,
    }


@activity.defn(name="load-media-production-context")
async def load_media_production_context(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        storyboard = await connection.fetchrow(
            """SELECT bv.id,bv.storyboard_id,bv.script_version_id,bv.version_number,bv.status,
                      bv.content_hash,s.opportunity_id,cp.id AS channel_profile_id,cp.name AS channel_name,
                      cp.version AS channel_profile_version,cp.brand_kit,
                      cp.default_render_settings,sv.verification_report
               FROM storyboard_versions bv
               JOIN storyboards b ON b.id=bv.storyboard_id
               JOIN scripts s ON s.id=b.script_id
               JOIN script_versions sv ON sv.id=bv.script_version_id
               JOIN opportunities o ON o.id=s.opportunity_id
               JOIN subject_profiles sp ON sp.id=o.subject_profile_id
               JOIN channel_profiles cp ON cp.id=sp.channel_profile_id
               WHERE bv.id=$1 AND b.status='approved' AND b.current_version_id=bv.id""",
            UUID(str(request["storyboard_version_id"])),
        )
        if storyboard is None:
            raise ApplicationError("storyboard version is not the current approved version", non_retryable=True)
        if storyboard["content_hash"] != request["expected_storyboard_hash"]:
            raise ApplicationError("storyboard hash changed before media generation", non_retryable=True)
        exact_approval = await connection.fetchval(
            """SELECT id FROM approvals WHERE target_type='storyboard_version' AND target_id=$1
               AND target_version=$2 AND target_hash=$3 AND decision='approved'
               ORDER BY created_at DESC LIMIT 1""",
            storyboard["id"], storyboard["version_number"], storyboard["content_hash"],
        )
        if exact_approval is None:
            raise ApplicationError("storyboard exact-hash approval is missing", non_retryable=True)
        workflow = await connection.fetchrow("SELECT * FROM comfy_workflow_versions WHERE id=$1", UUID(str(request["comfy_workflow_version_id"])))
        workflow_active = await connection.fetchval("SELECT active_version_id FROM comfy_workflow_heads WHERE workflow_key=$1", workflow["workflow_key"] if workflow else "")
        if workflow is None or workflow["approval_state"] != "approved" or workflow_active != workflow["id"]:
            raise ApplicationError("ComfyUI workflow is no longer active and approved", non_retryable=True)
        voice = await connection.fetchrow("SELECT * FROM voice_profile_versions WHERE id=$1", UUID(str(request["voice_profile_version_id"])))
        voice_active = await connection.fetchval("SELECT active_version_id FROM voice_profile_heads WHERE profile_key=$1", voice["profile_key"] if voice else "")
        if voice is None or not voice["enabled"] or voice_active != voice["id"]:
            raise ApplicationError("voice profile is no longer active", non_retryable=True)
        scenes = await connection.fetch(
            """SELECT sv.id,sv.scene_id,sc.scene_key,sv.scene_order,sv.duration_seconds,
                      sv.scene_spec,sv.content_hash
               FROM scene_versions sv JOIN scenes sc ON sc.id=sv.scene_id
               WHERE sv.storyboard_version_id=$1 ORDER BY sv.scene_order""", storyboard["id"],
        )
        segments = await connection.fetch(
            """SELECT id,segment_key,segment_order,narration,duration_seconds,content_hash,annotations
               FROM script_segments WHERE script_version_id=$1 ORDER BY segment_order""",
            storyboard["script_version_id"],
        )
        sources = await connection.fetch(
            """SELECT DISTINCT sd.id,sd.title,sd.canonical_url,sd.publisher,sd.author,
                      ss.content_hash AS snapshot_hash,ss.retrieved_at
               FROM script_segments seg
               JOIN segment_claims sg ON sg.script_segment_id=seg.id
               JOIN claim_evidence ce ON ce.claim_id=sg.claim_id AND ce.relationship='supports'
               JOIN evidence_excerpts ee ON ee.id=ce.evidence_excerpt_id
               JOIN source_snapshots ss ON ss.id=ee.source_snapshot_id
               JOIN source_documents sd ON sd.id=ss.source_document_id
               WHERE seg.script_version_id=$1 ORDER BY sd.title""",
            storyboard["script_version_id"],
        )
        claim_count = await connection.fetchval("SELECT count(*) FROM script_segments seg JOIN segment_claims sc ON sc.script_segment_id=seg.id WHERE seg.script_version_id=$1", storyboard["script_version_id"])
        supported_count = await connection.fetchval("SELECT count(DISTINCT sc.id) FROM script_segments seg JOIN segment_claims sc ON sc.script_segment_id=seg.id JOIN claim_evidence ce ON ce.claim_id=sc.claim_id AND ce.relationship='supports' WHERE seg.script_version_id=$1", storyboard["script_version_id"])
        brand = normalize_brand_kit(_json(storyboard["brand_kit"]), channel_name=storyboard["channel_name"])
        return {
            "workflow_id": request["workflow_id"], "production_id": str(_production_id(request["workflow_id"])),
            "actor_id": request["actor_id"], "correlation_id": request["correlation_id"],
            "storyboard_version_id": str(storyboard["id"]), "storyboard_hash": storyboard["content_hash"],
            "storyboard_version_number": storyboard["version_number"], "script_version_id": str(storyboard["script_version_id"]),
            "channel_profile_id": str(storyboard["channel_profile_id"]),
            "channel_profile_version": storyboard["channel_profile_version"],
            "channel_name": storyboard["channel_name"],
            "render_tier": request["render_tier"], "width": request["width"], "height": request["height"], "fps": request["fps"],
            "brand": brand,
            "brand_hash": canonical_hash(brand),
            "render_policy": _json(storyboard["default_render_settings"]),
            "verification_report": _json(storyboard["verification_report"]),
            "comfy_workflow": {
                **{key: workflow[key] for key in ("workflow_key", "version_number", "purpose", "content_hash")},
                **{key: _json(workflow[key]) for key in ("api_workflow", "required_nodes", "required_models", "typed_inputs", "output_contract")},
                "id": str(workflow["id"]),
            },
            "voice_profile": {
                **{key: voice[key] for key in ("profile_key", "version_number", "provider_type", "endpoint", "voice_id", "language", "engine", "model_version", "content_hash")},
                **{key: _json(voice[key]) for key in ("delivery", "pronunciation", "output_settings", "consent")},
                "id": str(voice["id"]),
            },
            "scenes": [{"id": str(row["id"]), "scene_id": str(row["scene_id"]), "scene_key": row["scene_key"], "order": row["scene_order"], "duration_seconds": row["duration_seconds"], "scene_spec": _json(row["scene_spec"]), "content_hash": row["content_hash"]} for row in scenes],
            "segments": [{"id": str(row["id"]), "segment_key": row["segment_key"], "order": row["segment_order"], "narration": row["narration"], "duration_seconds": row["duration_seconds"], "content_hash": row["content_hash"], "annotations": _json(row["annotations"])} for row in segments],
            "sources": [{"id": str(row["id"]), "title": row["title"], "url": row["canonical_url"], "publisher": row["publisher"], "author": row["author"], "snapshot_hash": row["snapshot_hash"], "retrieved_at": row["retrieved_at"].isoformat()} for row in sources],
            "claims": {"total": claim_count, "supported": supported_count},
        }
    finally:
        await connection.close()


def _synchronize_media_timing(
    context: dict[str, Any], narration: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use real mastered narration lengths without altering approved SceneSpecs."""

    duration_by_segment = {
        str(item["script_segment_id"]): float(item["duration_seconds"])
        for item in narration["narration"]
    }
    expected_segments = {str(item["id"]) for item in context["segments"]}
    if set(duration_by_segment) != expected_segments:
        raise ValueError("narration timing does not cover the exact approved Script segments")

    assigned: list[str] = []
    timed_scenes: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    cursor = 0.0
    for scene in context["scenes"]:
        segment_ids = [str(value) for value in scene["scene_spec"].get("narration_segment_ids", [])]
        if not segment_ids or any(value not in duration_by_segment for value in segment_ids):
            raise ValueError("a SceneSpec is not bound to mastered narration")
        duration = sum(duration_by_segment[value] for value in segment_ids)
        assigned.extend(segment_ids)
        timed_scenes.append(
            {
                **scene,
                "storyboard_duration_seconds": float(scene["duration_seconds"]),
                "duration_seconds": duration,
            }
        )
        chapters.append(
            {
                "timecode_seconds": cursor,
                "title": str(scene["scene_spec"]["purpose"])[:160],
                "scene_version_id": scene["id"],
            }
        )
        cursor += duration
    if len(assigned) != len(set(assigned)) or set(assigned) != expected_segments:
        raise ValueError("mastered narration must be assigned to exactly one production scene")

    timed_segments = [
        {**item, "storyboard_duration_seconds": float(item["duration_seconds"]), "duration_seconds": duration_by_segment[str(item["id"])]}
        for item in context["segments"]
    ]
    return (
        {**context, "scenes": timed_scenes, "segments": timed_segments, "production_duration_seconds": cursor},
        {**narration, "chapters": chapters, "duration_seconds": cursor},
    )


@activity.defn(name="synchronize-media-timing")
async def synchronize_media_timing(request: dict[str, Any]) -> dict[str, Any]:
    try:
        context, narration = _synchronize_media_timing(request["context"], request["narration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApplicationError(
            f"mastered narration could not define the production timeline: {str(exc)[:1000]}",
            non_retryable=True,
        ) from exc
    return {"context": context, "narration": narration}


def _fixture_png(scene: dict[str, Any], width: int, height: int) -> bytes:
    seed = int(hashlib.sha256(scene["content_hash"].encode()).hexdigest()[:6], 16)
    image = Image.new("RGB", (width, height), ((seed >> 16) & 80, (seed >> 8) & 80, seed & 80))
    draw = ImageDraw.Draw(image)
    accent = (100 + (seed % 155), 180, 240)
    draw.rectangle((0, 0, width, max(6, height // 60)), fill=accent)
    draw.text((width // 12, height // 5), f"SCENE {scene['order']:02d}", fill=accent)
    brief = scene["scene_spec"].get("visual_brief", "TubeFactory")
    lines = [brief[index:index + 70] for index in range(0, min(len(brief), 280), 70)]
    for index, line in enumerate(lines):
        draw.text((width // 12, height // 3 + index * 26), line, fill=(240, 245, 255))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


@activity.defn(name="generate-scene-media-assets")
async def generate_scene_media_assets(context: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    minio = _minio(settings)
    production_id = UUID(context["production_id"])
    workflow = context["comfy_workflow"]
    capability_hash = canonical_hash({"required_nodes": workflow["required_nodes"], "required_models": workflow["required_models"]})
    assets: list[dict[str, Any]] = []
    for scene in context["scenes"]:
        activity.heartbeat(f"scene {scene['order']} of {len(context['scenes'])}")
        seed = int(hashlib.sha256(f"{context['storyboard_hash']}:{scene['id']}".encode()).hexdigest()[:16], 16)
        inputs = {"prompt": scene["scene_spec"]["visual_brief"], "seed": seed, "width": context["width"], "height": context["height"], "duration": scene["duration_seconds"]}
        cache_key = canonical_hash({"workflow_hash": workflow["content_hash"], "scene_hash": scene["content_hash"], "inputs": inputs, "capability_hash": capability_hash})
        provenance = {"adapter": "fixture" if workflow["workflow_key"].startswith("fixture-") else "comfyui", "workflow_id": workflow["id"], "workflow_version": workflow["version_number"], "workflow_hash": workflow["content_hash"], "prompt": inputs["prompt"], "seed": seed, "model_inventory": workflow["required_models"], "node_inventory": workflow["required_nodes"], "typed_inputs": inputs}
        cached = await _cached_media(settings, cache_key)
        if cached is not None:
            body, mime = cached
            suffix = "png" if mime == "image/png" else "mp4"
            provenance["cache_hit"] = True
        elif workflow["workflow_key"].startswith("fixture-"):
            body = await asyncio.to_thread(_fixture_png, scene, context["width"], context["height"])
            mime, suffix = "image/png", "png"
        else:
            client = ComfyUIClient(settings.comfyui_endpoint)
            await client.validate_inventory(required_nodes=workflow["required_nodes"], required_models=workflow["required_models"])
            specs = [TypedWorkflowInput(**item) for item in workflow["typed_inputs"]]
            declared = {spec.name for spec in specs}
            outputs = await client.execute(workflow=workflow["api_workflow"], typed_inputs=specs, values={key: value for key, value in inputs.items() if key in declared}, client_id=context["workflow_id"])
            body = outputs[0].body
            mime = "image/png" if outputs[0].filename.lower().endswith(".png") else "video/mp4"
            suffix = "png" if mime == "image/png" else "mp4"
        scene_root = f"productions/{production_id}/regenerations/{context['workflow_id']}/scenes" if context.get("regeneration") else f"productions/{production_id}/scenes"
        key = f"{scene_root}/{scene['order']:03d}-{scene['id']}.{suffix}"
        await _put(minio, settings.minio_bucket, key, body, mime)
        assets.append(_asset(workflow_id=context["workflow_id"], key=f"scene:{scene['id']}", production_id=production_id, kind="visual", body=body, mime=mime, object_key=key, scene_version_id=scene["id"], width=context["width"], height=context["height"], duration=scene["duration_seconds"] if mime == "video/mp4" else None, licence={"status": "cleared", "basis": "generated_for_production", "attribution": None, "synthetic": True}, provenance=provenance, cache_key=cache_key))
    return {"assets": assets, "capability_hash": capability_hash}


def _fixture_wav(text: str, duration: float, sample_rate: int) -> tuple[bytes, list[dict[str, Any]]]:
    frame_count = max(1, round(duration * sample_rate))
    words = text.split()
    alignment = []
    for index, word in enumerate(words):
        alignment.append({"word": word, "start_seconds": round(duration * index / max(1, len(words)), 3), "end_seconds": round(duration * (index + 1) / max(1, len(words)), 3)})
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        frames = bytearray()
        fade = max(1, round(sample_rate * 0.03))
        for index in range(frame_count):
            word_index = min(len(words) - 1, int(index / frame_count * max(1, len(words)))) if words else 0
            frequency = 150 + (word_index % 7) * 22
            envelope = min(1.0, index / fade, (frame_count - index) / fade)
            value = int(3100 * envelope * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.extend(struct.pack("<h", value))
        writer.writeframes(bytes(frames))
    return output.getvalue(), alignment


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[-2000:]}")


def _master_wav(original: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "original.wav"
        output = Path(directory) / "mastered.wav"
        source.write_bytes(original)
        _run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.95", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(output)])
        return output.read_bytes()


def _crossfade_wavs(parts: list[bytes], crossfade_seconds: float = 0.02) -> bytes:
    if not parts:
        raise ValueError("At least one WAV part is required")
    pcm_parts: list[bytes] = []
    parameters: tuple[int, int, int] | None = None
    for body in parts:
        with wave.open(io.BytesIO(body), "rb") as reader:
            current = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
            if current[0] != 1 or current[1] != 2 or (parameters is not None and current != parameters):
                raise ValueError("Voicebox chunks must be mono 16-bit WAV with one sample rate")
            parameters = current
            pcm_parts.append(reader.readframes(reader.getnframes()))
    assert parameters is not None
    overlap_frames = max(0, round(parameters[2] * crossfade_seconds))
    combined = bytearray(pcm_parts[0])
    for part in pcm_parts[1:]:
        overlap = min(overlap_frames, len(combined) // 2, len(part) // 2)
        if overlap:
            left = bytes(combined[len(combined) - overlap * 2:])
            right = part[:overlap * 2]
            mixed = bytearray()
            for index in range(overlap):
                a = struct.unpack_from("<h", left, index * 2)[0]
                b = struct.unpack_from("<h", right, index * 2)[0]
                ratio = (index + 1) / (overlap + 1)
                mixed.extend(struct.pack("<h", round(a * (1 - ratio) + b * ratio)))
            del combined[len(combined) - overlap * 2:]
            combined.extend(mixed)
            combined.extend(part[overlap * 2:])
        else:
            combined.extend(part)
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(parameters[0]); writer.setsampwidth(parameters[1]); writer.setframerate(parameters[2]); writer.writeframes(combined)
    return output.getvalue()


def _srt_time(seconds: float, separator: str = ",") -> str:
    millis = round(seconds * 1000)
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{ms:03d}"


@activity.defn(name="generate-production-narration")
async def generate_production_narration(context: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    minio = _minio(settings)
    production_id = UUID(context["production_id"])
    profile = context["voice_profile"]
    sample_rate = int(profile["output_settings"].get("sample_rate", 48000))
    asset_rows: list[dict[str, Any]] = []
    narration_rows: list[dict[str, Any]] = []
    mastered_pcm = bytearray()
    srt: list[str] = []
    vtt: list[str] = ["WEBVTT", ""]
    cursor = 0.0
    voice_client: VoiceboxRESTClient | VoiceboxWSClient | None = None
    if profile["provider_type"] == "voicebox_rest":
        voice_client = VoiceboxRESTClient(profile["endpoint"])
    elif profile["provider_type"] == "voicebox_ws":
        voice_client = VoiceboxWSClient(profile["endpoint"])
    if voice_client is not None:
        await voice_client.health()
    for segment in context["segments"]:
        activity.heartbeat(f"narration {segment['order']} of {len(context['segments'])}")
        request = {"text": segment["narration"], "language": profile["language"], "voice_profile_version_id": profile["id"], "engine": profile["engine"], "delivery": profile["delivery"], "speed": profile["delivery"].get("speed", 1.0), "pronunciation": profile["pronunciation"], "output": {"format": "wav", "sample_rate": sample_rate}, "sampling": profile["output_settings"].get("sampling", {}), "consent": profile["consent"]}
        chunks = stable_voice_chunks(segment["narration"], maximum_characters=int(profile["output_settings"].get("maximum_chunk_characters", 500)))
        request_hash = canonical_hash(request)
        voice_cache_key = canonical_hash({"kind": "voice_original", "request_hash": request_hash})
        cached_voice = await _cached_narration(settings, voice_cache_key)
        if cached_voice is not None:
            original, alignment = cached_voice["audio"], cached_voice["alignment"]
            engine, model_version, warnings = cached_voice["engine"], cached_voice["model_version"], cached_voice["warnings"]
        elif profile["provider_type"] == "fake":
            original, alignment = await asyncio.to_thread(_fixture_wav, segment["narration"], segment["duration_seconds"], sample_rate)
            engine, model_version, warnings = profile["engine"], profile["model_version"], []
        elif voice_client is not None:
            results = []
            for chunk in chunks:
                results.append(await voice_client.synthesize({**request, "text": chunk}))
            original = await asyncio.to_thread(_crossfade_wavs, [item.audio for item in results])
            alignment, alignment_cursor = [], 0.0
            for index, item in enumerate(results):
                alignment.extend([{**word, "start_seconds": float(word["start_seconds"]) + alignment_cursor, "end_seconds": float(word["end_seconds"]) + alignment_cursor} for word in item.word_alignment])
                alignment_cursor += item.duration_seconds - (0.02 if index < len(results) - 1 else 0)
            engine, model_version = results[0].engine, results[0].model_version
            warnings = [warning for item in results for warning in item.warnings]
        else:
            raise ApplicationError("Unsupported Voicebox provider type", non_retryable=True)
        mastered = await asyncio.to_thread(_master_wav, original)
        with wave.open(io.BytesIO(mastered), "rb") as reader:
            duration = reader.getnframes() / reader.getframerate()
            mastered_pcm.extend(reader.readframes(reader.getnframes()))
        narration_root = f"productions/{production_id}/regenerations/{context['workflow_id']}/narration" if context.get("regeneration") else f"productions/{production_id}/narration"
        base = f"{narration_root}/{segment['order']:03d}-{segment['segment_key']}"
        original_key, mastered_key = f"{base}-original.wav", f"{base}-mastered.wav"
        await _put(minio, settings.minio_bucket, original_key, original, "audio/wav")
        await _put(minio, settings.minio_bucket, mastered_key, mastered, "audio/wav")
        original_asset = _asset(workflow_id=context["workflow_id"], key=f"narration-original:{segment['id']}", production_id=production_id, kind="narration_original", body=original, mime="audio/wav", object_key=original_key, duration=duration, licence={"status": "cleared", "basis": "voice_profile_consent", "consent": profile["consent"], "synthetic": True}, provenance={"provider_type": profile["provider_type"], "profile_version_id": profile["id"], "profile_hash": profile["content_hash"], "engine": engine, "model_version": model_version, "request_hash": request_hash}, cache_key=voice_cache_key)
        mastered_asset = _asset(workflow_id=context["workflow_id"], key=f"narration-mastered:{segment['id']}", production_id=production_id, kind="narration_mastered", body=mastered, mime="audio/wav", object_key=mastered_key, duration=duration, licence=original_asset["licence"], provenance={**original_asset["generation_provenance"], "mastering": {"loudness_lufs": -16, "true_peak_db": -1.5, "limiter": 0.95}}, cache_key=canonical_hash({"kind": "voice_mastered", "request_hash": request_hash}))
        asset_rows.extend((original_asset, mastered_asset))
        response = {"segments": [{"order": index, "text_hash": hashlib.sha256(chunk.encode()).hexdigest()} for index, chunk in enumerate(chunks, 1)], "duration_seconds": duration, "sample_rate": sample_rate, "engine": engine, "model_version": model_version, "warnings": warnings}
        narration_rows.append({"id": str(uuid5(NAMESPACE_URL, f"{context['workflow_id']}:narration:{segment['id']}")), "script_segment_id": segment["id"], "segment_order": segment["order"], "request": request, "response": response, "original_asset_id": original_asset["id"], "mastered_asset_id": mastered_asset["id"], "duration_seconds": duration, "sample_rate": sample_rate, "word_alignment": [{**item, "start_seconds": item["start_seconds"] + cursor, "end_seconds": item["end_seconds"] + cursor} for item in alignment], "content_hash": canonical_hash({"request": request, "response": response, "mastered_hash": mastered_asset["content_hash"]})})
        end = cursor + duration
        srt.extend([str(segment["order"]), f"{_srt_time(cursor)} --> {_srt_time(end)}", segment["narration"], ""])
        vtt.extend([f"{_srt_time(cursor, '.')} --> {_srt_time(end, '.')}", segment["narration"], ""])
        cursor = end
    if context.get("regeneration"):
        return {"assets": asset_rows, "narration": narration_rows, "duration_seconds": cursor}
    combined = io.BytesIO()
    with wave.open(combined, "wb") as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(48000); writer.writeframes(bytes(mastered_pcm))
    combined_body = combined.getvalue()
    combined_key = f"productions/{production_id}/narration/mastered-full.wav"
    await _put(minio, settings.minio_bucket, combined_key, combined_body, "audio/wav")
    combined_asset = _asset(workflow_id=context["workflow_id"], key="narration-mastered:full", production_id=production_id, kind="narration_mastered", body=combined_body, mime="audio/wav", object_key=combined_key, duration=cursor, licence={"status": "cleared", "basis": "voice_profile_consent", "consent": profile["consent"], "synthetic": True}, provenance={"profile_version_id": profile["id"], "profile_hash": profile["content_hash"], "assembly": "ordered-pcm-concatenation"})
    asset_rows.append(combined_asset)
    auxiliaries = [("captions.srt", "caption_srt", "application/x-subrip", "\n".join(srt).encode()), ("captions.vtt", "caption_vtt", "text/vtt", "\n".join(vtt).encode())]
    _, timed_narration = _synchronize_media_timing(
        context,
        {"narration": narration_rows, "duration_seconds": cursor, "chapters": []},
    )
    chapters = timed_narration["chapters"]
    description = {"title": f"{context['channel_name']} production", "language": profile["language"], "synthetic_media_disclosure": "This production contains synthetic visuals and narration where identified in the manifest.", "source_count": len(context["sources"])}
    auxiliaries.extend([("chapters.json", "chapter", "application/json", json.dumps(chapters, sort_keys=True).encode()), ("description.json", "description", "application/json", json.dumps(description, sort_keys=True).encode()), ("sources.json", "source_list", "application/json", json.dumps(context["sources"], sort_keys=True).encode())])
    for filename, kind, mime, body in auxiliaries:
        key = f"productions/{production_id}/{filename}"
        await _put(minio, settings.minio_bucket, key, body, mime)
        asset_rows.append(_asset(workflow_id=context["workflow_id"], key=filename, production_id=production_id, kind=kind, body=body, mime=mime, object_key=key, licence={"status": "cleared", "basis": "production_metadata", "synthetic": False}, provenance={"storyboard_hash": context["storyboard_hash"], "script_version_id": context["script_version_id"]}))
    return {"assets": asset_rows, "narration": narration_rows, "combined_audio_asset_id": combined_asset["id"], "combined_audio_key": combined_key, "duration_seconds": cursor, "chapters": chapters, "description": description}


def _probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError("ffprobe could not read the render")
    return json.loads(result.stdout)


def _ffmpeg_scan(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-vf", "blackdetect=d=1:pix_th=0.02,freezedetect=n=-60dB:d=3",
            "-af", "volumedetect,silencedetect=n=-50dB:d=1",
            "-f", "null", "-",
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    output = result.stderr.decode(errors="replace")
    if result.returncode:
        raise RuntimeError(f"FFmpeg QA scan failed: {output[-2000:]}")
    number = r"(-?(?:\d+(?:\.\d+)?|inf))"
    mean = re.findall(rf"mean_volume:\s*{number}\s*dB", output)
    peak = re.findall(rf"max_volume:\s*{number}\s*dB", output)
    return {
        "mean_volume_db": float(mean[-1]) if mean and mean[-1] != "-inf" else -999.0,
        "max_volume_db": float(peak[-1]) if peak and peak[-1] != "-inf" else -999.0,
        "silence_seconds": sum(float(item) for item in re.findall(r"silence_duration:\s*([0-9.]+)", output)),
        "black_segments": len(re.findall(r"black_start:", output)),
        "freeze_segments": len(re.findall(r"freeze_start:", output)),
    }


async def _await_with_activity_heartbeat(
    awaitable: Any,
    detail: str,
    *,
    interval_seconds: float = 30.0,
) -> Any:
    """Await long provider/QA work while proving liveness to Temporal."""

    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            activity.heartbeat({"state": detail})
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task), timeout=interval_seconds
                )
            except TimeoutError:
                continue
    finally:
        if not task.done():
            task.cancel()


@activity.defn(name="assemble-and-qa-production")
async def assemble_and_qa_production(request: dict[str, Any]) -> dict[str, Any]:
    context, scene_result, narration = request["context"], request["scene_result"], request["narration"]
    settings = Settings()
    minio = _minio(settings)
    production_id = UUID(context["production_id"])
    assets_by_scene: dict[str, list[dict[str, Any]]] = {}
    for asset in scene_result["assets"]:
        assets_by_scene.setdefault(str(asset.get("scene_version_id")), []).append(asset)
    invalid_bindings = [scene["id"] for scene in context["scenes"] if len(assets_by_scene.get(scene["id"], [])) != 1]
    if invalid_bindings:
        raise ApplicationError(
            "every approved SceneSpec must resolve to exactly one Scene Media Asset: " + ", ".join(invalid_bindings[:20]),
            non_retryable=True,
        )
    scene_props = []
    manifest_scene_props = []
    for scene in context["scenes"]:
        asset = assets_by_scene[scene["id"]][0]
        asset_url = await asyncio.to_thread(
            minio.presigned_get_object,
            settings.minio_bucket,
            asset["object_key"],
            timedelta(hours=2),
        )
        common = {
            "sceneVersionId": scene["id"],
            "durationSeconds": scene["duration_seconds"],
            "storyboardDurationSeconds": scene.get("storyboard_duration_seconds", scene["duration_seconds"]),
            "purpose": scene["scene_spec"]["purpose"],
            "visualType": scene["scene_spec"]["visual_type"],
            "onScreenText": scene["scene_spec"]["on_screen_text"],
            "citationStyle": scene["scene_spec"]["citation_style"],
            "syntheticMediaFlag": scene["scene_spec"]["synthetic_media_flag"],
            "assetHash": asset["content_hash"],
            "assetMimeType": asset["mime_type"],
        }
        scene_props.append({**common, "assetUrl": asset_url})
        manifest_scene_props.append(common)
    brand_kit = context["brand"]
    brand = {**brand_kit, "name": context["channel_name"], "brandHash": context["brand_hash"], "channelProfileVersion": context["channel_profile_version"]}
    render_props = {"width": context["width"], "height": context["height"], "fps": context["fps"], "scenes": scene_props, "brand": brand}
    props = {"width": context["width"], "height": context["height"], "fps": context["fps"], "scenes": manifest_scene_props, "brand": brand}
    async with httpx.AsyncClient(timeout=900, follow_redirects=False) as client:
        response = await _await_with_activity_heartbeat(
            client.post(f"{settings.render_endpoint.rstrip('/')}/render", json=render_props),
            "waiting_for_remotion_render",
        )
        if response.status_code != 200:
            raise ApplicationError(f"Remotion render failed: {response.text[:1000]}")
        silent_video = response.content
        engine = response.headers.get("X-Render-Engine", "remotion")
        engine_version = response.headers.get("X-Render-Engine-Version", "unknown")
        composition = response.headers.get("X-Composition", "EvidenceVideo")
    audio = await _await_with_activity_heartbeat(
        asyncio.to_thread(_get, minio, settings.minio_bucket, narration["combined_audio_key"]),
        "loading_mastered_narration",
    )
    with tempfile.TemporaryDirectory() as directory:
        silent_path, audio_path = Path(directory) / "visual.mp4", Path(directory) / "narration.wav"
        output_path, thumb_path = Path(directory) / "master.mp4", Path(directory) / "thumbnail.jpg"
        silent_path.write_bytes(silent_video); audio_path.write_bytes(audio)
        await _await_with_activity_heartbeat(
            asyncio.to_thread(_run_ffmpeg, ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(silent_path), "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(output_path)]),
            "muxing_video_and_narration",
        )
        final_video = output_path.read_bytes()
        probe = await _await_with_activity_heartbeat(
            asyncio.to_thread(_probe, output_path), "probing_render"
        )
        scan = await _await_with_activity_heartbeat(
            asyncio.to_thread(_ffmpeg_scan, output_path), "scanning_render_qa"
        )
        await _await_with_activity_heartbeat(
            asyncio.to_thread(_run_ffmpeg, ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(output_path), "-frames:v", "1", "-q:v", "2", str(thumb_path)]),
            "extracting_thumbnail",
        )
        thumbnail = thumb_path.read_bytes()
    tier_kind = "render_preview" if context["render_tier"] == "preview" else "render_master"
    video_key = f"productions/{production_id}/{context['render_tier']}-master.mp4"
    thumb_key = f"productions/{production_id}/thumbnail.jpg"
    await _put(minio, settings.minio_bucket, video_key, final_video, "video/mp4")
    await _put(minio, settings.minio_bucket, thumb_key, thumbnail, "image/jpeg")
    video_asset = _asset(workflow_id=context["workflow_id"], key=f"render:{context['render_tier']}", production_id=production_id, kind=tier_kind, body=final_video, mime="video/mp4", object_key=video_key, width=context["width"], height=context["height"], duration=narration["duration_seconds"], licence={"status": "cleared", "basis": "assembled_from_cleared_assets", "synthetic": True}, provenance={"engine": engine, "engine_version": engine_version, "composition": composition, "props_hash": canonical_hash(props), "ffmpeg": "5.1.9", "storyboard_hash": context["storyboard_hash"]})
    thumb_asset = _asset(workflow_id=context["workflow_id"], key="thumbnail", production_id=production_id, kind="thumbnail", body=thumbnail, mime="image/jpeg", object_key=thumb_key, width=context["width"], height=context["height"], licence=video_asset["licence"], provenance={"ffmpeg": "5.1.9", "source_render_hash": video_asset["content_hash"]})
    all_assets = [*scene_result["assets"], *narration["assets"], video_asset, thumb_asset]
    manifest = {"schema_version": "1.0", "production_id": str(production_id), "storyboard_version_id": context["storyboard_version_id"], "storyboard_hash": context["storyboard_hash"], "render_tier": context["render_tier"], "brand": {"channel_profile_id": context["channel_profile_id"], "channel_profile_version": context["channel_profile_version"], "brand_hash": context["brand_hash"], "brand_kit": brand_kit}, "scenes": [{"scene_version_id": item["id"], "scene_hash": item["content_hash"], "production_duration_seconds": item["duration_seconds"], "storyboard_duration_seconds": item.get("storyboard_duration_seconds", item["duration_seconds"]), "scene_spec": item["scene_spec"]} for item in context["scenes"]], "narration": narration["narration"], "captions": [item for item in all_assets if item["asset_kind"].startswith("caption_")], "chapters": narration["chapters"], "sources": context["sources"], "assets": [{key: item[key] for key in ("id", "asset_kind", "object_key", "content_hash", "mime_type", "licence", "generation_provenance")} for item in all_assets], "render": {"asset_id": video_asset["id"], "content_hash": video_asset["content_hash"], "engine": engine, "engine_version": engine_version, "composition": composition, "settings": props, "probe": probe}}
    manifest_body = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    manifest_key = f"productions/{production_id}/production-manifest.json"
    await _put(minio, settings.minio_bucket, manifest_key, manifest_body, "application/json")
    manifest_asset = _asset(workflow_id=context["workflow_id"], key="manifest", production_id=production_id, kind="manifest", body=manifest_body, mime="application/json", object_key=manifest_key, licence={"status": "cleared", "basis": "production_metadata", "synthetic": False}, provenance={"schema_version": "1.0", "storyboard_hash": context["storyboard_hash"]})
    all_assets.append(manifest_asset)
    streams = probe.get("streams", [])
    has_video, has_audio = any(item.get("codec_type") == "video" for item in streams), any(item.get("codec_type") == "audio" for item in streams)
    probed_duration = float(probe.get("format", {}).get("duration", 0))
    expected_duration = sum(scene["duration_seconds"] for scene in context["scenes"])
    caption_assets = [item for item in all_assets if item["asset_kind"] in {"caption_srt", "caption_vtt"}]
    licences_clear = all(item["licence"].get("status") == "cleared" for item in all_assets)
    qa_policy = (context.get("render_policy") or {}).get("qa", {})
    freshness_days = int(qa_policy.get("source_freshness_days", 365))
    caption_max_characters = int(qa_policy.get("caption_max_characters", 120))
    caption_max_words_per_second = float(qa_policy.get("caption_max_words_per_second", 3.5))
    minimum_mean_volume = float(qa_policy.get("minimum_mean_volume_db", -24))
    maximum_mean_volume = float(qa_policy.get("maximum_mean_volume_db", -10))
    maximum_peak_volume = float(qa_policy.get("maximum_peak_volume_db", -0.5))
    maximum_silence_ratio = float(qa_policy.get("maximum_silence_ratio", 0.1))
    now = datetime.now(timezone.utc)
    expired_sources = [item for item in context["sources"] if (now - datetime.fromisoformat(item["retrieved_at"])).days > freshness_days]
    verification_issues = (context.get("verification_report") or {}).get("issues", [])
    contradiction_issues = [item for item in verification_issues if "contradict" in str(item.get("code", "")).lower()]
    caption_overflow = [item["segment_key"] for item in context["segments"] if len(item["narration"]) > caption_max_characters]
    caption_rate = max((len(item["narration"].split()) / max(0.1, float(item["duration_seconds"])) for item in context["segments"]), default=0.0)
    silence_ratio = scan["silence_seconds"] / max(0.1, probed_duration)
    synthetic_assets = [item for item in all_assets if item["licence"].get("synthetic")]
    disclosed = bool(narration["description"].get("synthetic_media_disclosure"))
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        prior_rows = await connection.fetch(
            """SELECT ma.content_hash FROM media_assets ma
               JOIN media_productions mp ON mp.id=ma.production_id
               JOIN storyboard_versions bv ON bv.id=mp.storyboard_version_id
               JOIN storyboards b ON b.id=bv.storyboard_id
               JOIN scripts s ON s.id=b.script_id
               JOIN opportunities o ON o.id=s.opportunity_id
               JOIN subject_profiles sp ON sp.id=o.subject_profile_id
               WHERE sp.channel_profile_id=$1 AND mp.id<>$2 AND ma.asset_kind='visual'""",
            UUID(context["channel_profile_id"]), production_id,
        )
        prior_hashes = {row["content_hash"] for row in prior_rows}
    finally:
        await connection.close()
    visual_hashes = {item["content_hash"] for item in scene_result["assets"]}
    prior_overlap = len(visual_hashes & prior_hashes)
    findings = [
        {"code": "media_decodable", "verdict": "pass" if has_video and has_audio else "fail", "message": "ffprobe found video and audio streams." if has_video and has_audio else "Render is corrupt or is missing a required stream.", "override_policy": "never", "details": {"has_video": has_video, "has_audio": has_audio}},
        {"code": "timing_alignment", "verdict": "pass" if abs(probed_duration - expected_duration) <= 1 else "fail", "message": "Render timing matches the approved storyboard." if abs(probed_duration - expected_duration) <= 1 else "Render duration diverges from storyboard timing.", "override_policy": "never", "details": {"expected_seconds": expected_duration, "actual_seconds": probed_duration}},
        {"code": "claim_evidence_coverage", "verdict": "pass" if context["claims"]["total"] == context["claims"]["supported"] else "fail", "message": "Every narrated claim has supporting evidence." if context["claims"]["total"] == context["claims"]["supported"] else "One or more narrated claims lack supporting evidence.", "override_policy": "never", "details": context["claims"]},
        {"code": "asset_licences", "verdict": "pass" if licences_clear else "fail", "message": "Every production asset has a cleared licence basis." if licences_clear else "An asset licence is unresolved.", "override_policy": "never", "details": {"asset_count": len(all_assets)}},
        {"code": "captions_present", "verdict": "pass" if len(caption_assets) == 2 else "fail", "message": "SRT and WebVTT captions are present." if len(caption_assets) == 2 else "Required caption formats are missing.", "override_policy": "never", "details": {"formats": [item["mime_type"] for item in caption_assets]}},
        {"code": "contradictions_and_freshness", "verdict": "pass" if not contradiction_issues and not expired_sources else "fail", "message": "No unresolved contradiction omissions or expired evidence snapshots were found." if not contradiction_issues and not expired_sources else "Contradiction review or source freshness requires attention.", "override_policy": "reasoned", "details": {"contradiction_issues": contradiction_issues, "expired_source_ids": [item["id"] for item in expired_sources], "freshness_days": freshness_days}},
        {"code": "audio_levels", "verdict": "pass" if minimum_mean_volume <= scan["mean_volume_db"] <= maximum_mean_volume and scan["max_volume_db"] <= maximum_peak_volume else "fail", "message": "Audio loudness and peak levels are within channel thresholds." if minimum_mean_volume <= scan["mean_volume_db"] <= maximum_mean_volume and scan["max_volume_db"] <= maximum_peak_volume else "Audio is too quiet, too loud, or clipping.", "override_policy": "never", "details": {**scan, "mean_volume_range_db": [minimum_mean_volume, maximum_mean_volume], "maximum_peak_volume_db": maximum_peak_volume}},
        {"code": "audio_silence", "verdict": "pass" if silence_ratio <= maximum_silence_ratio else "fail", "message": "No excessive silence was detected." if silence_ratio <= maximum_silence_ratio else "Excessive silence was detected in the mastered audio.", "override_policy": "reasoned", "details": {"silence_ratio": silence_ratio, "maximum_silence_ratio": maximum_silence_ratio}},
        {"code": "black_and_frozen_frames", "verdict": "fail" if scan["black_segments"] else ("warn" if scan["freeze_segments"] else "pass"), "message": "No black or frozen frame runs were detected." if not scan["black_segments"] and not scan["freeze_segments"] else "Black or frozen frame runs require visual review.", "override_policy": "reasoned", "details": {"black_segments": scan["black_segments"], "freeze_segments": scan["freeze_segments"]}},
        {"code": "caption_readability_and_sync", "verdict": "fail" if abs(narration["duration_seconds"] - probed_duration) > 1 else ("warn" if caption_overflow or caption_rate > caption_max_words_per_second else "pass"), "message": "Caption timing, line length and reading rate are within thresholds." if not caption_overflow and caption_rate <= caption_max_words_per_second and abs(narration["duration_seconds"] - probed_duration) <= 1 else "Caption timing or readability requires review.", "override_policy": "reasoned", "details": {"overflow_segment_keys": caption_overflow, "maximum_words_per_second": caption_rate, "threshold_words_per_second": caption_max_words_per_second, "sync_delta_seconds": abs(narration["duration_seconds"] - probed_duration)}},
        {"code": "chapters_sources_accessibility", "verdict": "pass" if narration["chapters"] and context["sources"] and all(scene["scene_spec"].get("accessibility_notes") for scene in context["scenes"]) else "fail", "message": "Chapters, source list and scene accessibility notes are present.", "override_policy": "reasoned", "details": {"chapters": len(narration["chapters"]), "sources": len(context["sources"])}},
        {"code": "synthetic_disclosure", "verdict": "pass" if not synthetic_assets or disclosed else "fail", "message": "Synthetic visual and narration disclosure is recorded in the manifest and description." if not synthetic_assets or disclosed else "Synthetic assets exist without the required disclosure.", "override_policy": "reasoned", "details": {"disclosed": disclosed, "synthetic_asset_count": len(synthetic_assets)}},
        {"code": "repeated_asset_similarity", "verdict": "warn" if len({item["content_hash"] for item in scene_result["assets"]}) < len(scene_result["assets"]) else "pass", "message": "Scene asset hashes were checked for exact repetition.", "override_policy": "reasoned", "details": {"scene_assets": len(scene_result["assets"]), "unique_hashes": len({item["content_hash"] for item in scene_result["assets"]})}},
        {"code": "prior_video_similarity", "verdict": "warn" if prior_overlap else "pass", "message": "Scene asset hashes were compared with prior channel productions.", "override_policy": "reasoned", "details": {"overlapping_asset_hashes": prior_overlap, "prior_asset_hashes": len(prior_hashes)}},
    ]
    verdict = "fail" if any(item["verdict"] == "fail" for item in findings) else ("warn" if any(item["verdict"] == "warn" for item in findings) else "pass")
    qa = {"verdict": verdict, "policy_snapshot": {"version": "media-qa-2", "mandatory": [item["code"] for item in findings if item["override_policy"] == "never"], "thresholds": {"source_freshness_days": freshness_days, "caption_max_characters": caption_max_characters, "caption_max_words_per_second": caption_max_words_per_second, "minimum_mean_volume_db": minimum_mean_volume, "maximum_mean_volume_db": maximum_mean_volume, "maximum_peak_volume_db": maximum_peak_volume, "maximum_silence_ratio": maximum_silence_ratio}}, "metrics": {"expected_duration_seconds": expected_duration, "probed_duration_seconds": probed_duration, "stream_count": len(streams), "asset_count": len(all_assets), "caption_formats": 2, "audio_scan": scan, "caption_max_words_per_second": caption_rate, "prior_visual_hash_overlap": prior_overlap}, "findings": findings}
    qa["content_hash"] = canonical_hash(qa)
    return {"assets": all_assets, "manifest": {"id": str(uuid5(NAMESPACE_URL, f"{context['workflow_id']}:manifest:1")), "document": manifest, "content_hash": hashlib.sha256(manifest_body).hexdigest(), "object_key": manifest_key}, "render": {"id": str(uuid5(NAMESPACE_URL, f"{context['workflow_id']}:render:1")), "video_asset_id": video_asset["id"], "content_hash": video_asset["content_hash"], "engine": engine, "engine_version": engine_version, "composition": composition, "settings": props, "probe": probe}, "qa": qa, "narration": narration["narration"]}


@activity.defn(name="persist-media-production")
async def persist_media_production(request: dict[str, Any]) -> dict[str, Any]:
    context, result = request["context"], request["result"]
    settings = Settings()
    connection = await asyncpg.connect(settings.database_dsn)
    transaction = connection.transaction()
    await transaction.start()
    try:
        existing = await connection.fetchrow("SELECT id,state FROM media_productions WHERE workflow_id=$1", context["workflow_id"])
        if existing:
            await transaction.rollback()
            return {"production_id": str(existing["id"]), "state": existing["state"], "reconciled": True}
        now = datetime.now(timezone.utc)
        production_id = UUID(context["production_id"])
        await connection.execute(
            """INSERT INTO media_productions(id,storyboard_version_id,storyboard_hash,workflow_id,render_tier,state,settings,correlation_id,started_by,created_at,completed_at)
               VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$10)""",
            production_id, UUID(context["storyboard_version_id"]), context["storyboard_hash"], context["workflow_id"], context["render_tier"], "ready" if result["qa"]["verdict"] != "fail" else "blocked", json.dumps({"width": context["width"], "height": context["height"], "fps": context["fps"], "comfy_workflow_version_id": context["comfy_workflow"]["id"], "voice_profile_version_id": context["voice_profile"]["id"], "channel_profile_version": context["channel_profile_version"], "brand_hash": context["brand_hash"], "production_duration_seconds": context.get("production_duration_seconds")}), context["correlation_id"], UUID(context["actor_id"]), now,
        )
        for asset in result["assets"]:
            await connection.execute(
                """INSERT INTO media_assets(id,production_id,scene_version_id,asset_kind,object_key,content_hash,mime_type,byte_size,width,height,duration_seconds,licence,generation_provenance,cache_key,created_at)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,$14,$15)""",
                UUID(asset["id"]), production_id, UUID(asset["scene_version_id"]) if asset["scene_version_id"] else None, asset["asset_kind"], asset["object_key"], asset["content_hash"], asset["mime_type"], asset["byte_size"], asset["width"], asset["height"], asset["duration_seconds"], json.dumps(asset["licence"]), json.dumps(asset["generation_provenance"]), asset["cache_key"], now,
            )
        for item in result["narration"]:
            await connection.execute(
                """INSERT INTO narration_segments(id,production_id,script_segment_id,segment_order,request,response,original_asset_id,mastered_asset_id,duration_seconds,sample_rate,word_alignment,content_hash,parent_segment_id,created_at)
                   VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10,$11::jsonb,$12,NULL,$13)""",
                UUID(item["id"]), production_id, UUID(item["script_segment_id"]), item["segment_order"], json.dumps(item["request"]), json.dumps(item["response"]), UUID(item["original_asset_id"]), UUID(item["mastered_asset_id"]), item["duration_seconds"], item["sample_rate"], json.dumps(item["word_alignment"]), item["content_hash"], now,
            )
        manifest, render, qa = result["manifest"], result["render"], result["qa"]
        await connection.execute("INSERT INTO production_manifests(id,production_id,manifest_version,document,content_hash,object_key,created_at) VALUES($1,$2,1,$3::jsonb,$4,$5,$6)", UUID(manifest["id"]), production_id, json.dumps(manifest["document"]), manifest["content_hash"], manifest["object_key"], now)
        await connection.execute("""INSERT INTO production_renders(id,production_id,render_number,tier,video_asset_id,manifest_id,render_engine,render_engine_version,composition,settings,probe,content_hash,created_at)
                                    VALUES($1,$2,1,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11,$12)""", UUID(render["id"]), production_id, context["render_tier"], UUID(render["video_asset_id"]), UUID(manifest["id"]), render["engine"], render["engine_version"], render["composition"], json.dumps(render["settings"]), json.dumps(render["probe"]), render["content_hash"], now)
        report_id = uuid5(NAMESPACE_URL, f"{context['workflow_id']}:qa:1")
        await connection.execute("INSERT INTO qa_reports(id,render_id,verdict,policy_snapshot,metrics,content_hash,created_at) VALUES($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7)", report_id, UUID(render["id"]), qa["verdict"], json.dumps(qa["policy_snapshot"]), json.dumps(qa["metrics"]), qa["content_hash"], now)
        for order, finding in enumerate(qa["findings"], 1):
            await connection.execute("""INSERT INTO qa_findings(id,report_id,finding_order,code,verdict,message,scene_version_id,timecode_seconds,details,override_policy,created_at)
                                        VALUES($1,$2,$3,$4,$5,$6,NULL,NULL,$7::jsonb,$8,$9)""", uuid5(NAMESPACE_URL, f"{context['workflow_id']}:qa:{finding['code']}"), report_id, order, finding["code"], finding["verdict"], finding["message"], json.dumps(finding["details"]), finding["override_policy"], now)
        await append_audit(connection, action="media_production.completed" if qa["verdict"] != "fail" else "media_production.blocked", actor_id=UUID(context["actor_id"]), target_type="media_production", target_id=str(production_id), correlation_id=context["correlation_id"], context={"workflow_id": context["workflow_id"], "storyboard_version_id": context["storyboard_version_id"], "storyboard_hash": context["storyboard_hash"], "render_hash": render["content_hash"], "manifest_hash": manifest["content_hash"], "qa_hash": qa["content_hash"], "qa_verdict": qa["verdict"]})
        await transaction.commit()
        return {"production_id": str(production_id), "state": "ready" if qa["verdict"] != "fail" else "blocked", "render_id": render["id"], "render_hash": render["content_hash"], "manifest_hash": manifest["content_hash"], "qa_verdict": qa["verdict"], "reconciled": False}
    except Exception:
        await transaction.rollback()
        raise
    finally:
        await connection.close()


@activity.defn(name="persist-media-regeneration")
async def persist_media_regeneration(request: dict[str, Any]) -> dict[str, Any]:
    context, result, kind = request["context"], request["result"], request["kind"]
    settings = Settings()
    connection = await asyncpg.connect(settings.database_dsn)
    transaction = connection.transaction()
    await transaction.start()
    try:
        existing = await connection.fetchval("SELECT 1 FROM media_assets WHERE generation_provenance->>'regeneration_workflow_id'=$1", context["workflow_id"])
        if existing:
            await transaction.rollback()
            return {"production_id": context["production_id"], "kind": kind, "reconciled": True}
        now = datetime.now(timezone.utc)
        production_id = UUID(context["production_id"])
        for asset in result["assets"]:
            provenance = {**asset["generation_provenance"], "regeneration_workflow_id": context["workflow_id"], "regeneration_instruction": context.get("regeneration_instruction", "")}
            await connection.execute(
                """INSERT INTO media_assets(id,production_id,scene_version_id,asset_kind,object_key,content_hash,mime_type,byte_size,width,height,duration_seconds,licence,generation_provenance,cache_key,created_at)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,$14,$15)""",
                UUID(asset["id"]), production_id, UUID(asset["scene_version_id"]) if asset["scene_version_id"] else None, asset["asset_kind"], asset["object_key"], asset["content_hash"], asset["mime_type"], asset["byte_size"], asset["width"], asset["height"], asset["duration_seconds"], json.dumps(asset["licence"]), json.dumps(provenance), asset["cache_key"], now,
            )
        for item in result.get("narration", []):
            parent = await connection.fetchval("SELECT id FROM narration_segments WHERE production_id=$1 AND script_segment_id=$2 ORDER BY created_at DESC LIMIT 1", production_id, UUID(item["script_segment_id"]))
            await connection.execute(
                """INSERT INTO narration_segments(id,production_id,script_segment_id,segment_order,request,response,original_asset_id,mastered_asset_id,duration_seconds,sample_rate,word_alignment,content_hash,parent_segment_id,created_at)
                   VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10,$11::jsonb,$12,$13,$14)""",
                UUID(item["id"]), production_id, UUID(item["script_segment_id"]), item["segment_order"], json.dumps(item["request"]), json.dumps(item["response"]), UUID(item["original_asset_id"]), UUID(item["mastered_asset_id"]), item["duration_seconds"], item["sample_rate"], json.dumps(item["word_alignment"]), item["content_hash"], parent, now,
            )
        await append_audit(connection, action=f"media.{kind}_regenerated", actor_id=UUID(context["actor_id"]), target_type="media_production", target_id=str(production_id), correlation_id=context["correlation_id"], context={"workflow_id": context["workflow_id"], "instruction": context.get("regeneration_instruction", ""), "asset_ids": [item["id"] for item in result["assets"]]})
        await transaction.commit()
        return {"production_id": str(production_id), "kind": kind, "asset_ids": [item["id"] for item in result["assets"]], "reconciled": False}
    except Exception:
        await transaction.rollback()
        raise
    finally:
        await connection.close()
