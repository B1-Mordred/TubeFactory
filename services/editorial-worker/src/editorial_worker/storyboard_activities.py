from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import asyncpg
from temporalio import activity
from temporalio.exceptions import ApplicationError

from editorial_core.editorial import validate_scene_spec, validate_storyboard
from editorial_worker.config import Settings
from editorial_worker.channel_workflow import channel_workflow_context
from editorial_worker.contracts import StoryboardContentDraft, StoryboardDraft
from editorial_worker.db import append_audit
from editorial_worker.model_activities import load_task_routes


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _diverse_visual_plan(
    source_ids_by_segment: list[list[str]], requested: list[str]
) -> list[str]:
    """Keep a good model plan, otherwise apply a deterministic varied visual rhythm."""

    source_bound = {"citation_card", "chart", "source_screenshot"}
    safe_requested = [
        visual_type
        if source_ids or visual_type not in source_bound
        else "diagram"
        for visual_type, source_ids in zip(requested, source_ids_by_segment, strict=True)
    ]
    minimum_types = min(3, len(safe_requested))
    has_adjacent_repeat = any(
        current == previous
        for previous, current in zip(safe_requested, safe_requested[1:], strict=False)
    )
    if len(set(safe_requested)) >= minimum_types and not has_adjacent_repeat:
        return safe_requested

    rhythm = (
        "title_card",
        "timeline",
        "chart",
        "diagram",
        "branded_transition",
        "text",
        "citation_card",
        "branded_transition",
    )
    plan: list[str] = []
    for index, source_ids in enumerate(source_ids_by_segment):
        candidate = rhythm[index % len(rhythm)]
        if candidate in source_bound and not source_ids:
            candidate = "text" if index % 2 else "timeline"
        if plan and candidate == plan[-1]:
            candidate = "diagram" if candidate != "diagram" else "text"
        plan.append(candidate)
    return plan


def _diverse_visual_brief(
    visual_type: str, purpose: str, on_screen_text: list[str], original: str
) -> str:
    text = " · ".join(on_screen_text[:3]) or purpose
    if visual_type == "title_card":
        return f"Klare typografische Titelkarte mit einem zentralen Leitmotiv. Kerntext: {text}"
    if visual_type == "timeline":
        return f"Horizontale, schrittweise Abfolge ohne erfundene Datenwerte. Inhalt: {text}"
    if visual_type == "chart":
        return (
            "Quellengebundene qualitative Chart-Darstellung ohne erfundene Zahlen oder "
            f"Achsenwerte. Inhalt: {text}"
        )
    if visual_type == "text":
        return f"Ruhige typografische Erklärkarte mit klarer visueller Hierarchie. Inhalt: {text}"
    if visual_type == "citation_card":
        return f"Quellenkarte mit sichtbarer Claim-zu-Quelle-Zuordnung. Kernaussage: {text}"
    if visual_type == "branded_transition":
        return f"Kurze FaktischSimpel-Zwischenkarte als visueller Kapitelwechsel. Inhalt: {text}"
    return original


_GENERIC_SCENE_MARKERS = (
    "erfüllt die redaktionelle rolle",
    "freigegebenen aussagen",
    "generic visual",
    "appropriate visual",
    "passende visualisierung",
    "visual for this segment",
    "schrittweise erklären",
)


