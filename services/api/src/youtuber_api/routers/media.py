from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission
from editorial_core.media import TypedWorkflowInput, validate_comfy_api_workflow
from editorial_core.operating_policy import evaluate_operating_policy
from youtuber_api.audit import append_audit
from youtuber_api.config import get_settings
from youtuber_api.db import get_session
from youtuber_api.editorial_gateway import TemporalEditorialGateway
from youtuber_api.models import (
    ApprovalModel,
    ChannelProfileModel,
    ComfyWorkflowHeadModel,
    ComfyWorkflowVersionModel,
    MediaAssetModel,
    MediaProductionModel,
    OpportunityModel,
    ProductionBriefModel,
    ProductionManifestModel,
    ProductionRenderModel,
    PublishMetadataVersionModel,
    QAFindingModel,
    QAOverrideModel,
    QAReportModel,
    SceneVersionModel,
    ScriptModel,
    ScriptSegmentModel,
    ScriptVersionModel,
    StoryboardModel,
    StoryboardVersionModel,
    SubjectProfileModel,
    UserModel,
    VoiceProfileHeadModel,
    VoiceProfileVersionModel,
    WorkflowControlRecordModel,
)
from youtuber_api.schemas import (
    AutomaticContinuation,
    ComfyWorkflowView,
    ComfyWorkflowWrite,
    MediaAssetView,
    MediaProductionStart,
    MediaProductionView,
    MediaRegenerationStart,
    PublishMetadataWrite,
    QAFindingView,
    QAOverrideWrite,
    RenderApprovalWrite,
    ResearchWorkflowView,
    VoiceProfileView,
    VoiceProfileWrite,
)
from youtuber_api.security import require


router = APIRouter(prefix="/media", tags=["media-production"])
Viewer = Annotated[UserModel, Depends(require(Permission.VIEW))]
Operator = Annotated[UserModel, Depends(require(Permission.OPERATE_WORKFLOWS))]
Reviewer = Annotated[UserModel, Depends(require(Permission.REVIEW))]
ProviderAdmin = Annotated[UserModel, Depends(require(Permission.MANAGE_PROVIDERS))]


def parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Return an inclusive byte range, rejecting malformed or multi-range requests."""
    if value is None:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or size <= 0:
        raise ValueError("invalid range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("invalid range")
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid range")
        return max(0, size - suffix), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return start, min(end, size - 1)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


async def _direct_storyboard_placeholders(
    session: AsyncSession, storyboard_version_id: UUID
) -> list[str]:
    source_kind = await session.scalar(
        select(ScriptModel.source_kind)
        .join(StoryboardModel, StoryboardModel.script_id == ScriptModel.id)
        .join(StoryboardVersionModel, StoryboardVersionModel.storyboard_id == StoryboardModel.id)
        .where(StoryboardVersionModel.id == storyboard_version_id)
    )
    if source_kind != "direct_scripted_video":
        return []
    specs = list(
        await session.scalars(
            select(SceneVersionModel.scene_spec).where(
                SceneVersionModel.storyboard_version_id == storyboard_version_id
            )
        )
    )
    return sorted(
        set(re.findall(r"\[[A-ZÄÖÜ0-9][A-ZÄÖÜ0-9_.:-]{1,80}\]", _canonical(specs).decode()))
    )


async def _commit(session: AsyncSession, detail: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=detail) from exc


def _workflow_view(item: ComfyWorkflowVersionModel, active_id: UUID | None) -> ComfyWorkflowView:
    return ComfyWorkflowView(
        id=item.id, workflow_key=item.workflow_key, version_number=item.version_number,
        purpose=item.purpose, api_workflow=item.api_workflow, content_hash=item.content_hash,
        required_nodes=item.required_nodes, required_models=item.required_models,
        typed_inputs=item.typed_inputs, output_contract=item.output_contract,
        allowed_resolutions=item.allowed_resolutions, allowed_durations=item.allowed_durations,
        approval_state=item.approval_state, approved_by=item.approved_by,
        approved_at=item.approved_at, created_by=item.created_by, created_at=item.created_at,
        comment=item.comment, active=item.id == active_id,
    )


@router.get("/comfy-workflows", response_model=list[ComfyWorkflowView])
async def list_comfy_workflows(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[ComfyWorkflowView]:
    heads = {head.workflow_key: head.active_version_id for head in await session.scalars(select(ComfyWorkflowHeadModel))}
    items = list(await session.scalars(select(ComfyWorkflowVersionModel).order_by(ComfyWorkflowVersionModel.workflow_key, ComfyWorkflowVersionModel.version_number.desc())))
    return [_workflow_view(item, heads.get(item.workflow_key)) for item in items]


@router.post("/comfy-workflows", response_model=ComfyWorkflowView, status_code=201)
async def import_comfy_workflow(
    payload: ComfyWorkflowWrite,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ComfyWorkflowView:
    specs = [TypedWorkflowInput(**item.model_dump()) for item in payload.typed_inputs]
    errors = validate_comfy_api_workflow(
        payload.api_workflow,
        typed_inputs=specs,
        allowed_node_types={item.class_type for item in payload.required_nodes},
        allowed_models={item.name for item in payload.required_models},
    )
    minimum = payload.allowed_durations.get("minimum_seconds")
    maximum = payload.allowed_durations.get("maximum_seconds")
    if minimum is None or maximum is None or not 0.1 <= minimum <= maximum <= 900:
        errors += ("allowed duration bounds must be ordered between 0.1 and 900 seconds",)
    if any(not 64 <= value.get("width", 0) <= 8192 or not 64 <= value.get("height", 0) <= 8192 for value in payload.allowed_resolutions):
        errors += ("allowed resolutions must be between 64 and 8192 pixels",)
    if errors:
        raise HTTPException(status_code=422, detail=list(errors))
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"comfy-workflow:{payload.workflow_key}"})
    maximum_version = await session.scalar(select(func.max(ComfyWorkflowVersionModel.version_number)).where(ComfyWorkflowVersionModel.workflow_key == payload.workflow_key))
    content = payload.model_dump(exclude={"approve", "comment"})
    now = datetime.now(timezone.utc)
    item = ComfyWorkflowVersionModel(
        id=uuid4(), version_number=(maximum_version or 0) + 1,
        content_hash=_digest(content), approval_state="approved" if payload.approve else "draft",
        approved_by=actor.id if payload.approve else None, approved_at=now if payload.approve else None,
        created_by=actor.id, created_at=now, comment=payload.comment,
        **{key: value for key, value in content.items() if key != "workflow_key"},
        workflow_key=payload.workflow_key,
    )
    session.add(item)
    await session.flush()
    active_id = None
    if payload.approve:
        head = await session.get(ComfyWorkflowHeadModel, payload.workflow_key, with_for_update=True)
        if head is None:
            head = ComfyWorkflowHeadModel(workflow_key=payload.workflow_key, active_version_id=item.id, updated_at=now)
            session.add(head)
        else:
            head.active_version_id, head.updated_at = item.id, now
        active_id = item.id
    await append_audit(session, action="comfy_workflow.imported", actor_id=actor.id, target_type="comfy_workflow_version", target_id=str(item.id), correlation_id=request.state.correlation_id, context={"workflow_key": item.workflow_key, "version": item.version_number, "content_hash": item.content_hash, "approved": payload.approve})
    await _commit(session, "This exact workflow version is already registered")
    return _workflow_view(item, active_id)


def _voice_view(item: VoiceProfileVersionModel, active_id: UUID | None) -> VoiceProfileView:
    return VoiceProfileView(
        profile_key=item.profile_key, provider_type=item.provider_type, endpoint=item.endpoint,
        voice_id=item.voice_id, language=item.language, engine=item.engine,
        model_version=item.model_version, delivery=item.delivery, pronunciation=item.pronunciation,
        output_settings=item.output_settings, consent=item.consent, enabled=item.enabled,
        comment=item.comment, id=item.id, version_number=item.version_number,
        content_hash=item.content_hash, created_by=item.created_by, created_at=item.created_at,
        active=item.id == active_id,
    )


@router.get("/voice-profiles", response_model=list[VoiceProfileView])
async def list_voice_profiles(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[VoiceProfileView]:
    heads = {head.profile_key: head.active_version_id for head in await session.scalars(select(VoiceProfileHeadModel))}
    items = list(await session.scalars(select(VoiceProfileVersionModel).order_by(VoiceProfileVersionModel.profile_key, VoiceProfileVersionModel.version_number.desc())))
    return [_voice_view(item, heads.get(item.profile_key)) for item in items]


@router.post("/voice-profiles", response_model=VoiceProfileView, status_code=201)
async def write_voice_profile(
    payload: VoiceProfileWrite,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> VoiceProfileView:
    if payload.provider_type != "fake" and not payload.endpoint:
        raise HTTPException(status_code=422, detail="Voicebox providers require an endpoint")
    if payload.provider_type != "fake" and payload.endpoint and not payload.endpoint.startswith(("http://", "https://", "ws://", "wss://")):
        raise HTTPException(status_code=422, detail="Voicebox endpoint must be an HTTP or WebSocket URL")
    sample_rate = payload.output_settings.get("sample_rate")
    if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 192000:
        raise HTTPException(status_code=422, detail="Output sample_rate must be between 8000 and 192000")
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"voice-profile:{payload.profile_key}"})
    maximum_version = await session.scalar(select(func.max(VoiceProfileVersionModel.version_number)).where(VoiceProfileVersionModel.profile_key == payload.profile_key))
    content = payload.model_dump(exclude={"comment"})
    now = datetime.now(timezone.utc)
    item = VoiceProfileVersionModel(
        id=uuid4(),
        version_number=(maximum_version or 0) + 1,
        content_hash=_digest(content),
        created_by=actor.id,
        created_at=now,
        comment=payload.comment,
        **content,
    )
    session.add(item)
    await session.flush()
    head = await session.get(VoiceProfileHeadModel, payload.profile_key, with_for_update=True)
    if payload.enabled:
        if head is None:
            head = VoiceProfileHeadModel(profile_key=payload.profile_key, active_version_id=item.id, updated_at=now)
            session.add(head)
        else:
            head.active_version_id, head.updated_at = item.id, now
    await append_audit(session, action="voice_profile.version_activated" if payload.enabled else "voice_profile.version_created", actor_id=actor.id, target_type="voice_profile_version", target_id=str(item.id), correlation_id=request.state.correlation_id, context={"profile_key": payload.profile_key, "version": item.version_number, "provider_type": item.provider_type, "content_hash": item.content_hash})
    await _commit(session, "This exact voice profile version is already registered")
    return _voice_view(item, item.id if payload.enabled else (head.active_version_id if head else None))


async def _production_view(session: AsyncSession, production: MediaProductionModel) -> MediaProductionView:
    assets = list(await session.scalars(select(MediaAssetModel).where(MediaAssetModel.production_id == production.id).order_by(MediaAssetModel.created_at)))
    render = await session.scalar(select(ProductionRenderModel).where(ProductionRenderModel.production_id == production.id).order_by(ProductionRenderModel.render_number.desc()).limit(1))
    manifest = await session.get(ProductionManifestModel, render.manifest_id) if render else None
    report = await session.scalar(select(QAReportModel).where(QAReportModel.render_id == render.id)) if render else None
    finding_rows: list[tuple[QAFindingModel, QAOverrideModel | None]] = []
    if report:
        finding_rows = list((await session.execute(select(QAFindingModel, QAOverrideModel).outerjoin(QAOverrideModel, QAOverrideModel.finding_id == QAFindingModel.id).where(QAFindingModel.report_id == report.id).order_by(QAFindingModel.finding_order))).all())
    approval = await session.scalar(select(ApprovalModel).where(ApprovalModel.target_type == "production_render", ApprovalModel.target_id == render.id).order_by(ApprovalModel.created_at.desc()).limit(1)) if render else None
    return MediaProductionView(
        id=production.id, storyboard_version_id=production.storyboard_version_id,
        storyboard_hash=production.storyboard_hash, workflow_id=production.workflow_id,
        render_tier=production.render_tier, state=production.state, settings=production.settings,
        correlation_id=production.correlation_id, created_at=production.created_at,
        completed_at=production.completed_at,
        assets=[MediaAssetView.model_validate(item, from_attributes=True) for item in assets],
        render=None if not render else {"id": str(render.id), "number": render.render_number, "tier": render.tier, "video_asset_id": str(render.video_asset_id), "content_hash": render.content_hash, "engine": render.render_engine, "engine_version": render.render_engine_version, "composition": render.composition, "settings": render.settings, "probe": render.probe, "created_at": render.created_at.isoformat()},
        manifest=None if not manifest else {"id": str(manifest.id), "version": manifest.manifest_version, "content_hash": manifest.content_hash, "object_key": manifest.object_key, "document": manifest.document},
        qa=None if not report else {"id": str(report.id), "verdict": report.verdict, "content_hash": report.content_hash, "metrics": report.metrics, "policy_snapshot": report.policy_snapshot},
        findings=[QAFindingView(id=item.id, code=item.code, verdict=item.verdict, message=item.message, scene_version_id=item.scene_version_id, timecode_seconds=item.timecode_seconds, details=item.details, override_policy=item.override_policy, overridden=override is not None, override_reason=override.reason if override else None) for item, override in finding_rows],
        approval=None if not approval else {"id": str(approval.id), "decision": approval.decision, "comment": approval.comment, "actor_id": str(approval.actor_id), "created_at": approval.created_at.isoformat()},
    )


@router.get("/productions", response_model=list[MediaProductionView])
async def list_productions(_: Viewer, session: Annotated[AsyncSession, Depends(get_session)]) -> list[MediaProductionView]:
    productions = list(
        await session.scalars(
            select(MediaProductionModel)
            .join(
                StoryboardVersionModel,
                StoryboardVersionModel.id == MediaProductionModel.storyboard_version_id,
            )
            .join(StoryboardModel, StoryboardModel.id == StoryboardVersionModel.storyboard_id)
            .join(ScriptModel, ScriptModel.id == StoryboardModel.script_id)
            .outerjoin(OpportunityModel, OpportunityModel.id == ScriptModel.opportunity_id)
            .where(
                StoryboardModel.deleted_at.is_(None),
                ScriptModel.deleted_at.is_(None),
                (ScriptModel.source_kind == "direct_scripted_video")
                | (OpportunityModel.deleted_at.is_(None)),
            )
            .order_by(MediaProductionModel.created_at.desc())
            .limit(100)
        )
    )
    return [await _production_view(session, item) for item in productions]


@router.get("/productions/{production_id}", response_model=MediaProductionView)
async def get_production(production_id: UUID, _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]) -> MediaProductionView:
    production = await session.get(MediaProductionModel, production_id)
    if production is None:
        raise HTTPException(status_code=404, detail="Media production not found")
    return await _production_view(session, production)


@router.post("/productions", response_model=ResearchWorkflowView, status_code=202)
async def start_production(
    payload: MediaProductionStart,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    storyboard = await session.get(StoryboardVersionModel, payload.storyboard_version_id)
    storyboard_record = await session.get(StoryboardModel, storyboard.storyboard_id) if storyboard else None
    if (
        storyboard is None
        or storyboard_record is None
        or storyboard_record.status != "approved"
        or storyboard_record.current_version_id != storyboard.id
        or storyboard.content_hash != payload.expected_storyboard_hash
    ):
        raise HTTPException(status_code=409, detail="Use the exact approved storyboard version and hash")
    approval = await session.scalar(select(ApprovalModel.id).where(ApprovalModel.target_type == "storyboard_version", ApprovalModel.target_id == storyboard.id, ApprovalModel.target_version == storyboard.version_number, ApprovalModel.target_hash == storyboard.content_hash, ApprovalModel.decision == "approved"))
    if approval is None:
        raise HTTPException(status_code=409, detail="Storyboard version has no exact-hash approval")
    unresolved = await _direct_storyboard_placeholders(session, storyboard.id)
    if unresolved:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Direct scripted-video media production is blocked until placeholders are resolved",
                "placeholder_tokens": unresolved,
            },
        )
    workflow_head = await session.get(ComfyWorkflowHeadModel, payload.workflow_key)
    voice_head = await session.get(VoiceProfileHeadModel, payload.voice_profile_key)
    workflow = await session.get(ComfyWorkflowVersionModel, workflow_head.active_version_id) if workflow_head else None
    voice = await session.get(VoiceProfileVersionModel, voice_head.active_version_id) if voice_head else None
    if workflow is None or workflow.approval_state != "approved":
        raise HTTPException(status_code=409, detail="Select an active approved ComfyUI workflow")
    if voice is None or not voice.enabled:
        raise HTTPException(status_code=409, detail="Select an active enabled voice profile")
    if {"width": payload.width, "height": payload.height} not in workflow.allowed_resolutions:
        raise HTTPException(status_code=422, detail="Resolution is not allowed by this workflow version")
    workflow_id = f"media-production-{payload.idempotency_key}"
    existing = await session.get(WorkflowControlRecordModel, workflow_id)
    workflow_payload = {**payload.model_dump(mode="json"), "workflow_id": workflow_id, "actor_id": str(actor.id), "correlation_id": request.state.correlation_id, "comfy_workflow_version_id": str(workflow.id), "voice_profile_version_id": str(voice.id)}
    gateway = TemporalEditorialGateway(request.app.state.temporal_client)
    if existing:
        comparable = {key: value for key, value in existing.request_payload.items() if key not in {"actor_id", "correlation_id"}}
        proposed = {key: value for key, value in workflow_payload.items() if key not in {"actor_id", "correlation_id"}}
        if existing.workflow_type != "media-production" or comparable != proposed:
            raise HTTPException(status_code=409, detail="Idempotency key is already used by another request")
    else:
        session.add(WorkflowControlRecordModel(workflow_id=workflow_id, workflow_type="media-production", request_payload=workflow_payload, parent_workflow_id=None, correlation_id=request.state.correlation_id, started_by=actor.id, created_at=datetime.now(timezone.utc)))
        await session.flush()
    await gateway.start("media-production", existing.request_payload if existing else workflow_payload)
    await append_audit(session, action="media_production.reconciled" if existing else "media_production.started", actor_id=actor.id, target_type="storyboard_version", target_id=str(storyboard.id), correlation_id=request.state.correlation_id, context={"workflow_id": workflow_id, "storyboard_hash": storyboard.content_hash, "render_tier": payload.render_tier, "comfy_workflow_version_id": str(workflow.id), "voice_profile_version_id": str(voice.id)})
    await session.commit()
    description = await gateway.describe(workflow_id)
    description["correlation_id"] = request.state.correlation_id
    return ResearchWorkflowView.model_validate(description)


async def _start_regeneration(
    *,
    workflow_type: str,
    production: MediaProductionModel,
    selection: dict[str, str],
    payload: MediaRegenerationStart,
    request: Request,
    actor: UserModel,
    session: AsyncSession,
) -> ResearchWorkflowView:
    settings = production.settings
    workflow_id = f"{workflow_type}-{payload.idempotency_key}"
    workflow_payload = {
        "workflow_id": workflow_id,
        "production_id": str(production.id),
        "storyboard_version_id": str(production.storyboard_version_id),
        "expected_storyboard_hash": production.storyboard_hash,
        "render_tier": production.render_tier,
        "width": int(settings["width"]),
        "height": int(settings["height"]),
        "fps": int(settings["fps"]),
        "comfy_workflow_version_id": str(settings["comfy_workflow_version_id"]),
        "voice_profile_version_id": str(settings["voice_profile_version_id"]),
        "instruction": payload.instruction,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
        **selection,
    }
    existing = await session.get(WorkflowControlRecordModel, workflow_id)
    if existing:
        comparable = {key: value for key, value in existing.request_payload.items() if key not in {"actor_id", "correlation_id"}}
        proposed = {key: value for key, value in workflow_payload.items() if key not in {"actor_id", "correlation_id"}}
        if existing.workflow_type != workflow_type or comparable != proposed:
            raise HTTPException(status_code=409, detail="Idempotency key is already used by another request")
    else:
        session.add(WorkflowControlRecordModel(workflow_id=workflow_id, workflow_type=workflow_type, request_payload=workflow_payload, parent_workflow_id=production.workflow_id, correlation_id=request.state.correlation_id, started_by=actor.id, created_at=datetime.now(timezone.utc)))
        await session.flush()
    gateway = TemporalEditorialGateway(request.app.state.temporal_client)
    await gateway.start(workflow_type, existing.request_payload if existing else workflow_payload)
    await append_audit(session, action=f"media.{workflow_type}.started", actor_id=actor.id, target_type="media_production", target_id=str(production.id), correlation_id=request.state.correlation_id, context={"workflow_id": workflow_id, **selection, "instruction": payload.instruction})
    await session.commit()
    description = await gateway.describe(workflow_id)
    description["correlation_id"] = request.state.correlation_id
    return ResearchWorkflowView.model_validate(description)


@router.post("/productions/{production_id}/scenes/{scene_version_id}/regenerate", response_model=ResearchWorkflowView, status_code=202)
async def regenerate_scene_media(
    production_id: UUID, scene_version_id: UUID, payload: MediaRegenerationStart,
    request: Request, actor: Operator, session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    production = await session.get(MediaProductionModel, production_id)
    scene = await session.get(SceneVersionModel, scene_version_id)
    if production is None or scene is None or scene.storyboard_version_id != production.storyboard_version_id:
        raise HTTPException(status_code=404, detail="Scene is not part of this production storyboard")
    return await _start_regeneration(workflow_type="scene-media-regeneration", production=production, selection={"scene_version_id": str(scene.id)}, payload=payload, request=request, actor=actor, session=session)


@router.post("/productions/{production_id}/narration/{script_segment_id}/regenerate", response_model=ResearchWorkflowView, status_code=202)
async def regenerate_narration_segment(
    production_id: UUID, script_segment_id: UUID, payload: MediaRegenerationStart,
    request: Request, actor: Operator, session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    production = await session.get(MediaProductionModel, production_id)
    storyboard = await session.get(StoryboardVersionModel, production.storyboard_version_id) if production else None
    segment = await session.get(ScriptSegmentModel, script_segment_id)
    if production is None or storyboard is None or segment is None or segment.script_version_id != storyboard.script_version_id:
        raise HTTPException(status_code=404, detail="Narration segment is not part of this production script")
    return await _start_regeneration(workflow_type="narration-segment-regeneration", production=production, selection={"script_segment_id": str(segment.id)}, payload=payload, request=request, actor=actor, session=session)


@router.post("/qa-findings/{finding_id}/override", response_model=MediaProductionView)
async def override_finding(finding_id: UUID, payload: QAOverrideWrite, request: Request, actor: Reviewer, session: Annotated[AsyncSession, Depends(get_session)]) -> MediaProductionView:
    finding = await session.get(QAFindingModel, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="QA finding not found")
    if finding.override_policy != "reasoned":
        raise HTTPException(status_code=409, detail="This mandatory QA failure cannot be overridden")
    existing = await session.scalar(select(QAOverrideModel).where(QAOverrideModel.finding_id == finding.id))
    if existing:
        raise HTTPException(status_code=409, detail="This finding already has an immutable override")
    override = QAOverrideModel(id=uuid4(), finding_id=finding.id, reason=payload.reason, actor_id=actor.id, correlation_id=request.state.correlation_id, created_at=datetime.now(timezone.utc))
    session.add(override)
    report = await session.get(QAReportModel, finding.report_id)
    render = await session.get(ProductionRenderModel, report.render_id)
    await append_audit(session, action="qa_finding.overridden", actor_id=actor.id, target_type="qa_finding", target_id=str(finding.id), correlation_id=request.state.correlation_id, context={"code": finding.code, "reason": payload.reason})
    await session.commit()
    return await _production_view(session, await session.get(MediaProductionModel, render.production_id))


@router.post("/renders/{render_id}/approval", response_model=MediaProductionView)
async def approve_render(render_id: UUID, payload: RenderApprovalWrite, request: Request, actor: Reviewer, session: Annotated[AsyncSession, Depends(get_session)]) -> MediaProductionView:
    render = await session.get(ProductionRenderModel, render_id)
    if render is None:
        raise HTTPException(status_code=404, detail="Render not found")
    manifest = await session.get(ProductionManifestModel, render.manifest_id)
    report = await session.scalar(select(QAReportModel).where(QAReportModel.render_id == render.id))
    if render.content_hash != payload.expected_render_hash or manifest.content_hash != payload.expected_manifest_hash:
        raise HTTPException(status_code=409, detail="Render or manifest changed; reload before reviewing")
    unoverridden_fail = await session.scalar(select(func.count(QAFindingModel.id)).outerjoin(QAOverrideModel, QAOverrideModel.finding_id == QAFindingModel.id).where(QAFindingModel.report_id == report.id, QAFindingModel.verdict == "fail", QAOverrideModel.id.is_(None)))
    if payload.decision == "approved" and unoverridden_fail:
        raise HTTPException(status_code=409, detail="Unresolved mandatory QA failures block approval")
    production = await session.get(MediaProductionModel, render.production_id)
    storyboard = await session.get(StoryboardVersionModel, production.storyboard_version_id)
    if storyboard.content_hash != production.storyboard_hash:
        raise HTTPException(status_code=409, detail="Production is stale against its bound storyboard")
    script = await session.scalar(
        select(ScriptModel)
        .join(StoryboardModel, StoryboardModel.script_id == ScriptModel.id)
        .join(StoryboardVersionModel, StoryboardVersionModel.storyboard_id == StoryboardModel.id)
        .where(StoryboardVersionModel.id == production.storyboard_version_id)
    )
    if script is None:
        raise HTTPException(status_code=409, detail="Production script lineage is unavailable")
    if script.source_kind == "direct_scripted_video":
        policy_snapshot = {
            "mode": "direct_scripted_video",
            "risk": "operator_supplied",
            "required_human_gates": ["script_approval", "storyboard_approval", "render_approval"],
            "evidence_required": False,
        }
        editorial_rationale = "Direct scripted-video production; factual and business correctness accepted by reviewer."
        subject = None
        opportunity = None
    else:
        policy_row = (
            await session.execute(
                select(SubjectProfileModel, OpportunityModel)
                .join(OpportunityModel, OpportunityModel.subject_profile_id == SubjectProfileModel.id)
                .where(OpportunityModel.id == script.opportunity_id)
            )
        ).one_or_none()
        if policy_row is None:
            raise HTTPException(status_code=409, detail="Production editorial policy lineage is unavailable")
        subject, opportunity = policy_row
        profile = subject.approval_profile or {}
        policy_snapshot = evaluate_operating_policy(
            mode=str(profile.get("mode", "assisted")), risk=subject.risk,
            sensitive_topics=profile.get("sensitive_topics", []),
        ).as_dict()
        if payload.decision == "approved" and len(opportunity.editorial_rationale.strip()) < 20:
            raise HTTPException(status_code=409, detail="Final approval requires the opportunity editorial rationale")
        editorial_rationale = opportunity.editorial_rationale
    previous = await session.scalar(select(ApprovalModel).where(ApprovalModel.target_type == "production_render", ApprovalModel.target_id == render.id).order_by(ApprovalModel.created_at.desc()).limit(1))
    approval = ApprovalModel(id=uuid4(), target_type="production_render", target_id=render.id, target_version=render.render_number, target_hash=render.content_hash, decision=payload.decision, comment=payload.comment, policy_snapshot={"manifest_hash": manifest.content_hash, "qa_report_hash": report.content_hash, "unoverridden_failures": unoverridden_fail, "operating_policy": policy_snapshot, "editorial_rationale": editorial_rationale}, supersedes_approval_id=previous.id if previous else None, actor_id=actor.id, correlation_id=request.state.correlation_id, created_at=datetime.now(timezone.utc))
    session.add(approval)
    await append_audit(session, action=f"production_render.{payload.decision}", actor_id=actor.id, target_type="production_render", target_id=str(render.id), correlation_id=request.state.correlation_id, context={"render_hash": render.content_hash, "manifest_hash": manifest.content_hash, "qa_report_hash": report.content_hash, "operating_policy": policy_snapshot})
    continuation: AutomaticContinuation | None = None
    if payload.decision == "approved":
        existing_metadata = await session.scalar(
            select(PublishMetadataVersionModel).where(
                PublishMetadataVersionModel.render_id == render.id
            ).order_by(PublishMetadataVersionModel.version_number.desc()).limit(1)
        )
        if existing_metadata:
            continuation = AutomaticContinuation(
                state="completed",
                action="publishing_metadata",
                message="Publishing metadata already exists for this approved render.",
            )
        else:
            assets = list(
                await session.scalars(
                    select(MediaAssetModel).where(MediaAssetModel.production_id == production.id)
                )
            )
            caption = next(
                (item for item in assets if item.asset_kind in {"caption_vtt", "caption_srt"}),
                None,
            )
            thumbnail = next((item for item in assets if item.asset_kind == "thumbnail"), None)
            if script.source_kind == "direct_scripted_video":
                sources = [
                    {
                        "title": "Operator-supplied direct script reviewed in TubeFactory",
                        "url": f"urn:tubefactory:script:{script.id}",
                    }
                ]
            else:
                sources = [
                    {"title": str(item.get("title", "Source")), "url": str(item.get("url", ""))}
                    for item in manifest.document.get("sources", [])
                    if item.get("url")
                ]
            raw_chapters = manifest.document.get("chapters", [])
            chapters = [
                {
                    "title": str(item.get("title") or f"Chapter {index}"),
                    "start_seconds": max(0, int(item.get("start_seconds", item.get("timecode_seconds", 0)))),
                }
                for index, item in enumerate(raw_chapters, start=1)
            ]
            if not chapters:
                chapters = [{"title": "Introduction", "start_seconds": 0}]
            script_version = await session.get(ScriptVersionModel, storyboard.script_version_id)
            if subject is not None:
                channel = await session.get(ChannelProfileModel, subject.channel_profile_id)
            elif script.production_brief_id:
                channel = await session.scalar(
                    select(ChannelProfileModel)
                    .join(
                        ProductionBriefModel,
                        ProductionBriefModel.channel_profile_id == ChannelProfileModel.id,
                    )
                    .where(ProductionBriefModel.id == script.production_brief_id)
                )
            else:
                channel = None
            if caption is None or thumbnail is None or not sources or script_version is None:
                missing = []
                if caption is None:
                    missing.append("captions")
                if thumbnail is None:
                    missing.append("thumbnail")
                if not sources:
                    missing.append("sources")
                if script_version is None:
                    missing.append("script title")
                continuation = AutomaticContinuation(
                    state="awaiting_input",
                    action="publishing_metadata",
                    message="Render approval is recorded, but publishing metadata needs " + ", ".join(missing) + ".",
                )
            else:
                from youtuber_api.routers.publishing import create_metadata

                metadata = await create_metadata(
                    PublishMetadataWrite(
                        render_id=render.id,
                        expected_render_hash=render.content_hash,
                        title=script_version.title[:100],
                        description=(
                            "Direct scripted-video production from operator-supplied, reviewer-approved material."
                            if script.source_kind == "direct_scripted_video"
                            else "Evidence-first explanation based on the cited sources, including uncertainty and counterevidence."
                        ),
                        sources=sources,
                        evidence_url=None,
                        chapters=chapters,
                        tags=(
                            ["direct-script", "reviewed", "explainer"]
                            if script.source_kind == "direct_scripted_video"
                            else ["evidence", "sources", "explainer"]
                        ),
                        category_id="27",
                        language=(channel.languages[0] if channel and channel.languages else "en"),
                        made_for_kids=False,
                        contains_synthetic_media=any(
                            bool(item.get("scene_spec", {}).get("synthetic_media_flag"))
                            for item in manifest.document.get("scenes", [])
                        ),
                        captions={"asset_id": str(caption.id), "language": (channel.languages[0] if channel and channel.languages else "en"), "name": "Captions"},
                        thumbnail={"asset_id": str(thumbnail.id)},
                        comment="Automatically created after exact render approval.",
                    ),
                    request,
                    actor,
                    session,
                )
                continuation = AutomaticContinuation(
                    state="completed",
                    action="publishing_metadata",
                    message=f"Publishing metadata v{metadata.version_number} was created automatically; private-upload approval is the next human gate.",
                )
        await append_audit(
            session,
            action=f"automation.render_{continuation.state}",
            actor_id=actor.id,
            target_type="production_render",
            target_id=str(render.id),
            correlation_id=request.state.correlation_id,
            context=continuation.model_dump(mode="json"),
        )
    await session.commit()
    detail = await _production_view(session, production)
    return detail.model_copy(update={"automatic_continuation": continuation})


@router.get("/assets/{asset_id}/content")
async def stream_asset(asset_id: UUID, request: Request, _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]) -> StreamingResponse:
    asset = await session.get(MediaAssetModel, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Media asset not found")
    try:
        requested_range = parse_byte_range(request.headers.get("range"), asset.byte_size)
    except ValueError as exc:
        raise HTTPException(
            status_code=416,
            detail="Requested byte range is not satisfiable",
            headers={"Content-Range": f"bytes */{asset.byte_size}"},
        ) from exc
    start, end = requested_range or (0, asset.byte_size - 1)
    length = end - start + 1
    response = await asyncio.to_thread(
        request.app.state.minio_client.get_object,
        get_settings().minio_bucket,
        asset.object_key,
        offset=start,
        length=length,
    )

    def body():
        try:
            yield from response.stream(1024 * 1024)
        finally:
            response.close()
            response.release_conn()

    filename = asset.object_key.rsplit("/", 1)[-1]
    headers = {
        "ETag": asset.content_hash,
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "private, max-age=3600, immutable",
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    if requested_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{asset.byte_size}"
    return StreamingResponse(
        body(),
        status_code=206 if requested_range else 200,
        media_type=asset.mime_type,
        headers=headers,
    )
