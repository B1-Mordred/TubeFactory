from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from temporalio import activity
from temporalio.exceptions import ApplicationError

from editorial_core.editorial import validate_scene_spec, validate_storyboard
from editorial_worker.config import Settings
from editorial_worker.contracts import StoryboardDraft
from editorial_worker.db import append_audit
from editorial_worker.model_activities import load_task_routes


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@activity.defn(name="load-storyboard-generation-context")
async def load_storyboard_generation_context(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    script_id = UUID(str(request["script_id"]))
    script_version_id = UUID(str(request["script_version_id"]))
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        script = await connection.fetchrow(
            """SELECT s.id,s.status,s.current_version_id,sv.id AS script_version_id,
                      sv.version_number,sv.status AS version_status,sv.title,sv.content_hash,
                      sv.coverage_percent,sv.total_duration_seconds
               FROM scripts s JOIN script_versions sv ON sv.id=s.current_version_id
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
        routes = await load_task_routes(connection, "storyboard")
        return {
            "script_id": str(script_id),
            "script_version_id": str(script_version_id),
            "script_version_number": script["version_number"],
            "script_content_hash": script["content_hash"],
            "actor_id": str(request["actor_id"]),
            "correlation_id": request["correlation_id"],
            "routes": routes,
            "structured_inputs": {
                "script_id": str(script_id),
                "script_version_id": str(script_version_id),
                "title": script["title"],
                "coverage_percent": script["coverage_percent"],
                "total_duration_seconds": script["total_duration_seconds"],
                "segments": segments,
            },
            "expected_segment_ids": [item["id"] for item in segments],
            "allowed_claim_ids": {key: sorted(value) for key, value in allowed_claim_ids.items()},
            "allowed_source_ids": {key: sorted(value) for key, value in allowed_source_ids.items()},
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
                      sc.current_version_id AS scene_version_id,sc.locked,
                      scv.version_number AS scene_version,scv.scene_order,
                      scv.scene_spec,scv.content_hash AS scene_content_hash
               FROM storyboards sb
               JOIN storyboard_versions sbv ON sbv.id=sb.current_version_id
               JOIN script_versions sv ON sv.id=sbv.script_version_id
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
            "structured_inputs": {
                "script_id": str(row["script_id"]),
                "script_version_id": str(row["script_version_id"]),
                "title": row["title"],
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
    if len(matches) != 1:
        raise ApplicationError(
            "scene alternative output did not contain exactly one target order",
            non_retryable=True,
        )
    candidate = matches[0]
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