def _topic_excerpt(value: str, *, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact[:limit].rstrip(" ,;:")


def _scene_concreteness_errors(
    scenes: list[dict[str, Any]], segment_narrations: dict[str, str]
) -> list[str]:
    """Reject production placeholders while keeping creative choices model-owned."""

    errors: list[str] = []
    total_duration = sum(float(scene.get("duration", 0)) for scene in scenes)
    transition_duration = 0.0
    previous_visual: str | None = None
    seen_briefs: set[str] = set()
    for index, scene in enumerate(scenes):
        purpose = _topic_excerpt(str(scene.get("purpose", "")))
        brief = _topic_excerpt(str(scene.get("visual_brief", "")), limit=4_000)
        normalized = f"{purpose} {brief}".casefold()
        if any(marker in normalized for marker in _GENERIC_SCENE_MARKERS):
            errors.append(f"scene[{index}]: purpose or visual brief is a generic placeholder")
        if len(brief) < 35:
            errors.append(f"scene[{index}]: visual brief is too short to produce concretely")
        on_screen = [str(value).strip() for value in scene.get("on_screen_text", []) if str(value).strip()]
        if not on_screen:
            errors.append(f"scene[{index}]: at least one concrete on-screen text cue is required")
        referenced = [str(value) for value in scene.get("narration_segment_ids", [])]
        narration = " ".join(segment_narrations.get(value, "") for value in referenced)
        topic_tokens = {
            token
            for token in re.findall(r"[\wÄÖÜäöüß-]{5,}", narration.casefold())
            if token not in {"diese", "dieser", "einen", "einer", "werden", "wurde", "sowie"}
        }
        scene_tokens = set(re.findall(r"[\wÄÖÜäöüß-]{5,}", normalized.casefold()))
        if topic_tokens and not topic_tokens.intersection(scene_tokens):
            errors.append(f"scene[{index}]: visual plan is not tied to its narration topic")
        brief_key = re.sub(r"\W+", " ", brief.casefold()).strip()
        if brief_key in seen_briefs:
            errors.append(f"scene[{index}]: visual brief duplicates an earlier scene")
        seen_briefs.add(brief_key)
        visual_type = str(scene.get("visual_type", ""))
        if visual_type == "branded_transition":
            transition_duration += float(scene.get("duration", 0))
        if previous_visual == visual_type:
            errors.append(f"scene[{index}]: adjacent scenes repeat visual_type {visual_type}")
        previous_visual = visual_type
    if total_duration and transition_duration / total_duration > 0.15:
        errors.append("branded transitions exceed 15 percent of storyboard duration")
    return errors


def _assemble_storyboard_draft(request: dict[str, Any]) -> StoryboardDraft:
    try:
        full_draft = StoryboardDraft.model_validate(request["content_draft"])
        if all(not validate_scene_spec(scene) for scene in full_draft.scenes):
            return full_draft
    except Exception:
        # Keep audited full-contract fixture prompts replayable.
        pass
    content = StoryboardContentDraft.model_validate(request["content_draft"])
    expected = [str(UUID(value)) for value in request["expected_segment_ids"]]
    expected_set = set(expected)
    by_segment = {str(scene.narration_segment_id): scene for scene in content.scenes}
    if len(by_segment) != len(content.scenes) or set(by_segment) != expected_set:
        raise ValueError("semantic storyboard must contain exactly one scene per script segment")

    durations = {str(UUID(key)): float(value) for key, value in request["segment_durations"].items()}
    source_ids_by_segment = [
        list(request["allowed_source_ids"].get(segment_id, []))
        for segment_id in expected
    ]
    segment_narrations = {
        str(key): str(value)
        for key, value in request.get("segment_narrations", {}).items()
    }
    visual_plan = _diverse_visual_plan(
        source_ids_by_segment,
        [by_segment[segment_id].visual_type for segment_id in expected],
    )
    scenes = []
    for order, segment_id in enumerate(expected, start=1):
        scene = by_segment[segment_id]
        claim_ids = list(request["allowed_claim_ids"].get(segment_id, []))
        source_ids = source_ids_by_segment[order - 1]
        visual_type = visual_plan[order - 1]
        narration_excerpt = _topic_excerpt(segment_narrations.get(segment_id, ""))
        purpose = _topic_excerpt(scene.purpose)
        if any(marker in purpose.casefold() for marker in _GENERIC_SCENE_MARKERS):
            purpose = f"Macht diesen konkreten Erklärschritt sichtbar: {narration_excerpt}"
        on_screen_text = [str(value).strip() for value in scene.on_screen_text if str(value).strip()]
        if not on_screen_text and narration_excerpt:
            on_screen_text = [narration_excerpt[:100]]
        visual_brief = _diverse_visual_brief(
            visual_type,
            purpose,
            on_screen_text,
            scene.visual_brief,
        )
        if len(visual_brief.strip()) < 35 or any(
            marker in visual_brief.casefold() for marker in _GENERIC_SCENE_MARKERS
        ):
            visual_brief = (
                f"Konkrete {visual_type}-Darstellung dieses Narrationsschritts, ohne neue "
                f"Fakten oder erfundene Zahlen: {narration_excerpt}"
            )
        scenes.append(
            {
                "scene_id": str(uuid5(NAMESPACE_URL, f"tubefactory:scene:{segment_id}")),
                "order": order,
                "purpose": purpose,
                "narration_segment_ids": [segment_id],
                "claim_ids": claim_ids,
                "duration": durations[segment_id],
                "visual_type": visual_type,
                "visual_brief": visual_brief,
                "on_screen_text": on_screen_text,
                "citation_style": "Kurzer Quellenhinweis im Bild; vollständige Quelle in der Beschreibung.",
                "source_ids": source_ids,
                "asset_requests": [],
                "transition": "Ruhiger, klarer Schnitt zur nächsten Erklärstufe.",
                "music_sfx_policy": "Leise, sachlich und ohne Signalwirkung auf die Belegstärke.",
                "synthetic_media_flag": visual_type in {"comfyui_image", "comfyui_video"},
                "accessibility_notes": "Hoher Kontrast; Bildinhalt wird durch Narration oder Bildschirmtext erklärt.",
            }
        )
    return StoryboardDraft.model_validate({"scenes": scenes})


@activity.defn(name="assemble-storyboard-draft")
async def assemble_storyboard_draft(request: dict[str, Any]) -> dict[str, Any]:
    try:
        draft = _assemble_storyboard_draft(request)
    except Exception as exc:
        raise ApplicationError(
            f"semantic storyboard output could not be assembled: {str(exc)[:1000]}",
            non_retryable=True,
        ) from exc
    return {"draft": draft.model_dump(mode="json")}


@activity.defn(name="load-storyboard-generation-context")
async def load_storyboard_generation_context(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    script_id = UUID(str(request["script_id"]))
    script_version_id = UUID(str(request["script_version_id"]))
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        script = await connection.fetchrow(
            """SELECT s.id,s.status,s.current_version_id,sv.id AS script_version_id,
                      s.source_kind,s.production_brief_id,
                      sv.version_number,sv.status AS version_status,sv.title,sv.content_hash,
                      sv.coverage_percent,sv.total_duration_seconds,
                      sv.verification_report,pb.parse_report AS production_parse_report,
                      cp.id AS channel_profile_id,cp.name AS channel_name,
                      cp.editorial_rules AS channel_editorial_rules
               FROM scripts s
               JOIN script_versions sv ON sv.id=s.current_version_id
               LEFT JOIN research_dossiers d ON d.id=s.research_dossier_id
               LEFT JOIN opportunities o ON o.id=d.opportunity_id
               LEFT JOIN subject_profiles sp ON sp.id=o.subject_profile_id
               LEFT JOIN production_briefs pb ON pb.id=s.production_brief_id
               JOIN channel_profiles cp ON cp.id=COALESCE(sp.channel_profile_id,pb.channel_profile_id)
               WHERE s.id=$1 AND s.deleted_at IS NULL""",
            script_id,
        )
        if script is None or script["script_version_id"] != script_version_id:
            raise ApplicationError(
                "storyboard generation requires the exact current script version",
                non_retryable=True,
            )
        if script["status"] != "approved" or script["version_status"] != "verified":
            raise ApplicationError(
                "storyboard generation requires an approved script version",
                non_retryable=True,
            )
        approval = await connection.fetchrow(
            """SELECT id,target_version,target_hash FROM approvals
               WHERE target_type='script_version' AND target_id=$1 AND decision='approved'
               ORDER BY created_at DESC LIMIT 1""",
            script_version_id,
        )
        if (
            approval is None
            or approval["target_version"] != script["version_number"]
            or approval["target_hash"] != script["content_hash"]
        ):
            raise ApplicationError(
                "script approval does not match the current immutable version and hash",
                non_retryable=True,
            )
        segment_rows = await connection.fetch(
            """SELECT id,segment_key,segment_order,segment_type,narration,
                      presentation_purpose,duration_seconds,citation_display,annotations,
                      locked,content_hash
               FROM script_segments WHERE script_version_id=$1 ORDER BY segment_order""",
            script_version_id,
        )
        segments: list[dict[str, Any]] = []
        allowed_claim_ids: dict[str, set[str]] = {}
        allowed_source_ids: dict[str, set[str]] = {}
        for row in segment_rows:
            links = await connection.fetch(
                """SELECT DISTINCT sc.claim_id,ss.source_document_id
                   FROM segment_claims sc
                   LEFT JOIN evidence_excerpts ee ON ee.id=sc.evidence_excerpt_id
                   LEFT JOIN source_snapshots ss ON ss.id=ee.source_snapshot_id
                   WHERE sc.script_segment_id=$1""",
                row["id"],
            )
            segment_id = str(row["id"])
            claim_ids = {str(item["claim_id"]) for item in links}
            source_ids = {
                str(item["source_document_id"])
                for item in links
                if item["source_document_id"] is not None
            }
            allowed_claim_ids[segment_id] = claim_ids
            allowed_source_ids[segment_id] = source_ids
            annotations = json.loads(row["annotations"]) if isinstance(row["annotations"], str) else row["annotations"]
            enriched = [
                {
                    **item,
                    "source_ids": sorted(source_ids)
                    if set(item.get("claim_ids", [])) & claim_ids
                    else [],
                }
                for item in annotations
            ]
            segments.append(
                {
                    "id": segment_id,
                    "segment_key": row["segment_key"],
                    "segment_order": row["segment_order"],
                    "segment_type": row["segment_type"],
                    "narration": row["narration"],
                    "presentation_purpose": row["presentation_purpose"],
                    "duration_seconds": row["duration_seconds"],
                    "citation_display": row["citation_display"],
                    "annotations": enriched,
                    "locked": row["locked"],
                    "content_hash": row["content_hash"],
                }
            )
        if not segments:
            raise ApplicationError("approved script has no immutable segments", non_retryable=True)
        direct_storyboard_scenes: list[dict[str, Any]] = []
        if script["source_kind"] == "direct_scripted_video":
            report = script["verification_report"]
            if isinstance(report, str):
                report = json.loads(report)
            parse_report = script["production_parse_report"]
            if isinstance(parse_report, str):
                parse_report = json.loads(parse_report)
            templates = (
                (report or {}).get("direct_storyboard", {}).get("scenes")
                or (parse_report or {}).get("direct_storyboard", {}).get("scenes")
                or []
            )
            segment_by_key = {item["segment_key"]: item for item in segments}
            for template in templates:
                segment = segment_by_key.get(str(template.get("segment_key")))
                if segment is None:
                    raise ApplicationError(
                        "direct storyboard template references an unknown script segment",
                        non_retryable=True,
                    )
                scene_key = str(template.get("scene_key") or template.get("segment_key"))
                direct_storyboard_scenes.append(
                    {
                        "scene_id": str(uuid5(NAMESPACE_URL, f"{script_version_id}:{scene_key}")),
                        "order": int(template["order"]),
                        "purpose": str(template["purpose"]),
                        "narration_segment_ids": [segment["id"]],
                        "claim_ids": [],
                        "duration": float(segment["duration_seconds"]),
                        "visual_type": str(template["visual_type"]),
                        "visual_brief": str(template["visual_brief"]),
                        "on_screen_text": [str(value) for value in template.get("on_screen_text", [])],
                        "citation_style": str(template["citation_style"]),
                        "source_ids": [],
                        "asset_requests": list(template.get("asset_requests", [])),
                        "transition": str(template["transition"]),
                        "music_sfx_policy": str(template["music_sfx_policy"]),
                        "synthetic_media_flag": bool(template["synthetic_media_flag"]),
                        "accessibility_notes": str(template["accessibility_notes"]),
                    }
                )
            if not direct_storyboard_scenes:
                raise ApplicationError(
                    "direct scripted-video script has no imported storyboard templates",
                    non_retryable=True,
                )
        routes = await load_task_routes(connection, "storyboard")
        channel_workflow = channel_workflow_context(script["channel_editorial_rules"])
        return {
            "script_id": str(script_id),
            "script_version_id": str(script_version_id),
            "script_version_number": script["version_number"],
            "script_content_hash": script["content_hash"],
            "actor_id": str(request["actor_id"]),
            "correlation_id": request["correlation_id"],
            "routes": routes,
            "channel_workflow": channel_workflow,
            "structured_inputs": {
                "script_id": str(script_id),
                "script_version_id": str(script_version_id),
                "title": script["title"],
                "channel": {
                    "id": str(script["channel_profile_id"]),
                    "name": script["channel_name"],
                    "workflow_key": channel_workflow["key"],
                    "workflow_version": channel_workflow["version"],
                },
                "coverage_percent": script["coverage_percent"],
                "total_duration_seconds": script["total_duration_seconds"],
                "segments": segments,
            },
            "expected_segment_ids": [item["id"] for item in segments],
            "segment_durations": {item["id"]: item["duration_seconds"] for item in segments},
            "segment_narrations": {item["id"]: item["narration"] for item in segments},
            "allowed_claim_ids": {key: sorted(value) for key, value in allowed_claim_ids.items()},
            "allowed_source_ids": {key: sorted(value) for key, value in allowed_source_ids.items()},
            "source_kind": script["source_kind"],
            "evidence_required": script["source_kind"] != "direct_scripted_video",
            "direct_storyboard_scenes": direct_storyboard_scenes,
        }
    finally:
        await connection.close()


@activity.defn(name="load-scene-alternative-context")
async def load_scene_alternative_context(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    storyboard_id = UUID(str(request["storyboard_id"]))
    scene_id = UUID(str(request["scene_id"]))
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        row = await connection.fetchrow(
            """SELECT sb.id AS storyboard_id,sb.script_id,sb.current_version_id,sb.status,
                      sbv.version_number AS storyboard_version,sbv.script_version_id,
                      sv.title,sv.content_hash AS script_content_hash,
                      sv.coverage_percent,sv.total_duration_seconds,
                      s.source_kind,
                      sc.current_version_id AS scene_version_id,sc.locked,
                      scv.version_number AS scene_version,scv.scene_order,
                      scv.scene_spec,scv.content_hash AS scene_content_hash,
                      cp.id AS channel_profile_id,cp.name AS channel_name,
                      cp.editorial_rules AS channel_editorial_rules
               FROM storyboards sb
               JOIN storyboard_versions sbv ON sbv.id=sb.current_version_id
               JOIN script_versions sv ON sv.id=sbv.script_version_id
               JOIN scripts s ON s.id=sb.script_id
               LEFT JOIN research_dossiers d ON d.id=s.research_dossier_id
               LEFT JOIN opportunities o ON o.id=d.opportunity_id
               LEFT JOIN subject_profiles sp ON sp.id=o.subject_profile_id
               LEFT JOIN production_briefs pb ON pb.id=s.production_brief_id
               JOIN channel_profiles cp ON cp.id=COALESCE(sp.channel_profile_id,pb.channel_profile_id)
               JOIN scenes sc ON sc.storyboard_id=sb.id
               JOIN scene_versions scv ON scv.id=sc.current_version_id
               WHERE sb.id=$1 AND sc.id=$2 AND sb.deleted_at IS NULL
                 AND sc.deleted_at IS NULL""",
            storyboard_id,
            scene_id,
        )
        if row is None:
            raise ApplicationError("storyboard scene does not exist", non_retryable=True)
        if (
            row["storyboard_version"] != int(request["expected_storyboard_version"])
            or row["scene_version"] != int(request["expected_scene_version"])
        ):
            raise ApplicationError(
                "scene or storyboard changed before alternative generation",
                non_retryable=True,
            )
        if row["locked"]:
            raise ApplicationError(
                "locked scenes cannot generate alternatives", non_retryable=True
            )
        script_approval = await connection.fetchrow(
            """SELECT id FROM approvals
               WHERE target_type='script_version' AND target_id=$1
                 AND target_version=(SELECT version_number FROM script_versions WHERE id=$1)
                 AND target_hash=(SELECT content_hash FROM script_versions WHERE id=$1)
                 AND decision='approved' ORDER BY created_at DESC LIMIT 1""",
            row["script_version_id"],
        )
        if script_approval is None:
            raise ApplicationError(
                "storyboard script version has no exact approval", non_retryable=True
            )
        current_rows = await connection.fetch(
            """SELECT sc.id,scv.scene_spec FROM scenes sc
               JOIN scene_versions scv ON scv.id=sc.current_version_id
               WHERE sc.storyboard_id=$1 AND sc.deleted_at IS NULL
               ORDER BY scv.scene_order""",
            storyboard_id,
        )
        segment_rows = await connection.fetch(
            """SELECT id,segment_key,segment_order,segment_type,narration,
                      presentation_purpose,duration_seconds,citation_display,annotations,
                      locked,content_hash
               FROM script_segments WHERE script_version_id=$1 ORDER BY segment_order""",
            row["script_version_id"],
        )
        segments: list[dict[str, Any]] = []
        allowed_claim_ids: dict[str, set[str]] = {}
        allowed_source_ids: dict[str, set[str]] = {}
        for segment in segment_rows:
            links = await connection.fetch(
                """SELECT DISTINCT sc.claim_id,ss.source_document_id
                   FROM segment_claims sc
                   LEFT JOIN evidence_excerpts ee ON ee.id=sc.evidence_excerpt_id
                   LEFT JOIN source_snapshots ss ON ss.id=ee.source_snapshot_id
                   WHERE sc.script_segment_id=$1""",
                segment["id"],
            )
            segment_id = str(segment["id"])
            claim_ids = {str(item["claim_id"]) for item in links}
            source_ids = {
                str(item["source_document_id"])
                for item in links
                if item["source_document_id"] is not None
            }
            allowed_claim_ids[segment_id] = claim_ids
            allowed_source_ids[segment_id] = source_ids
            annotations = (
                json.loads(segment["annotations"])
                if isinstance(segment["annotations"], str)
                else segment["annotations"]
            )
            segments.append(
                {
                    "id": segment_id,
                    "segment_key": segment["segment_key"],
                    "segment_order": segment["segment_order"],
                    "segment_type": segment["segment_type"],
                    "narration": segment["narration"],
                    "presentation_purpose": segment["presentation_purpose"],
                    "duration_seconds": segment["duration_seconds"],
                    "citation_display": segment["citation_display"],
                    "annotations": [
                        {
                            **item,
                            "source_ids": sorted(source_ids)
                            if set(item.get("claim_ids", [])) & claim_ids
                            else [],
                        }
                        for item in annotations
                    ],
                    "locked": segment["locked"],
                    "content_hash": segment["content_hash"],
                }
            )
        routes = await load_task_routes(connection, "storyboard")
        channel_workflow = channel_workflow_context(row["channel_editorial_rules"])
        return {
            "storyboard_id": str(storyboard_id),
            "storyboard_version_id": str(row["current_version_id"]),
            "storyboard_version": row["storyboard_version"],
            "scene_id": str(scene_id),
            "scene_version_id": str(row["scene_version_id"]),
            "scene_version": row["scene_version"],
            "scene_order": row["scene_order"],
            "base_scene_hash": row["scene_content_hash"],
            "actor_id": str(request["actor_id"]),
            "correlation_id": request["correlation_id"],
            "instruction": str(request["instruction"]),
            "routes": routes,
            "channel_workflow": channel_workflow,
            "structured_inputs": {
                "script_id": str(row["script_id"]),
                "script_version_id": str(row["script_version_id"]),
                "title": row["title"],
                "channel": {
                    "id": str(row["channel_profile_id"]),
                    "name": row["channel_name"],
                    "workflow_key": channel_workflow["key"],
                    "workflow_version": channel_workflow["version"],
                },
                "coverage_percent": row["coverage_percent"],
                "total_duration_seconds": row["total_duration_seconds"],
                "segments": segments,
            },
            "current_scenes": [
                item["scene_spec"]
                if not isinstance(item["scene_spec"], str)
                else json.loads(item["scene_spec"])
                for item in current_rows
            ],
            "expected_segment_ids": [item["id"] for item in segments],
            "allowed_claim_ids": {
                key: sorted(value) for key, value in allowed_claim_ids.items()
            },
            "allowed_source_ids": {
                key: sorted(value) for key, value in allowed_source_ids.items()
            },
        }
    finally:
        await connection.close()


@activity.defn(name="validate-scene-alternative")
async def validate_scene_alternative(request: dict[str, Any]) -> dict[str, Any]:
    try:
        generated = StoryboardDraft.model_validate(request["generated_draft"])
    except Exception as exc:
        raise ApplicationError(
            f"scene alternative failed the strict storyboard contract: {str(exc)[:1000]}",
            non_retryable=True,
        ) from exc
    matches = [
        dict(item) for item in generated.scenes if item.get("order") == request["scene_order"]
    ]
    if not matches:
        try:
            semantic = StoryboardContentDraft.model_validate(request["generated_draft"])
            base = next(
                dict(item)
                for item in request["current_scenes"]
                if item.get("scene_id") == request["scene_id"]
            )
            target_segments = {
                str(value) for value in base.get("narration_segment_ids", [])
            }
            semantic_matches = [
                scene
                for scene in semantic.scenes
                if str(scene.narration_segment_id) in target_segments
            ]
            if len(semantic_matches) == 1:
                scene = semantic_matches[0]
                visual_type = scene.visual_type
                if (
                    visual_type in {"citation_card", "chart", "source_screenshot"}
                    and not base.get("source_ids")
                ):
                    visual_type = "diagram"
                base.update(
                    {
                        "purpose": scene.purpose,
                        "visual_type": visual_type,
                        "visual_brief": scene.visual_brief,
                        "on_screen_text": scene.on_screen_text,
                        "synthetic_media_flag": visual_type
                        in {"comfyui_image", "comfyui_video"},
                    }
                )
                matches = [base]
        except (StopIteration, ValueError):
            matches = []
    if len(matches) != 1:
        raise ApplicationError(
            "scene alternative output did not contain exactly one target order",
            non_retryable=True,
        )
    candidate = matches[0]
    requested_type = re.search(
        r"\bvisual_type\s+(?:(?:exakt|exactly|exact)\s+)?[:=]?\s*([a-z_]+)",
        str(request.get("instruction", "")).casefold(),
    )
    allowed_types = {
        "title_card", "citation_card", "text", "diagram", "timeline", "chart",
        "source_screenshot", "licensed_media", "comfyui_image", "comfyui_video",
        "waveform", "branded_transition",
    }
    if requested_type and requested_type.group(1) in allowed_types:
        visual_type = requested_type.group(1)
        if (
            visual_type in {"citation_card", "chart", "source_screenshot"}
            and not candidate.get("source_ids")
        ):
            raise ApplicationError(
                f"requested visual_type {visual_type} requires an allowed source",
                non_retryable=True,
            )
        candidate["visual_type"] = visual_type
        candidate["visual_brief"] = _diverse_visual_brief(
            visual_type,
            str(candidate.get("purpose", "")),
            [str(value) for value in candidate.get("on_screen_text", [])],
            str(candidate.get("visual_brief", "")),
        )
        candidate["synthetic_media_flag"] = visual_type in {
            "comfyui_image", "comfyui_video",
        }
    candidate["scene_id"] = request["scene_id"]
    errors = list(validate_scene_spec(candidate))
    combined = [
        candidate if item.get("scene_id") == request["scene_id"] else item
        for item in request["current_scenes"]
    ]
    errors.extend(
        validate_storyboard(combined, expected_segment_ids=request["expected_segment_ids"])
    )
    for index, scene in enumerate(combined):
        referenced = set(scene.get("narration_segment_ids", []))
        allowed_claims = {
            claim_id
            for segment_id in referenced
            for claim_id in request["allowed_claim_ids"].get(segment_id, [])
        }
        allowed_sources = {
            source_id
            for segment_id in referenced
            for source_id in request["allowed_source_ids"].get(segment_id, [])
        }
        claims = set(scene.get("claim_ids", []))
        sources = set(scene.get("source_ids", []))
        if not claims <= allowed_claims:
            errors.append(f"scene[{index}]: claim is not linked to its narration segment")
        if not sources <= allowed_sources:
            errors.append(f"scene[{index}]: source is not linked to its narration segment")
        if claims and not sources:
            errors.append(f"scene[{index}]: claim-bearing scene must expose a source")
    content_hash = hashlib.sha256(_canonical(candidate).encode()).hexdigest()
    if content_hash == request["base_scene_hash"]:
        errors.append("generated alternative is identical to the current scene")
    if errors:
        raise ApplicationError(
            "scene alternative policy rejected the model output: " + "; ".join(errors[:20]),
            non_retryable=True,
        )
    return {"scene_spec": candidate, "content_hash": content_hash}


@activity.defn(name="persist-scene-alternative")
async def persist_scene_alternative(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    workflow_id = request["workflow_id"]
    storyboard_id = UUID(request["storyboard_id"])
    scene_id = UUID(request["scene_id"])
    base_scene_version_id = UUID(request["scene_version_id"])
    actor_id = UUID(request["actor_id"])
    now = datetime.now(timezone.utc)
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"scene-alternative:{scene_id}"
            )
            existing = await connection.fetchrow(
                """SELECT id,alternative_number,content_hash FROM scene_alternatives
                   WHERE workflow_id=$1""",
                workflow_id,
            )
            if existing:
                return {
                    "scene_alternative_id": str(existing["id"]),
                    "alternative_number": existing["alternative_number"],
                    "content_hash": existing["content_hash"],
                    "idempotent_replay": True,
                }
            current = await connection.fetchrow(
                """SELECT sb.current_version_id AS storyboard_current_version_id,
                          sc.current_version_id AS scene_current_version_id,sc.locked
                   FROM storyboards sb JOIN scenes sc ON sc.storyboard_id=sb.id
                   WHERE sb.id=$1 AND sc.id=$2 FOR UPDATE OF sb,sc""",
                storyboard_id,
                scene_id,
            )
            if (
                current is None
                or str(current["storyboard_current_version_id"])
                != request["storyboard_version_id"]
            ):
                raise ApplicationError(
                    "storyboard changed before alternative persistence", non_retryable=True
                )
            if current["scene_current_version_id"] != base_scene_version_id or current["locked"]:
                raise ApplicationError(
                    "scene changed or was locked before alternative persistence",
                    non_retryable=True,
                )
            alternative_number = 1 + int(
                await connection.fetchval(
                    """SELECT count(*) FROM scene_alternatives
                       WHERE scene_id=$1 AND base_scene_version_id=$2""",
                    scene_id,
                    base_scene_version_id,
                )
            )
            alternative_id = uuid4()
            await connection.execute(
                """INSERT INTO scene_alternatives
                   (id,scene_id,base_scene_version_id,alternative_number,scene_spec,
                    content_hash,model_id,prompt_template_id,workflow_id,instruction,
                    correlation_id,created_by,created_at)
                   VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12,$13)""",
                alternative_id,
                scene_id,
                base_scene_version_id,
                alternative_number,
                json.dumps(request["scene_spec"]),
                request["content_hash"],
                UUID(request["model_id"]),
                UUID(request["prompt_id"]),
                workflow_id,
                request["instruction"],
                request["correlation_id"],
                actor_id,
                now,
            )
            await append_audit(
                connection,
                action="scene.alternative_generated",
                actor_id=actor_id,
                target_type="scene_alternative",
                target_id=str(alternative_id),
                correlation_id=request["correlation_id"],
                context={
                    "storyboard_id": str(storyboard_id),
                    "scene_id": str(scene_id),
                    "base_scene_version_id": str(base_scene_version_id),
                    "alternative_number": alternative_number,
                    "content_hash": request["content_hash"],
                    "instruction": request["instruction"],
                },
            )
            return {
                "scene_alternative_id": str(alternative_id),
                "alternative_number": alternative_number,
                "content_hash": request["content_hash"],
                "idempotent_replay": False,
            }
    finally:
        await connection.close()


@activity.defn(name="validate-storyboard-draft")
async def validate_storyboard_activity(request: dict[str, Any]) -> dict[str, Any]:
    try:
        draft = StoryboardDraft.model_validate(request["draft"])
    except Exception as exc:
        raise ApplicationError(
            f"storyboard output failed its strict wrapper contract: {str(exc)[:1000]}",
            non_retryable=True,
        ) from exc
    errors = list(
        validate_storyboard(
            draft.scenes,
            expected_segment_ids=request["expected_segment_ids"],
        )
    )
    errors.extend(
        _scene_concreteness_errors(
            draft.scenes,
            {
                str(key): str(value)
                for key, value in request.get("segment_narrations", {}).items()
            },
        )
    )
    known_segments = set(request["expected_segment_ids"])
    for index, scene in enumerate(draft.scenes):
        referenced = set(scene.get("narration_segment_ids", []))
        if not referenced or not referenced <= known_segments:
            errors.append(f"scene[{index}]: narration references an unknown script segment")
            continue
        allowed_claims = {
            claim_id
            for segment_id in referenced
            for claim_id in request["allowed_claim_ids"].get(segment_id, [])
        }
        allowed_sources = {
            source_id
            for segment_id in referenced
            for source_id in request["allowed_source_ids"].get(segment_id, [])
        }
        claims = set(scene.get("claim_ids", []))
        sources = set(scene.get("source_ids", []))
        if not claims <= allowed_claims:
            errors.append(f"scene[{index}]: claim is not linked to its narration segment")
        if not sources <= allowed_sources:
            errors.append(f"scene[{index}]: source is not linked to its narration segment")
        if claims and not sources:
            errors.append(f"scene[{index}]: claim-bearing scene must expose a source")
    if errors:
        raise ApplicationError(
            "storyboard policy rejected the model output: " + "; ".join(errors[:20]),
            non_retryable=True,
        )
    return {"scenes": draft.scenes}


@activity.defn(name="persist-storyboard-result")
async def persist_storyboard_result(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    draft = StoryboardDraft.model_validate({"scenes": request["scenes"]})
    workflow_id = request["workflow_id"]
    script_id = UUID(request["script_id"])
    script_version_id = UUID(request["script_version_id"])
    actor_id = UUID(request["actor_id"])
    now = datetime.now(timezone.utc)
    content_hash = hashlib.sha256(_canonical(draft.model_dump(mode="json")).encode()).hexdigest()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"storyboard:{script_id}"
            )
            existing = await connection.fetchrow(
                """SELECT sb.id AS storyboard_id,sbv.id AS storyboard_version_id,
                          sbv.version_number,sb.status,sbv.content_hash
                   FROM storyboard_versions sbv JOIN storyboards sb ON sb.id=sbv.storyboard_id
                   WHERE sbv.workflow_id=$1""",
                workflow_id,
            )
            if existing:
                return {
                    **{key: str(existing[key]) for key in ("storyboard_id", "storyboard_version_id")},
                    "version_number": existing["version_number"],
                    "status": existing["status"],
                    "content_hash": existing["content_hash"],
                    "idempotent_replay": True,
                }
            if await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM storyboards WHERE script_id=$1)", script_id
            ):
                raise ApplicationError(
                    "this script already has a storyboard; use versioned scene editing or regeneration",
                    non_retryable=True,
                )
            script = await connection.fetchrow(
                """SELECT s.status,s.current_version_id,sv.status AS version_status,
                          sv.version_number,sv.content_hash
                   FROM scripts s JOIN script_versions sv ON sv.id=s.current_version_id
                   WHERE s.id=$1 FOR SHARE""",
                script_id,
            )
            if (
                script is None
                or script["status"] != "approved"
                or script["version_status"] != "verified"
                or script["current_version_id"] != script_version_id
                or script["version_number"] != request["script_version_number"]
                or script["content_hash"] != request["script_content_hash"]
            ):
                raise ApplicationError(
                    "approved script changed before storyboard persistence", non_retryable=True
                )
            storyboard_id = uuid4()
            storyboard_version_id = uuid4()
            await connection.execute(
                """INSERT INTO storyboards
                   (id,version,created_at,updated_at,deleted_at,script_id,status,
                    current_version_id,created_by)
                   VALUES($1,1,$2,$2,NULL,$3,'in_review',NULL,$4)""",
                storyboard_id,
                now,
                script_id,
                actor_id,
            )
            await connection.execute(
                """INSERT INTO storyboard_versions
                   (id,storyboard_id,script_version_id,version_number,status,content_hash,
                    parent_version_id,workflow_id,correlation_id,created_by,created_at)
                   VALUES($1,$2,$3,1,'in_review',$4,NULL,$5,$6,$7,$8)""",
                storyboard_version_id,
                storyboard_id,
                script_version_id,
                content_hash,
                workflow_id,
                request["correlation_id"],
                actor_id,
                now,
            )
            for scene in draft.scenes:
                scene_id = UUID(str(scene["scene_id"]))
                scene_version_id = uuid4()
                scene_hash = hashlib.sha256(_canonical(scene).encode()).hexdigest()
                await connection.execute(
                    """INSERT INTO scenes
                       (id,version,created_at,updated_at,deleted_at,storyboard_id,scene_key,
                        current_version_id,locked)
                       VALUES($1,1,$2,$2,NULL,$3,$4,NULL,false)""",
                    scene_id,
                    now,
                    storyboard_id,
                    f"scene-{scene['order']:03d}",
                )
                await connection.execute(
                    """INSERT INTO scene_versions
                       (id,scene_id,storyboard_version_id,version_number,scene_order,
                        duration_seconds,visual_type,scene_spec,content_hash,parent_version_id,
                        created_by,created_at)
                       VALUES($1,$2,$3,1,$4,$5,$6,$7::jsonb,$8,NULL,$9,$10)""",
                    scene_version_id,
                    scene_id,
                    storyboard_version_id,
                    scene["order"],
                    scene["duration"],
                    scene["visual_type"],
                    json.dumps(scene),
                    scene_hash,
                    actor_id,
                    now,
                )
                await connection.execute(
                    "UPDATE scenes SET current_version_id=$1 WHERE id=$2",
                    scene_version_id,
                    scene_id,
                )
            await connection.execute(
                "UPDATE storyboards SET current_version_id=$1 WHERE id=$2",
                storyboard_version_id,
                storyboard_id,
            )
            await connection.execute(
                """INSERT INTO workflow_transitions
                   (id,aggregate_type,aggregate_id,from_stage,to_stage,reason,actor_id,
                    correlation_id,occurred_at)
                   VALUES($1,'storyboard',$2,NULL,'IN_REVIEW',$3,$4,$5,$6)""",
                uuid4(),
                storyboard_id,
                "strict SceneSpec validation completed for the exact approved script version",
                actor_id,
                request["correlation_id"],
                now,
            )
            await append_audit(
                connection,
                action="storyboard.generated",
                actor_id=actor_id,
                target_type="storyboard_version",
                target_id=str(storyboard_version_id),
                correlation_id=request["correlation_id"],
                context={
                    "storyboard_id": str(storyboard_id),
                    "script_version_id": str(script_version_id),
                    "version": 1,
                    "content_hash": content_hash,
                    "scene_count": len(draft.scenes),
                },
            )
            return {
                "storyboard_id": str(storyboard_id),
                "storyboard_version_id": str(storyboard_version_id),
                "version_number": 1,
                "status": "in_review",
                "content_hash": content_hash,
                "scene_count": len(draft.scenes),
                "idempotent_replay": False,
            }
    finally:
        await connection.close()
