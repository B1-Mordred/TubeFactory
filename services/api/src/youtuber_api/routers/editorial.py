from __future__ import annotations

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.service import RPCError

from editorial_core.authorization import Permission
from editorial_core.editorial import (
    ScriptSegmentDraft,
    StatementAnnotation,
    StatementKind,
    validate_scene_spec,
    validate_storyboard,
    verify_script_draft,
)
from editorial_core.direct_scripted_video import parse_direct_scripted_video
from youtuber_api.audit import append_audit
from youtuber_api.db import get_session
from youtuber_api.editorial_gateway import TemporalEditorialGateway
from youtuber_api.models import (
    ApprovalModel,
    ChannelProfileModel,
    ClaimEvidenceModel,
    ClaimModel,
    ComfyWorkflowHeadModel,
    ComfyWorkflowVersionModel,
    EvidenceExcerptModel,
    MediaProductionModel,
    OpportunityModel,
    ProductionBriefModel,
    ResearchDossierModel,
    SceneAlternativeModel,
    SceneModel,
    SceneVersionModel,
    ScriptModel,
    ScriptSegmentModel,
    ScriptVersionModel,
    SegmentClaimModel,
    StoryboardModel,
    StoryboardVersionModel,
    SourceSnapshotModel,
    SubjectProfileModel,
    UserModel,
    VoiceProfileHeadModel,
    VoiceProfileVersionModel,
    WorkflowControlRecordModel,
    WorkflowTransitionModel,
)
from youtuber_api.schemas import (
    AutomaticContinuation,
    DirectScriptedVideoImportStart,
    DirectScriptedVideoPreviewStart,
    DirectScriptedVideoPreviewView,
    EditorialApprovalWrite,
    ExistingResearchScriptImportStart,
    MediaProductionStart,
    MediaTimelineDraftStart,
    PlaceholderReplacementWrite,
    ResearchWorkflowCancel,
    ResearchWorkflowLogEntry,
    ResearchWorkflowRetry,
    ResearchWorkflowView,
    SceneEdit,
    SceneAlternativeGenerationStart,
    SceneAlternativeSelect,
    SceneAlternativeView,
    SceneLockWrite,
    ScriptDetailView,
    ScriptGenerationStart,
    ScriptRegenerationStart,
    ScriptSummaryView,
    ScriptVerificationStart,
    ScriptVersionEdit,
    StoryboardDetailView,
    StoryboardGenerationStart,
    StoryboardSummaryView,
)
from youtuber_api.security import require


router = APIRouter(prefix="/editorial", tags=["editorial-production"])
Viewer = Annotated[UserModel, Depends(require(Permission.VIEW))]
Operator = Annotated[UserModel, Depends(require(Permission.OPERATE_WORKFLOWS))]
Reviewer = Annotated[UserModel, Depends(require(Permission.REVIEW))]
Editor = Annotated[UserModel, Depends(require(Permission.EDIT_EDITORIAL))]
_WORKFLOW_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,240}$")
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TERMINATED", "TIMED_OUT"}
_EDITORIAL_WORKFLOWS = {
    "direct-scripted-video-import",
    "script-import-existing-research",
    "script-generation",
    "script-regeneration",
    "script-verification",
    "scene-alternative-generation",
    "storyboard-generation",
    "media-production",
    "media-timeline-draft",
    "media-timeline-render",
    "scene-media-regeneration",
    "narration-segment-regeneration",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _gateway(request: Request) -> TemporalEditorialGateway:
    return TemporalEditorialGateway(request.app.state.temporal_client)


def _valid_workflow_id(value: str) -> str:
    if not _WORKFLOW_ID.fullmatch(value):
        raise HTTPException(status_code=422, detail="Invalid workflow ID")
    return value


async def _start_tracked(
    session: AsyncSession,
    gateway: TemporalEditorialGateway,
    *,
    workflow_type: str,
    payload: dict[str, Any],
    actor: UserModel,
    correlation_id: str,
    parent_workflow_id: str | None = None,
) -> tuple[str, bool]:
    workflow_id = payload["workflow_id"]
    existing = await session.get(WorkflowControlRecordModel, workflow_id)
    if existing:
        old = {key: value for key, value in existing.request_payload.items() if key not in {"actor_id", "correlation_id"}}
        new = {key: value for key, value in payload.items() if key not in {"actor_id", "correlation_id"}}
        if existing.workflow_type != workflow_type or old != new or existing.parent_workflow_id != parent_workflow_id:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key is already associated with a different workflow request",
            )
        await gateway.start(workflow_type, existing.request_payload)
        return workflow_id, True
    session.add(
        WorkflowControlRecordModel(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            request_payload=payload,
            parent_workflow_id=parent_workflow_id,
            correlation_id=correlation_id,
            started_by=actor.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.flush()
    await gateway.start(workflow_type, payload)
    return workflow_id, False


async def _workflow_view(
    session: AsyncSession, gateway: TemporalEditorialGateway, workflow_id: str
) -> ResearchWorkflowView:
    value = await gateway.describe(workflow_id)
    record = await session.get(WorkflowControlRecordModel, workflow_id)
    value["correlation_id"] = record.correlation_id if record else None
    return ResearchWorkflowView.model_validate(value)


@router.post("/script-runs", response_model=ResearchWorkflowView, status_code=202)
async def start_script_run(
    payload: ScriptGenerationStart,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    dossier = await session.get(ResearchDossierModel, payload.dossier_id)
    if dossier is None or dossier.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Dossier not found")
    if dossier.status != "approved" or dossier.version != payload.expected_dossier_version:
        raise HTTPException(status_code=409, detail="Use the exact current approved dossier version")
    readiness = dossier.completion_evaluation.get("explanation_readiness", {})
    if not readiness.get("ready"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Dossier is approved but not ready for the configured explainer format",
                "gaps": readiness.get("gaps", ["explanation_readiness_missing"]),
            },
        )
    if await session.scalar(select(ScriptModel.id).where(ScriptModel.research_dossier_id == dossier.id)):
        raise HTTPException(status_code=409, detail="This dossier already has a versioned script")
    approved_claims = await session.scalar(
        select(func.count(ClaimModel.id)).where(
            ClaimModel.research_dossier_id == dossier.id,
            ClaimModel.status == "approved",
            ClaimModel.deleted_at.is_(None),
        )
    )
    if not approved_claims:
        raise HTTPException(status_code=409, detail="Approve at least one dossier claim before generating a script")
    workflow_id = f"script-generation-{payload.idempotency_key}"
    gateway = _gateway(request)
    workflow_payload = {
        "workflow_id": workflow_id,
        "dossier_id": str(dossier.id),
        "expected_dossier_version": dossier.version,
        "sensitivity": payload.sensitivity,
        "idempotency_key": payload.idempotency_key,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    _, reconciled = await _start_tracked(
        session,
        gateway,
        workflow_type="script-generation",
        payload=workflow_payload,
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action="script.generation_reconciled" if reconciled else "script.generation_started",
        actor_id=actor.id,
        target_type="research_dossier",
        target_id=str(dossier.id),
        correlation_id=request.state.correlation_id,
        context={"workflow_id": workflow_id, "dossier_version": dossier.version},
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post(
    "/script-import-runs", response_model=ResearchWorkflowView, status_code=202
)
async def start_existing_research_script_import(
    payload: ExistingResearchScriptImportStart,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    opportunity = await session.get(OpportunityModel, payload.opportunity_id)
    if opportunity is None or opportunity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Active opportunity not found")

    script = await session.scalar(
        select(ScriptModel).where(
            ScriptModel.opportunity_id == opportunity.id,
            ScriptModel.deleted_at.is_(None),
        )
    )
    if script is not None:
        if script.status == "approved":
            raise HTTPException(
                status_code=409,
                detail="The current script is approved; create a new production branch before replacing it",
            )
        if await session.scalar(
            select(StoryboardModel.id).where(StoryboardModel.script_id == script.id)
        ):
            raise HTTPException(
                status_code=409,
                detail="This script already has a storyboard and cannot be replaced by an import",
            )
        dossier = await session.get(ResearchDossierModel, script.research_dossier_id)
    else:
        dossier = await session.scalar(
            select(ResearchDossierModel)
            .where(
                ResearchDossierModel.opportunity_id == opportunity.id,
                ResearchDossierModel.status == "approved",
                ResearchDossierModel.deleted_at.is_(None),
            )
            .order_by(
                ResearchDossierModel.dossier_version.desc(),
                ResearchDossierModel.version.desc(),
            )
        )
    if dossier is None or dossier.deleted_at is not None or dossier.status != "approved":
        raise HTTPException(
            status_code=409,
            detail="Use existing research requires an approved source brief for this opportunity",
        )
    readiness = dossier.completion_evaluation.get("explanation_readiness", {})
    if not readiness.get("ready"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "The selected source brief is not explanation-ready",
                "gaps": readiness.get("gaps", ["explanation_readiness_missing"]),
            },
        )
    approved_claims = await session.scalar(
        select(func.count(ClaimModel.id)).where(
            ClaimModel.research_dossier_id == dossier.id,
            ClaimModel.status == "approved",
            ClaimModel.deleted_at.is_(None),
        )
    )
    if not approved_claims:
        raise HTTPException(
            status_code=409,
            detail="The selected source brief has no approved claims",
        )

    workflow_id = f"script-import-existing-research-{payload.idempotency_key}"
    workflow_payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "mode": "replace_unapproved" if script is not None else "initial",
        "opportunity_id": str(opportunity.id),
        "dossier_id": str(dossier.id),
        "expected_dossier_version": dossier.version,
        "script_title": payload.title,
        "script_text": payload.script_text,
        "script_text_hash": hashlib.sha256(payload.script_text.encode()).hexdigest(),
        "sensitivity": payload.sensitivity,
        "idempotency_key": payload.idempotency_key,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    if script is not None:
        current = await session.get(ScriptVersionModel, script.current_version_id)
        if current is None:
            raise HTTPException(status_code=409, detail="Current script version is missing")
        segment_rows = list(
            await session.scalars(
                select(ScriptSegmentModel)
                .where(ScriptSegmentModel.script_version_id == current.id)
                .order_by(ScriptSegmentModel.segment_order)
            )
        )
        locked = [item.segment_key for item in segment_rows if item.locked]
        if locked:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Unlock all script segments before replacing the draft",
                    "locked_segment_keys": locked,
                },
            )
        workflow_payload.update(
            {
                "script_id": str(script.id),
                "expected_version": current.version_number,
                "expected_hash": current.content_hash,
                "segment_keys": [item.segment_key for item in segment_rows],
            }
        )

    gateway = _gateway(request)
    _, reconciled = await _start_tracked(
        session,
        gateway,
        workflow_type="script-import-existing-research",
        payload=workflow_payload,
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action=(
            "script.existing_research_import_reconciled"
            if reconciled
            else "script.existing_research_import_started"
        ),
        actor_id=actor.id,
        target_type="research_dossier",
        target_id=str(dossier.id),
        correlation_id=request.state.correlation_id,
        context={
            "workflow_id": workflow_id,
            "opportunity_id": str(opportunity.id),
            "dossier_version": dossier.version,
            "mode": workflow_payload["mode"],
            "script_text_hash": workflow_payload["script_text_hash"],
            "script_character_count": len(payload.script_text),
        },
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post(
    "/direct-scripted-video-preview",
    response_model=DirectScriptedVideoPreviewView,
)
async def preview_direct_scripted_video(
    payload: DirectScriptedVideoPreviewStart,
    _: Editor,
) -> DirectScriptedVideoPreviewView:
    try:
        parsed = parse_direct_scripted_video(
            payload.master_script,
            title=payload.title,
            target_wpm_min=payload.target_wpm_min,
            target_wpm_max=payload.target_wpm_max,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DirectScriptedVideoPreviewView.model_validate(parsed.preview())


@router.post(
    "/direct-scripted-video-runs",
    response_model=ResearchWorkflowView,
    status_code=202,
)
async def start_direct_scripted_video_import(
    payload: DirectScriptedVideoImportStart,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    channel = await session.get(ChannelProfileModel, payload.channel_profile_id)
    if channel is None or channel.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Active channel profile not found")
    try:
        parsed = parse_direct_scripted_video(
            payload.master_script,
            title=payload.title,
            target_wpm_min=payload.target_wpm_min,
            target_wpm_max=payload.target_wpm_max,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    workflow_id = f"direct-scripted-video-import-{payload.idempotency_key}"
    workflow_payload = {
        "workflow_id": workflow_id,
        "channel_profile_id": str(channel.id),
        "title": payload.title,
        "master_script": payload.master_script,
        "master_script_hash": hashlib.sha256(payload.master_script.encode()).hexdigest(),
        "target_wpm_min": payload.target_wpm_min,
        "target_wpm_max": payload.target_wpm_max,
        "sensitivity": payload.sensitivity,
        "idempotency_key": payload.idempotency_key,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    gateway = _gateway(request)
    _, reconciled = await _start_tracked(
        session,
        gateway,
        workflow_type="direct-scripted-video-import",
        payload=workflow_payload,
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action=(
            "script.direct_scripted_video_import_reconciled"
            if reconciled
            else "script.direct_scripted_video_import_started"
        ),
        actor_id=actor.id,
        target_type="channel_profile",
        target_id=str(channel.id),
        correlation_id=request.state.correlation_id,
        context={
            "workflow_id": workflow_id,
            "scene_count": parsed.scene_count,
            "placeholder_tokens": list(parsed.placeholder_tokens),
            "master_script_hash": workflow_payload["master_script_hash"],
        },
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post("/scripts/{script_id}/storyboard-runs", response_model=ResearchWorkflowView, status_code=202)
async def start_storyboard_run(
    script_id: UUID,
    payload: StoryboardGenerationStart,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    script = await session.get(ScriptModel, script_id)
    if script is None or script.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Script not found")
    if script.status != "approved" or script.current_version_id != payload.script_version_id:
        raise HTTPException(status_code=409, detail="Use the exact current approved script version")
    if await session.scalar(select(StoryboardModel.id).where(StoryboardModel.script_id == script.id)):
        raise HTTPException(status_code=409, detail="This script already has a storyboard")
    workflow_id = f"storyboard-generation-{payload.idempotency_key}"
    gateway = _gateway(request)
    workflow_payload = {
        "workflow_id": workflow_id,
        "script_id": str(script.id),
        "script_version_id": str(payload.script_version_id),
        "sensitivity": payload.sensitivity,
        "idempotency_key": payload.idempotency_key,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    _, reconciled = await _start_tracked(
        session,
        gateway,
        workflow_type="storyboard-generation",
        payload=workflow_payload,
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action="storyboard.generation_reconciled" if reconciled else "storyboard.generation_started",
        actor_id=actor.id,
        target_type="script_version",
        target_id=str(payload.script_version_id),
        correlation_id=request.state.correlation_id,
        context={"workflow_id": workflow_id},
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post("/scripts/{script_id}/verification-runs", response_model=ResearchWorkflowView, status_code=202)
async def start_script_verification_run(
    script_id: UUID,
    payload: ScriptVerificationStart,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    script = await session.get(ScriptModel, script_id)
    if script is None or script.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Script not found")
    if script.source_kind == "direct_scripted_video":
        raise HTTPException(
            status_code=409,
            detail="Direct scripted-video scripts do not use independent evidence verification",
        )
    version = await session.get(ScriptVersionModel, script.current_version_id)
    if version is None:
        raise HTTPException(status_code=409, detail="Script has no current version")
    if version.version_number != payload.expected_version or version.content_hash != payload.expected_hash:
        raise HTTPException(status_code=409, detail="Script version or hash changed; reload before verification")
    if version.status != "draft" or not version.verification_report.get("deterministic_valid"):
        raise HTTPException(
            status_code=409,
            detail="Only a deterministic-valid edited draft can enter independent verification",
        )
    workflow_id = f"script-verification-{payload.idempotency_key}"
    gateway = _gateway(request)
    workflow_payload = {
        "workflow_id": workflow_id,
        "script_id": str(script.id),
        "expected_version": version.version_number,
        "expected_hash": version.content_hash,
        "sensitivity": payload.sensitivity,
        "idempotency_key": payload.idempotency_key,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    _, reconciled = await _start_tracked(
        session,
        gateway,
        workflow_type="script-verification",
        payload=workflow_payload,
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action="script.verification_reconciled" if reconciled else "script.verification_started",
        actor_id=actor.id,
        target_type="script_version",
        target_id=str(version.id),
        correlation_id=request.state.correlation_id,
        context={"workflow_id": workflow_id, "version": version.version_number},
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post("/scripts/{script_id}/regeneration-runs", response_model=ResearchWorkflowView, status_code=202)
async def start_script_regeneration_run(
    script_id: UUID,
    payload: ScriptRegenerationStart,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    script = await session.get(ScriptModel, script_id)
    if script is None or script.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Script not found")
    if script.source_kind == "direct_scripted_video":
        raise HTTPException(
            status_code=409,
            detail="Direct scripted-video scripts cannot use evidence-bound regeneration",
        )
    version = await session.get(ScriptVersionModel, script.current_version_id)
    if version is None:
        raise HTTPException(status_code=409, detail="Script has no current version")
    if version.version_number != payload.expected_version or version.content_hash != payload.expected_hash:
        raise HTTPException(status_code=409, detail="Script version or hash changed; reload before regeneration")
    segments = list(
        await session.scalars(
            select(ScriptSegmentModel).where(
                ScriptSegmentModel.script_version_id == version.id,
                ScriptSegmentModel.segment_key.in_(payload.segment_keys),
            )
        )
    )
    found = {item.segment_key for item in segments}
    missing = sorted(set(payload.segment_keys) - found)
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown segment keys: {', '.join(missing)}")
    locked = sorted(item.segment_key for item in segments if item.locked)
    if locked:
        raise HTTPException(
            status_code=409,
            detail=f"Locked segments cannot be regenerated: {', '.join(locked)}",
        )
    workflow_id = f"script-regeneration-{payload.idempotency_key}"
    gateway = _gateway(request)
    workflow_payload = {
        "workflow_id": workflow_id,
        "script_id": str(script.id),
        "expected_version": version.version_number,
        "expected_hash": version.content_hash,
        "segment_keys": payload.segment_keys,
        "instruction": payload.instruction,
        "sensitivity": payload.sensitivity,
        "idempotency_key": payload.idempotency_key,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    _, reconciled = await _start_tracked(
        session,
        gateway,
        workflow_type="script-regeneration",
        payload=workflow_payload,
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action=(
            "script.regeneration_reconciled" if reconciled else "script.regeneration_started"
        ),
        actor_id=actor.id,
        target_type="script_version",
        target_id=str(version.id),
        correlation_id=request.state.correlation_id,
        context={
            "workflow_id": workflow_id,
            "version": version.version_number,
            "segment_keys": payload.segment_keys,
            "instruction": payload.instruction,
        },
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.get("/runs/{workflow_id}", response_model=ResearchWorkflowView)
async def get_run(
    workflow_id: str,
    request: Request,
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    workflow_id = _valid_workflow_id(workflow_id)
    record = await session.get(WorkflowControlRecordModel, workflow_id)
    if record is None or record.workflow_type not in _EDITORIAL_WORKFLOWS:
        raise HTTPException(status_code=404, detail="Editorial workflow not found")
    try:
        return await _workflow_view(session, _gateway(request), workflow_id)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail="Editorial workflow not found") from exc


@router.get("/runs/{workflow_id}/logs", response_model=list[ResearchWorkflowLogEntry])
async def get_run_logs(workflow_id: str, request: Request, _: Viewer) -> list[dict[str, Any]]:
    workflow_id = _valid_workflow_id(workflow_id)
    try:
        return await _gateway(request).safe_logs(workflow_id)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail="Editorial workflow not found") from exc


@router.get("/runs/{workflow_id}/events")
async def stream_run(
    workflow_id: str,
    request: Request,
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    workflow_id = _valid_workflow_id(workflow_id)
    gateway = _gateway(request)
    try:
        initial = await _workflow_view(session, gateway, workflow_id)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail="Editorial workflow not found") from exc

    async def events():
        current = initial
        for _ in range(900):
            yield "event: status\ndata: " + json.dumps(current.model_dump(mode="json"), separators=(",", ":")) + "\n\n"
            if current.execution_status in _TERMINAL or await request.is_disconnected():
                break
            await asyncio.sleep(1)
            try:
                current = await _workflow_view(session, gateway, workflow_id)
            except RPCError:
                break

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{workflow_id}/cancel", response_model=ResearchWorkflowView)
async def cancel_run(
    workflow_id: str,
    payload: ResearchWorkflowCancel,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    workflow_id = _valid_workflow_id(workflow_id)
    record = await session.get(WorkflowControlRecordModel, workflow_id)
    if record is None or record.workflow_type not in _EDITORIAL_WORKFLOWS:
        raise HTTPException(status_code=404, detail="Editorial workflow not found")
    gateway = _gateway(request)
    current = await _workflow_view(session, gateway, workflow_id)
    if current.execution_status == "RUNNING":
        await gateway.cancel(workflow_id)
    await append_audit(
        session,
        action="editorial.workflow_cancel_requested",
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=workflow_id,
        correlation_id=request.state.correlation_id,
        context={"reason": payload.reason, "prior_status": current.execution_status},
    )
    await session.commit()
    if current.execution_status != "RUNNING":
        return current
    for _ in range(20):
        await asyncio.sleep(0.1)
        current = await _workflow_view(session, gateway, workflow_id)
        if current.execution_status != "RUNNING":
            break
    return current


@router.post("/runs/{workflow_id}/retry", response_model=ResearchWorkflowView, status_code=202)
async def retry_run(
    workflow_id: str,
    payload: ResearchWorkflowRetry,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    workflow_id = _valid_workflow_id(workflow_id)
    original = await session.get(WorkflowControlRecordModel, workflow_id)
    if original is None or original.workflow_type not in _EDITORIAL_WORKFLOWS:
        raise HTTPException(status_code=404, detail="Editorial workflow not found")
    gateway = _gateway(request)
    current = await _workflow_view(session, gateway, workflow_id)
    if not current.retryable:
        raise HTTPException(status_code=409, detail="Only failed or cancelled workflows can be retried")
    retry_id = f"{original.workflow_type}-retry-{payload.idempotency_key}"
    retry_payload = {
        **original.request_payload,
        "workflow_id": retry_id,
        "idempotency_key": payload.idempotency_key,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    _, reconciled = await _start_tracked(
        session,
        gateway,
        workflow_type=original.workflow_type,
        payload=retry_payload,
        actor=actor,
        correlation_id=request.state.correlation_id,
        parent_workflow_id=workflow_id,
    )
    await append_audit(
        session,
        action="editorial.workflow_retry_reconciled" if reconciled else "editorial.workflow_retried",
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=retry_id,
        correlation_id=request.state.correlation_id,
        context={"parent_workflow_id": workflow_id, "reason": payload.reason},
    )
    await session.commit()
    return await _workflow_view(session, gateway, retry_id)


async def _script_detail(session: AsyncSession, script: ScriptModel) -> ScriptDetailView:
    version = await session.get(ScriptVersionModel, script.current_version_id)
    if version is None:
        raise HTTPException(status_code=409, detail="Script has no current version")
    segments = list(
        await session.scalars(
            select(ScriptSegmentModel)
            .where(ScriptSegmentModel.script_version_id == version.id)
            .order_by(ScriptSegmentModel.segment_order)
        )
    )
    output: list[dict[str, Any]] = []
    for segment in segments:
        links = list(
            await session.scalars(
                select(SegmentClaimModel).where(
                    SegmentClaimModel.script_segment_id == segment.id
                )
            )
        )
        output.append(
            {
                "id": str(segment.id),
                "segment_key": segment.segment_key,
                "segment_order": segment.segment_order,
                "segment_type": segment.segment_type,
                "narration": segment.narration,
                "presentation_purpose": segment.presentation_purpose,
                "duration_seconds": segment.duration_seconds,
                "citation_display": segment.citation_display,
                "annotations": segment.annotations,
                "locked": segment.locked,
                "content_hash": segment.content_hash,
                "claim_links": [
                    {
                        "claim_id": str(item.claim_id),
                        "evidence_excerpt_id": (
                            str(item.evidence_excerpt_id) if item.evidence_excerpt_id else None
                        ),
                        "statement_text": item.statement_text,
                        "start_offset": item.start_offset,
                        "end_offset": item.end_offset,
                        "statement_kind": item.statement_kind,
                    }
                    for item in links
                ],
            }
        )
    channel_profile_id = await _script_channel_profile_id(session, script)
    evidence_required = bool(version.verification_report.get("evidence_required", True))
    return ScriptDetailView(
        id=script.id,
        dossier_id=script.research_dossier_id,
        opportunity_id=script.opportunity_id,
        production_brief_id=script.production_brief_id,
        channel_profile_id=channel_profile_id,
        source_kind=script.source_kind,
        evidence_required=evidence_required,
        status=script.status,
        version=version.version_number,
        current_version_id=version.id,
        title=version.title,
        content_hash=version.content_hash,
        coverage_percent=version.coverage_percent,
        created_at=version.created_at,
        verification_report=version.verification_report,
        segments=output,
    )


async def _script_channel_profile_id(
    session: AsyncSession, script: ScriptModel
) -> UUID | None:
    if script.production_brief_id:
        return await session.scalar(
            select(ProductionBriefModel.channel_profile_id).where(
                ProductionBriefModel.id == script.production_brief_id
            )
        )
    if script.opportunity_id:
        return await session.scalar(
            select(SubjectProfileModel.channel_profile_id)
            .join(OpportunityModel, OpportunityModel.subject_profile_id == SubjectProfileModel.id)
            .where(OpportunityModel.id == script.opportunity_id)
        )
    return None


@router.get("/scripts", response_model=list[ScriptSummaryView])
async def list_scripts(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[ScriptSummaryView]:
    scripts = list(
        await session.scalars(
            select(ScriptModel)
            .outerjoin(OpportunityModel, OpportunityModel.id == ScriptModel.opportunity_id)
            .where(
                ScriptModel.deleted_at.is_(None),
                (ScriptModel.source_kind == "direct_scripted_video")
                | (OpportunityModel.deleted_at.is_(None)),
            )
            .order_by(ScriptModel.created_at.desc())
        )
    )
    output: list[ScriptSummaryView] = []
    for script in scripts:
        detail = await _script_detail(session, script)
        output.append(
            ScriptSummaryView.model_validate(
                detail.model_dump(exclude={"verification_report", "segments"})
            )
        )
    return output


@router.get("/scripts/{script_id}", response_model=ScriptDetailView)
async def get_script(
    script_id: UUID,
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScriptDetailView:
    script = await session.get(ScriptModel, script_id)
    if script is None or script.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Script not found")
    return await _script_detail(session, script)


def _edit_core_segments(payload: ScriptVersionEdit) -> tuple[ScriptSegmentDraft, ...]:
    return tuple(
        ScriptSegmentDraft(
            segment_key=segment.segment_key,
            segment_type=segment.segment_type,
            narration=segment.narration,
            presentation_purpose=segment.presentation_purpose,
            duration_seconds=segment.duration_seconds,
            citation_display=segment.citation_display,
            annotations=tuple(
                StatementAnnotation(
                    text=item.text,
                    start_offset=item.start_offset,
                    end_offset=item.end_offset,
                    kind=StatementKind(item.kind),
                    claim_ids=tuple(str(value) for value in item.claim_ids),
                    evidence_excerpt_id=(
                        str(item.evidence_excerpt_id) if item.evidence_excerpt_id else None
                    ),
                )
                for item in segment.annotations
            ),
            locked=segment.locked,
        )
        for segment in payload.segments
    )


def _direct_edit_report(payload: ScriptVersionEdit) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    for segment in payload.segments:
        if not segment.annotations:
            issues.append(
                {
                    "code": "missing_editorial_annotation",
                    "severity": "error",
                    "segment_key": segment.segment_key,
                    "statement": None,
                    "message": "Direct scripted-video segments need at least one editorial annotation.",
                }
            )
        for annotation in segment.annotations:
            if annotation.claim_ids or annotation.evidence_excerpt_id:
                issues.append(
                    {
                        "code": "direct_mode_claim_link",
                        "severity": "error",
                        "segment_key": segment.segment_key,
                        "statement": annotation.text,
                        "message": "Direct scripted-video mode cannot reference dossier claims or evidence excerpts.",
                    }
                )
            if annotation.kind not in {"editorial", "opinion"}:
                issues.append(
                    {
                        "code": "direct_mode_factual_annotation",
                        "severity": "error",
                        "segment_key": segment.segment_key,
                        "statement": annotation.text,
                        "message": "Direct scripted-video mode stores operator-supplied narration without factual evidence labels.",
                    }
                )
    return {
        "valid": not any(item["severity"] == "error" for item in issues),
        "coverage_percent": 0,
        "issues": issues,
    }


def _direct_placeholders(payload: ScriptVersionEdit) -> list[str]:
    text = _canonical(payload.model_dump(mode="json"))
    return sorted(set(re.findall(r"\[[A-ZÄÖÜ0-9][A-ZÄÖÜ0-9_.:-]{1,80}\]", text)))


@router.post("/scripts/{script_id}/versions", response_model=ScriptDetailView, status_code=201)
async def edit_script_version(
    script_id: UUID,
    payload: ScriptVersionEdit,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScriptDetailView:
    script = await session.get(ScriptModel, script_id, with_for_update=True)
    if script is None or script.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Script not found")
    parent = await session.get(ScriptVersionModel, script.current_version_id)
    if parent is None:
        raise HTTPException(status_code=409, detail="Script has no current version")
    if parent.version_number != payload.expected_version or parent.content_hash != payload.expected_hash:
        raise HTTPException(status_code=409, detail="Script version or hash changed; reload before editing")
    if script.source_kind != "direct_scripted_video" and len(payload.segments) < 8:
        raise HTTPException(status_code=422, detail="Evidence-bound scripts require at least 8 segments")
    old_segments = list(
        await session.scalars(
            select(ScriptSegmentModel)
            .where(ScriptSegmentModel.script_version_id == parent.id)
            .order_by(ScriptSegmentModel.segment_order)
        )
    )
    incoming_by_key = {item.segment_key: item for item in payload.segments}
    if len(incoming_by_key) != len(payload.segments):
        raise HTTPException(status_code=422, detail="Segment keys must be unique")
    for old in old_segments:
        if not old.locked:
            continue
        incoming = incoming_by_key.get(old.segment_key)
        if incoming is None:
            raise HTTPException(status_code=409, detail=f"Locked segment {old.segment_key} is missing")
        old_document = {
            "segment_key": old.segment_key,
            "segment_type": old.segment_type,
            "narration": old.narration,
            "presentation_purpose": old.presentation_purpose,
            "duration_seconds": old.duration_seconds,
            "citation_display": old.citation_display,
            "annotations": old.annotations,
            "locked": old.locked,
        }
        if _canonical(incoming.model_dump(mode="json")) != _canonical(old_document):
            raise HTTPException(
                status_code=409,
                detail=f"Locked segment {old.segment_key} cannot be changed or unlocked",
            )
    if script.source_kind == "direct_scripted_video":
        direct_report = _direct_edit_report(payload)
        parent_report = parent.verification_report or {}
        report = None
        coverage_percent = direct_report["coverage_percent"]
        status = "verified" if direct_report["valid"] else "blocked"
        verification_report = {
            "valid": direct_report["valid"],
            "deterministic_valid": direct_report["valid"],
            "requires_independent_verification": False,
            "evidence_required": False,
            "claim_coverage_applicable": False,
            "mode": "direct_scripted_video",
            "source_kind": "direct_scripted_video",
            "coverage_percent": coverage_percent,
            "issues": direct_report["issues"],
            "placeholder_tokens": _direct_placeholders(payload),
            "unresolved_placeholders": _direct_placeholders(payload),
            "edit_comment": payload.comment,
            "direct_storyboard": parent_report.get("direct_storyboard", {"scenes": []}),
        }
    else:
        claims = list(
            await session.scalars(
                select(ClaimModel).where(
                    ClaimModel.research_dossier_id == script.research_dossier_id,
                    ClaimModel.status == "approved",
                    ClaimModel.deleted_at.is_(None),
                )
            )
        )
        approved_claim_ids = [str(item.id) for item in claims]
        central_claim_ids = [str(item.id) for item in claims if item.central]
        evidence_rows = (
            await session.execute(
                select(ClaimEvidenceModel, EvidenceExcerptModel)
                .join(
                    EvidenceExcerptModel,
                    EvidenceExcerptModel.id == ClaimEvidenceModel.evidence_excerpt_id,
                )
                .where(
                    ClaimEvidenceModel.claim_id.in_([item.id for item in claims]),
                    ClaimEvidenceModel.relationship.in_(["supports", "context"]),
                )
            )
        ).all()
        evidence_text: dict[str, str] = {}
        evidence_claims: dict[str, list[str]] = {}
        for link, excerpt in evidence_rows:
            excerpt_id = str(excerpt.id)
            evidence_text[excerpt_id] = excerpt.exact_text
            evidence_claims.setdefault(excerpt_id, []).append(str(link.claim_id))
        verified = verify_script_draft(
            _edit_core_segments(payload),
            approved_claim_ids=approved_claim_ids,
            central_claim_ids=central_claim_ids,
            evidence_text_by_id=evidence_text,
            evidence_claim_ids_by_id=evidence_claims,
        )
        report = verified
        coverage_percent = verified.coverage_percent
        status = "draft" if verified.valid else "blocked"
        verification_report = {
            "valid": False,
            "deterministic_valid": verified.valid,
            "requires_independent_verification": verified.valid,
            "issues": [item.__dict__ for item in verified.issues],
            "edit_comment": payload.comment,
        }
    next_number = int(
        await session.scalar(
            select(func.coalesce(func.max(ScriptVersionModel.version_number), 0) + 1)
            .where(ScriptVersionModel.script_id == script.id)
        )
    )
    version_id = uuid4()
    now = datetime.now(timezone.utc)
    document = {
        "title": payload.title,
        "segments": [item.model_dump(mode="json") for item in payload.segments],
    }
    content_hash = hashlib.sha256(_canonical(document).encode()).hexdigest()
    session.add(
        ScriptVersionModel(
            id=version_id,
            script_id=script.id,
            version_number=next_number,
            status=status,
            title=payload.title,
            total_duration_seconds=sum(item.duration_seconds for item in payload.segments),
            writer_model_id=parent.writer_model_id,
            verifier_model_id=None,
            writer_prompt_id=parent.writer_prompt_id,
            verifier_prompt_id=None,
            verification_report=verification_report,
            coverage_percent=coverage_percent,
            content_hash=content_hash,
            parent_version_id=parent.id,
            workflow_id=f"manual-script-edit-{version_id}",
            correlation_id=request.state.correlation_id,
            created_by=actor.id,
            created_at=now,
        )
    )
    await session.flush()
    for order, segment in enumerate(payload.segments, start=1):
        segment_id = uuid4()
        segment_document = segment.model_dump(mode="json")
        session.add(
            ScriptSegmentModel(
                id=segment_id,
                script_version_id=version_id,
                segment_key=segment.segment_key,
                segment_order=order,
                segment_type=segment.segment_type,
                narration=segment.narration,
                presentation_purpose=segment.presentation_purpose,
                duration_seconds=segment.duration_seconds,
                citation_display=segment.citation_display,
                annotations=[item.model_dump(mode="json") for item in segment.annotations],
                locked=segment.locked,
                content_hash=hashlib.sha256(_canonical(segment_document).encode()).hexdigest(),
                created_at=now,
            )
        )
        if script.source_kind != "direct_scripted_video":
            for annotation in segment.annotations:
                for claim_id in annotation.claim_ids:
                    session.add(
                        SegmentClaimModel(
                            script_segment_id=segment_id,
                            claim_id=claim_id,
                            evidence_excerpt_id=annotation.evidence_excerpt_id,
                            statement_text=annotation.text,
                            start_offset=annotation.start_offset,
                            end_offset=annotation.end_offset,
                            statement_kind=annotation.kind,
                            created_at=now,
                        )
                    )
    script.current_version_id = version_id
    script.status = status
    script.version += 1
    script.updated_at = now
    session.add(
        WorkflowTransitionModel(
            aggregate_type="script",
            aggregate_id=script.id,
            from_stage=parent.status.upper(),
            to_stage=status.upper(),
            reason=payload.comment,
            actor_id=actor.id,
            correlation_id=request.state.correlation_id,
            occurred_at=now,
        )
    )
    await append_audit(
        session,
        action="script.version_created",
        actor_id=actor.id,
        target_type="script_version",
        target_id=str(version_id),
        correlation_id=request.state.correlation_id,
        context={
            "version": next_number,
            "parent_version_id": str(parent.id),
            "content_hash": content_hash,
            "deterministic_valid": verification_report["deterministic_valid"],
            "requires_independent_verification": verification_report["requires_independent_verification"],
        },
    )
    await session.commit()
    return await _script_detail(session, script)


@router.post("/scripts/{script_id}/approve", response_model=ScriptDetailView)
async def approve_script(
    script_id: UUID,
    payload: EditorialApprovalWrite,
    request: Request,
    actor: Reviewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScriptDetailView:
    script = await session.get(ScriptModel, script_id, with_for_update=True)
    if script is None or script.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Script not found")
    version = await session.get(ScriptVersionModel, script.current_version_id)
    if version is None or version.status != "verified" or not version.verification_report.get("valid"):
        raise HTTPException(status_code=409, detail="Only the verified current script can be approved")
    if version.version_number != payload.expected_version or version.content_hash != payload.expected_hash:
        raise HTTPException(status_code=409, detail="Script version or hash changed; reload before approval")
    if script.status == "approved":
        return await _script_detail(session, script)
    now = datetime.now(timezone.utc)
    session.add(
        ApprovalModel(
            target_type="script_version",
            target_id=version.id,
            target_version=version.version_number,
            target_hash=version.content_hash,
            decision="approved",
            comment=payload.comment,
            policy_snapshot={
                "verification_valid": True,
                "coverage_percent": version.coverage_percent,
                "source_kind": script.source_kind,
                "evidence_required": bool(version.verification_report.get("evidence_required", True)),
            },
            supersedes_approval_id=None,
            actor_id=actor.id,
            correlation_id=request.state.correlation_id,
            created_at=now,
        )
    )
    script.status = "approved"
    script.version += 1
    script.updated_at = now
    session.add(
        WorkflowTransitionModel(
            aggregate_type="script",
            aggregate_id=script.id,
            from_stage="VERIFIED",
            to_stage="STORYBOARDING",
            reason=payload.comment,
            actor_id=actor.id,
            correlation_id=request.state.correlation_id,
            occurred_at=now,
        )
    )
    await append_audit(
        session,
        action="script.approved",
        actor_id=actor.id,
        target_type="script_version",
        target_id=str(version.id),
        correlation_id=request.state.correlation_id,
        context={"version": version.version_number, "content_hash": version.content_hash},
    )
    existing_storyboard = await session.scalar(
        select(StoryboardModel.id).where(StoryboardModel.script_id == script.id)
    )
    if existing_storyboard:
        continuation = AutomaticContinuation(
            state="completed",
            action="storyboard_generation",
            message="A storyboard already exists for this approved script.",
        )
    else:
        idempotency_key = f"approval-{version.id.hex}-v{version.version_number}"
        workflow_id = f"storyboard-generation-{idempotency_key}"
        _, reconciled = await _start_tracked(
            session,
            _gateway(request),
            workflow_type="storyboard-generation",
            payload={
                "workflow_id": workflow_id,
                "script_id": str(script.id),
                "script_version_id": str(version.id),
                "sensitivity": "internal",
                "idempotency_key": idempotency_key,
                "actor_id": str(actor.id),
                "correlation_id": request.state.correlation_id,
            },
            actor=actor,
            correlation_id=request.state.correlation_id,
        )
        continuation = AutomaticContinuation(
            state="reconciled" if reconciled else "started",
            action="storyboard_generation",
            workflow_id=workflow_id,
            message="Storyboard generation started automatically from the approved script hash.",
        )
    await append_audit(
        session,
        action=f"automation.script_{continuation.state}",
        actor_id=actor.id,
        target_type="script_version",
        target_id=str(version.id),
        correlation_id=request.state.correlation_id,
        context=continuation.model_dump(mode="json"),
    )
    await session.commit()
    detail = await _script_detail(session, script)
    return detail.model_copy(update={"automatic_continuation": continuation})


async def _storyboard_detail(
    session: AsyncSession, storyboard: StoryboardModel
) -> StoryboardDetailView:
    version = await session.get(StoryboardVersionModel, storyboard.current_version_id)
    if version is None:
        raise HTTPException(status_code=409, detail="Storyboard has no current version")
    rows = (
        await session.execute(
            select(SceneModel, SceneVersionModel)
            .join(SceneVersionModel, SceneVersionModel.id == SceneModel.current_version_id)
            .where(
                SceneModel.storyboard_id == storyboard.id,
                SceneModel.deleted_at.is_(None),
            )
            .order_by(SceneVersionModel.scene_order)
        )
    ).all()
    scenes = [
        {
            "id": str(scene.id),
            "scene_key": scene.scene_key,
            "locked": scene.locked,
            "aggregate_version": scene.version,
            "version": scene_version.version_number,
            "content_hash": scene_version.content_hash,
            "scene_spec": scene_version.scene_spec,
        }
        for scene, scene_version in rows
    ]
    return StoryboardDetailView(
        id=storyboard.id,
        script_id=storyboard.script_id,
        status=storyboard.status,
        version=version.version_number,
        current_version_id=version.id,
        content_hash=version.content_hash,
        scene_count=len(scenes),
        placeholder_tokens=await _storyboard_placeholder_tokens(session, version.id),
        created_at=version.created_at,
        script_version_id=version.script_version_id,
        scenes=scenes,
    )


async def _storyboard_placeholder_tokens(
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
    rows = list(
        await session.scalars(
            select(SceneVersionModel.scene_spec).where(
                SceneVersionModel.storyboard_version_id == storyboard_version_id
            )
        )
    )
    text = _canonical(rows)
    return sorted(set(re.findall(r"\[[A-ZÄÖÜ0-9][A-ZÄÖÜ0-9_.:-]{1,80}\]", text)))


_PLACEHOLDER_TOKEN = re.compile(r"\[[A-ZÄÖÜ0-9][A-ZÄÖÜ0-9_.:-]{1,80}\]")


def _placeholder_tokens(value: Any) -> list[str]:
    return sorted(set(_PLACEHOLDER_TOKEN.findall(_canonical(value))))


def _replace_placeholder_values(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        next_value = value
        for token, replacement in replacements.items():
            next_value = next_value.replace(token, replacement)
        return next_value
    if isinstance(value, list):
        return [_replace_placeholder_values(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_placeholder_values(item, replacements)
            for key, item in value.items()
        }
    return value


def _replace_segment_ids(value: Any, segment_id_map: dict[str, str]) -> Any:
    if isinstance(value, str):
        return segment_id_map.get(value, value)
    if isinstance(value, list):
        return [_replace_segment_ids(item, segment_id_map) for item in value]
    if isinstance(value, dict):
        return {key: _replace_segment_ids(item, segment_id_map) for key, item in value.items()}
    return value


@router.post(
    "/storyboards/{storyboard_id}/placeholders/replace",
    response_model=StoryboardDetailView,
    status_code=201,
)
async def replace_storyboard_placeholders(
    storyboard_id: UUID,
    payload: PlaceholderReplacementWrite,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StoryboardDetailView:
    storyboard = await session.get(StoryboardModel, storyboard_id, with_for_update=True)
    if storyboard is None or storyboard.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Storyboard not found")
    script = await session.get(ScriptModel, storyboard.script_id, with_for_update=True)
    if script is None or script.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Script not found")
    if script.source_kind != "direct_scripted_video":
        raise HTTPException(
            status_code=409,
            detail="Placeholder replacement is only available for direct scripted-video productions",
        )
    parent_board = await session.get(StoryboardVersionModel, storyboard.current_version_id)
    if parent_board is None:
        raise HTTPException(status_code=409, detail="Storyboard has no current version")
    parent_script = await session.get(ScriptVersionModel, parent_board.script_version_id)
    if parent_script is None:
        raise HTTPException(status_code=409, detail="Storyboard script version is missing")
    if script.current_version_id != parent_script.id:
        raise HTTPException(
            status_code=409,
            detail="Storyboard is not based on the current script version; reload before replacing placeholders",
        )
    if (
        parent_script.version_number != payload.expected_script_version
        or parent_script.content_hash != payload.expected_script_hash
    ):
        raise HTTPException(status_code=409, detail="Script version or hash changed; reload before replacing placeholders")
    if (
        parent_board.version_number != payload.expected_storyboard_version
        or parent_board.content_hash != payload.expected_storyboard_hash
    ):
        raise HTTPException(status_code=409, detail="Storyboard version or hash changed; reload before replacing placeholders")
    script_segments = list(
        await session.scalars(
            select(ScriptSegmentModel)
            .where(ScriptSegmentModel.script_version_id == parent_script.id)
            .order_by(ScriptSegmentModel.segment_order)
        )
    )
    scene_rows = (
        await session.execute(
            select(SceneModel, SceneVersionModel)
            .join(SceneVersionModel, SceneVersionModel.id == SceneModel.current_version_id)
            .where(SceneModel.storyboard_id == storyboard.id, SceneModel.deleted_at.is_(None))
            .order_by(SceneVersionModel.scene_order)
        )
    ).all()
    current_tokens = sorted(
        set(_placeholder_tokens([segment.narration for segment in script_segments]))
        | set(_placeholder_tokens([segment.presentation_purpose for segment in script_segments]))
        | set(_placeholder_tokens([segment.citation_display for segment in script_segments]))
        | set(_placeholder_tokens([segment.annotations for segment in script_segments]))
        | set(_placeholder_tokens([scene_version.scene_spec for _, scene_version in scene_rows]))
    )
    if not current_tokens:
        raise HTTPException(status_code=409, detail="No unresolved placeholders are present")
    unknown_tokens = sorted(set(payload.replacements) - set(current_tokens))
    if unknown_tokens:
        raise HTTPException(
            status_code=422,
            detail={"message": "Replacement includes unknown placeholders", "placeholder_tokens": unknown_tokens},
        )
    next_script_number = int(
        await session.scalar(
            select(func.coalesce(func.max(ScriptVersionModel.version_number), 0) + 1)
            .where(ScriptVersionModel.script_id == script.id)
        )
    )
    next_storyboard_number = int(
        await session.scalar(
            select(func.coalesce(func.max(StoryboardVersionModel.version_number), 0) + 1)
            .where(StoryboardVersionModel.storyboard_id == storyboard.id)
        )
    )
    version_id = uuid4()
    board_id = uuid4()
    now = datetime.now(timezone.utc)
    new_segment_documents: list[dict[str, Any]] = []
    segment_id_map: dict[str, str] = {}
    for segment in script_segments:
        annotation_docs = _replace_placeholder_values(deepcopy(segment.annotations), payload.replacements)
        segment_document = {
            "segment_key": segment.segment_key,
            "segment_type": segment.segment_type,
            "narration": _replace_placeholder_values(segment.narration, payload.replacements),
            "presentation_purpose": _replace_placeholder_values(segment.presentation_purpose, payload.replacements),
            "duration_seconds": segment.duration_seconds,
            "citation_display": _replace_placeholder_values(deepcopy(segment.citation_display), payload.replacements),
            "annotations": annotation_docs,
            "locked": segment.locked,
        }
        for annotation in segment_document["annotations"]:
            if isinstance(annotation, dict) and annotation.get("kind") in {"editorial", "opinion"}:
                annotation["text"] = _replace_placeholder_values(str(annotation.get("text", "")), payload.replacements) or segment_document["narration"]
                annotation["start_offset"] = min(int(annotation.get("start_offset", 0) or 0), max(len(segment_document["narration"]) - 1, 0))
                annotation["end_offset"] = max(int(annotation.get("end_offset", 1) or 1), annotation["start_offset"] + 1)
                annotation["end_offset"] = min(annotation["end_offset"], len(segment_document["narration"]))
                if annotation["end_offset"] <= annotation["start_offset"]:
                    annotation["start_offset"] = 0
                    annotation["end_offset"] = max(1, len(segment_document["narration"]))
        new_segment_documents.append(segment_document)
    remaining_script_tokens = _placeholder_tokens(
        {"title": parent_script.title, "segments": new_segment_documents}
    )
    verification_report = {
        **(parent_script.verification_report or {}),
        "valid": True,
        "deterministic_valid": True,
        "requires_independent_verification": False,
        "evidence_required": False,
        "claim_coverage_applicable": False,
        "mode": "direct_scripted_video",
        "source_kind": "direct_scripted_video",
        "coverage_percent": 0,
        "issues": [],
        "placeholder_tokens": remaining_script_tokens,
        "unresolved_placeholders": remaining_script_tokens,
        "edit_comment": payload.comment,
    }
    script_document = {"title": parent_script.title, "segments": new_segment_documents}
    script_hash = hashlib.sha256(_canonical(script_document).encode()).hexdigest()
    session.add(
        ScriptVersionModel(
            id=version_id,
            script_id=script.id,
            version_number=next_script_number,
            status="verified",
            title=parent_script.title,
            total_duration_seconds=sum(float(item["duration_seconds"]) for item in new_segment_documents),
            writer_model_id=parent_script.writer_model_id,
            verifier_model_id=None,
            writer_prompt_id=parent_script.writer_prompt_id,
            verifier_prompt_id=None,
            verification_report=verification_report,
            coverage_percent=0,
            content_hash=script_hash,
            parent_version_id=parent_script.id,
            workflow_id=f"manual-placeholder-replacement-{version_id}",
            correlation_id=request.state.correlation_id,
            created_by=actor.id,
            created_at=now,
        )
    )
    await session.flush()
    for segment, document in zip(script_segments, new_segment_documents, strict=True):
        segment_id = uuid4()
        segment_id_map[str(segment.id)] = str(segment_id)
        session.add(
            ScriptSegmentModel(
                id=segment_id,
                script_version_id=version_id,
                segment_key=document["segment_key"],
                segment_order=segment.segment_order,
                segment_type=document["segment_type"],
                narration=document["narration"],
                presentation_purpose=document["presentation_purpose"],
                duration_seconds=document["duration_seconds"],
                citation_display=document["citation_display"],
                annotations=document["annotations"],
                locked=segment.locked,
                content_hash=hashlib.sha256(_canonical(document).encode()).hexdigest(),
                created_at=now,
            )
        )
    next_specs = [
        _replace_segment_ids(
            _replace_placeholder_values(deepcopy(scene_version.scene_spec), payload.replacements),
            segment_id_map,
        )
        for _, scene_version in scene_rows
    ]
    storyboard_errors = validate_storyboard(next_specs, expected_segment_ids=list(segment_id_map.values()))
    if storyboard_errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "Storyboard coverage rejected", "issues": list(storyboard_errors)},
        )
    remaining_storyboard_tokens = _placeholder_tokens(next_specs)
    board_hash = hashlib.sha256(_canonical({"scenes": next_specs}).encode()).hexdigest()
    session.add(
        StoryboardVersionModel(
            id=board_id,
            storyboard_id=storyboard.id,
            script_version_id=version_id,
            version_number=next_storyboard_number,
            status="in_review",
            content_hash=board_hash,
            parent_version_id=parent_board.id,
            workflow_id=f"manual-placeholder-replacement-{board_id}",
            correlation_id=request.state.correlation_id,
            created_by=actor.id,
            created_at=now,
        )
    )
    await session.flush()
    for (scene, old_version), spec in zip(scene_rows, next_specs, strict=True):
        next_scene_version = SceneVersionModel(
            scene_id=scene.id,
            storyboard_version_id=board_id,
            version_number=old_version.version_number + 1,
            scene_order=spec["order"],
            duration_seconds=spec["duration"],
            visual_type=spec["visual_type"],
            scene_spec=spec,
            content_hash=hashlib.sha256(_canonical(spec).encode()).hexdigest(),
            parent_version_id=old_version.id,
            created_by=actor.id,
            created_at=now,
        )
        session.add(next_scene_version)
        await session.flush()
        scene.current_version_id = next_scene_version.id
        scene.version += 1
        scene.updated_at = now
    script.current_version_id = version_id
    script.status = "verified"
    script.version += 1
    script.updated_at = now
    storyboard.current_version_id = board_id
    storyboard.status = "in_review"
    storyboard.version += 1
    storyboard.updated_at = now
    session.add(
        WorkflowTransitionModel(
            aggregate_type="script",
            aggregate_id=script.id,
            from_stage=parent_script.status.upper(),
            to_stage="VERIFIED",
            reason=payload.comment,
            actor_id=actor.id,
            correlation_id=request.state.correlation_id,
            occurred_at=now,
        )
    )
    await append_audit(
        session,
        action="storyboard.placeholders_replaced",
        actor_id=actor.id,
        target_type="storyboard_version",
        target_id=str(board_id),
        correlation_id=request.state.correlation_id,
        context={
            "script_version_id": str(version_id),
            "parent_script_version_id": str(parent_script.id),
            "parent_storyboard_version_id": str(parent_board.id),
            "replacement_tokens": sorted(payload.replacements),
            "remaining_script_placeholders": remaining_script_tokens,
            "remaining_storyboard_placeholders": remaining_storyboard_tokens,
            "script_hash": script_hash,
            "storyboard_hash": board_hash,
        },
    )
    await session.commit()
    detail = await _storyboard_detail(session, storyboard)
    return detail.model_copy(
        update={
            "automatic_continuation": AutomaticContinuation(
                state="awaiting_input",
                action="review_corrected_versions",
                message="Placeholder values were saved as new immutable script and storyboard versions. Review and approve the corrected hashes before rendering.",
            )
        }
    )


@router.get("/storyboards", response_model=list[StoryboardSummaryView])
async def list_storyboards(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[StoryboardSummaryView]:
    storyboards = list(
        await session.scalars(
            select(StoryboardModel)
            .join(ScriptModel, ScriptModel.id == StoryboardModel.script_id)
            .outerjoin(OpportunityModel, OpportunityModel.id == ScriptModel.opportunity_id)
            .where(
                StoryboardModel.deleted_at.is_(None),
                ScriptModel.deleted_at.is_(None),
                (ScriptModel.source_kind == "direct_scripted_video")
                | (OpportunityModel.deleted_at.is_(None)),
            )
            .order_by(StoryboardModel.created_at.desc())
        )
    )
    output: list[StoryboardSummaryView] = []
    for storyboard in storyboards:
        detail = await _storyboard_detail(session, storyboard)
        output.append(
            StoryboardSummaryView.model_validate(
                detail.model_dump(exclude={"script_version_id", "scenes"})
            )
        )
    return output


@router.get("/storyboards/{storyboard_id}", response_model=StoryboardDetailView)
async def get_storyboard(
    storyboard_id: UUID,
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StoryboardDetailView:
    storyboard = await session.get(StoryboardModel, storyboard_id)
    if storyboard is None or storyboard.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Storyboard not found")
    return await _storyboard_detail(session, storyboard)


@router.put("/storyboards/{storyboard_id}/scenes/{scene_id}/lock", response_model=StoryboardDetailView)
async def set_scene_lock(
    storyboard_id: UUID,
    scene_id: UUID,
    payload: SceneLockWrite,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StoryboardDetailView:
    storyboard = await session.get(StoryboardModel, storyboard_id, with_for_update=True)
    scene = await session.get(SceneModel, scene_id, with_for_update=True)
    if (
        storyboard is None
        or storyboard.deleted_at is not None
        or scene is None
        or scene.deleted_at is not None
        or scene.storyboard_id != storyboard.id
    ):
        raise HTTPException(status_code=404, detail="Storyboard scene not found")
    if scene.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="Scene changed; reload before locking")
    scene.locked = payload.locked
    scene.version += 1
    scene.updated_at = datetime.now(timezone.utc)
    await append_audit(
        session,
        action="scene.locked" if payload.locked else "scene.unlocked",
        actor_id=actor.id,
        target_type="scene",
        target_id=str(scene.id),
        correlation_id=request.state.correlation_id,
        context={"version": scene.version, "comment": payload.comment},
    )
    await session.commit()
    return await _storyboard_detail(session, storyboard)


def _scene_alternative_view(
    item: SceneAlternativeModel, current_scene_version_id: UUID | None
) -> SceneAlternativeView:
    return SceneAlternativeView(
        id=item.id,
        scene_id=item.scene_id,
        base_scene_version_id=item.base_scene_version_id,
        alternative_number=item.alternative_number,
        scene_spec=item.scene_spec,
        content_hash=item.content_hash,
        model_id=item.model_id,
        prompt_template_id=item.prompt_template_id,
        instruction=item.instruction,
        workflow_id=item.workflow_id,
        created_at=item.created_at,
        current_base=item.base_scene_version_id == current_scene_version_id,
    )


@router.get(
    "/storyboards/{storyboard_id}/scenes/{scene_id}/alternatives",
    response_model=list[SceneAlternativeView],
)
async def list_scene_alternatives(
    storyboard_id: UUID,
    scene_id: UUID,
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SceneAlternativeView]:
    scene = await session.get(SceneModel, scene_id)
    if (
        scene is None
        or scene.deleted_at is not None
        or scene.storyboard_id != storyboard_id
    ):
        raise HTTPException(status_code=404, detail="Storyboard scene not found")
    items = list(
        await session.scalars(
            select(SceneAlternativeModel)
            .where(SceneAlternativeModel.scene_id == scene.id)
            .order_by(
                SceneAlternativeModel.created_at.desc(),
                SceneAlternativeModel.alternative_number.desc(),
            )
        )
    )
    return [_scene_alternative_view(item, scene.current_version_id) for item in items]


@router.post(
    "/storyboards/{storyboard_id}/scenes/{scene_id}/alternative-runs",
    response_model=ResearchWorkflowView,
    status_code=202,
)
async def start_scene_alternative_run(
    storyboard_id: UUID,
    scene_id: UUID,
    payload: SceneAlternativeGenerationStart,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    storyboard = await session.get(StoryboardModel, storyboard_id)
    scene = await session.get(SceneModel, scene_id)
    if (
        storyboard is None
        or storyboard.deleted_at is not None
        or scene is None
        or scene.deleted_at is not None
        or scene.storyboard_id != storyboard.id
    ):
        raise HTTPException(status_code=404, detail="Storyboard scene not found")
    board_version = await session.get(StoryboardVersionModel, storyboard.current_version_id)
    scene_version = await session.get(SceneVersionModel, scene.current_version_id)
    if board_version is None or scene_version is None:
        raise HTTPException(status_code=409, detail="Storyboard scene has no current version")
    if (
        board_version.version_number != payload.expected_storyboard_version
        or scene_version.version_number != payload.expected_scene_version
    ):
        raise HTTPException(status_code=409, detail="Scene or storyboard changed; reload first")
    if scene.locked:
        raise HTTPException(status_code=409, detail="Locked scenes cannot generate alternatives")
    workflow_id = f"scene-alternative-generation-{payload.idempotency_key}"
    gateway = _gateway(request)
    workflow_payload = {
        "workflow_id": workflow_id,
        "storyboard_id": str(storyboard.id),
        "scene_id": str(scene.id),
        "expected_storyboard_version": board_version.version_number,
        "expected_scene_version": scene_version.version_number,
        "instruction": payload.instruction,
        "sensitivity": payload.sensitivity,
        "idempotency_key": payload.idempotency_key,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    _, reconciled = await _start_tracked(
        session,
        gateway,
        workflow_type="scene-alternative-generation",
        payload=workflow_payload,
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action=(
            "scene.alternative_generation_reconciled"
            if reconciled
            else "scene.alternative_generation_started"
        ),
        actor_id=actor.id,
        target_type="scene_version",
        target_id=str(scene_version.id),
        correlation_id=request.state.correlation_id,
        context={
            "workflow_id": workflow_id,
            "storyboard_version": board_version.version_number,
            "scene_version": scene_version.version_number,
            "instruction": payload.instruction,
        },
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post(
    "/storyboards/{storyboard_id}/scenes/{scene_id}/versions",
    response_model=StoryboardDetailView,
    status_code=201,
)
async def edit_scene_version(
    storyboard_id: UUID,
    scene_id: UUID,
    payload: SceneEdit,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StoryboardDetailView:
    storyboard = await session.get(StoryboardModel, storyboard_id, with_for_update=True)
    scene = await session.get(SceneModel, scene_id, with_for_update=True)
    if (
        storyboard is None
        or storyboard.deleted_at is not None
        or scene is None
        or scene.deleted_at is not None
        or scene.storyboard_id != storyboard.id
    ):
        raise HTTPException(status_code=404, detail="Storyboard scene not found")
    if scene.locked:
        raise HTTPException(status_code=409, detail="Locked scenes cannot be edited or regenerated")
    parent_board = await session.get(StoryboardVersionModel, storyboard.current_version_id)
    current_scene = await session.get(SceneVersionModel, scene.current_version_id)
    if parent_board is None or current_scene is None:
        raise HTTPException(status_code=409, detail="Storyboard version is incomplete")
    if (
        parent_board.version_number != payload.expected_storyboard_version
        or current_scene.version_number != payload.expected_scene_version
    ):
        raise HTTPException(status_code=409, detail="Scene or storyboard changed; reload before editing")
    spec = payload.scene_spec
    errors = list(validate_scene_spec(spec))
    if str(spec.get("scene_id")) != str(scene.id):
        errors.append("scene_id cannot change across scene versions")
    if spec.get("order") != current_scene.scene_order:
        errors.append("scene order cannot change through a single-scene edit")
    referenced_segments: set[UUID] = set()
    for value in spec.get("narration_segment_ids", []):
        try:
            referenced_segments.add(UUID(str(value)))
        except ValueError:
            errors.append("narration segment IDs must be UUIDs")
    segment_rows = list(
        await session.scalars(
            select(ScriptSegmentModel).where(
                ScriptSegmentModel.id.in_(referenced_segments),
                ScriptSegmentModel.script_version_id == parent_board.script_version_id,
            )
        )
    )
    if len(segment_rows) != len(referenced_segments) or not referenced_segments:
        errors.append("scene must reference known segments from the exact storyboard script version")
    link_rows = (
        await session.execute(
            select(SegmentClaimModel, SourceSnapshotModel.source_document_id)
            .outerjoin(
                EvidenceExcerptModel,
                EvidenceExcerptModel.id == SegmentClaimModel.evidence_excerpt_id,
            )
            .outerjoin(
                SourceSnapshotModel,
                SourceSnapshotModel.id == EvidenceExcerptModel.source_snapshot_id,
            )
            .where(SegmentClaimModel.script_segment_id.in_(referenced_segments))
        )
    ).all()
    allowed_claims = {str(link.claim_id) for link, _ in link_rows}
    allowed_sources = {str(source_id) for _, source_id in link_rows if source_id is not None}
    if not set(spec.get("claim_ids", [])) <= allowed_claims:
        errors.append("scene claim is not linked to its narration segments")
    if not set(spec.get("source_ids", [])) <= allowed_sources:
        errors.append("scene source is not linked to its narration segments")
    if spec.get("claim_ids") and not spec.get("source_ids"):
        errors.append("claim-bearing scenes must expose at least one linked source")
    if errors:
        raise HTTPException(status_code=422, detail={"message": "SceneSpec rejected", "issues": errors[:20]})
    current_rows = (
        await session.execute(
            select(SceneModel, SceneVersionModel)
            .join(SceneVersionModel, SceneVersionModel.id == SceneModel.current_version_id)
            .where(SceneModel.storyboard_id == storyboard.id, SceneModel.deleted_at.is_(None))
            .order_by(SceneVersionModel.scene_order)
        )
    ).all()
    next_specs = [spec if item.id == scene.id else version.scene_spec for item, version in current_rows]
    expected_segment_ids = list(
        await session.scalars(
            select(ScriptSegmentModel.id).where(
                ScriptSegmentModel.script_version_id == parent_board.script_version_id
            )
        )
    )
    storyboard_errors = validate_storyboard(
        next_specs, expected_segment_ids=[str(value) for value in expected_segment_ids]
    )
    if storyboard_errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "Storyboard coverage rejected", "issues": list(storyboard_errors)},
        )
    next_board_id = uuid4()
    next_board_number = parent_board.version_number + 1
    now = datetime.now(timezone.utc)
    board_hash = hashlib.sha256(_canonical({"scenes": next_specs}).encode()).hexdigest()
    session.add(
        StoryboardVersionModel(
            id=next_board_id,
            storyboard_id=storyboard.id,
            script_version_id=parent_board.script_version_id,
            version_number=next_board_number,
            status="in_review",
            content_hash=board_hash,
            parent_version_id=parent_board.id,
            workflow_id=f"manual-scene-edit-{next_board_id}",
            correlation_id=request.state.correlation_id,
            created_by=actor.id,
            created_at=now,
        )
    )
    await session.flush()
    for item, old_version in current_rows:
        next_spec = spec if item.id == scene.id else old_version.scene_spec
        next_scene_version = SceneVersionModel(
            scene_id=item.id,
            storyboard_version_id=next_board_id,
            version_number=old_version.version_number + 1,
            scene_order=next_spec["order"],
            duration_seconds=next_spec["duration"],
            visual_type=next_spec["visual_type"],
            scene_spec=next_spec,
            content_hash=hashlib.sha256(_canonical(next_spec).encode()).hexdigest(),
            parent_version_id=old_version.id,
            created_by=actor.id,
            created_at=now,
        )
        session.add(next_scene_version)
        await session.flush()
        item.current_version_id = next_scene_version.id
        item.version += 1
        item.updated_at = now
    storyboard.current_version_id = next_board_id
    storyboard.status = "in_review"
    storyboard.version += 1
    storyboard.updated_at = now
    await append_audit(
        session,
        action="storyboard.scene_version_created",
        actor_id=actor.id,
        target_type="storyboard_version",
        target_id=str(next_board_id),
        correlation_id=request.state.correlation_id,
        context={
            "scene_id": str(scene.id),
            "version": next_board_number,
            "parent_version_id": str(parent_board.id),
            "content_hash": board_hash,
            "comment": payload.comment,
        },
    )
    await session.commit()
    return await _storyboard_detail(session, storyboard)


@router.post(
    "/storyboards/{storyboard_id}/scenes/{scene_id}/alternatives/{alternative_id}/select",
    response_model=StoryboardDetailView,
    status_code=201,
)
async def select_scene_alternative(
    storyboard_id: UUID,
    scene_id: UUID,
    alternative_id: UUID,
    payload: SceneAlternativeSelect,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StoryboardDetailView:
    alternative = await session.get(SceneAlternativeModel, alternative_id)
    scene = await session.get(SceneModel, scene_id)
    if (
        alternative is None
        or alternative.scene_id != scene_id
        or scene is None
        or scene.deleted_at is not None
        or scene.storyboard_id != storyboard_id
    ):
        raise HTTPException(status_code=404, detail="Scene alternative not found")
    if alternative.base_scene_version_id != scene.current_version_id:
        raise HTTPException(
            status_code=409,
            detail="Alternative was generated from an older scene version; generate a new candidate",
        )
    detail = await edit_scene_version(
        storyboard_id,
        scene_id,
        SceneEdit(
            expected_scene_version=payload.expected_scene_version,
            expected_storyboard_version=payload.expected_storyboard_version,
            scene_spec=alternative.scene_spec,
            comment=payload.comment,
        ),
        request,
        actor,
        session,
    )
    await append_audit(
        session,
        action="scene.alternative_selected",
        actor_id=actor.id,
        target_type="scene_alternative",
        target_id=str(alternative.id),
        correlation_id=request.state.correlation_id,
        context={
            "storyboard_id": str(storyboard_id),
            "scene_id": str(scene_id),
            "base_scene_version_id": str(alternative.base_scene_version_id),
            "alternative_number": alternative.alternative_number,
            "content_hash": alternative.content_hash,
            "resulting_storyboard_version": detail.version,
            "comment": payload.comment,
        },
    )
    await session.commit()
    return detail


@router.post("/storyboards/{storyboard_id}/approve", response_model=StoryboardDetailView)
async def approve_storyboard(
    storyboard_id: UUID,
    payload: EditorialApprovalWrite,
    request: Request,
    actor: Reviewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StoryboardDetailView:
    storyboard = await session.get(StoryboardModel, storyboard_id, with_for_update=True)
    if storyboard is None or storyboard.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Storyboard not found")
    version = await session.get(StoryboardVersionModel, storyboard.current_version_id)
    if version is None or storyboard.status not in {"in_review", "approved"}:
        raise HTTPException(status_code=409, detail="Storyboard is not reviewable")
    if version.version_number != payload.expected_version or version.content_hash != payload.expected_hash:
        raise HTTPException(status_code=409, detail="Storyboard version or hash changed; reload before approval")
    script_record = await session.get(ScriptModel, storyboard.script_id)
    if (
        script_record is None
        or script_record.deleted_at is not None
        or script_record.current_version_id != version.script_version_id
        or script_record.status != "approved"
    ):
        raise HTTPException(
            status_code=409,
            detail="Approve the exact current script hash before approving this storyboard",
        )
    if storyboard.status == "approved":
        return await _storyboard_detail(session, storyboard)
    now = datetime.now(timezone.utc)
    scene_count = await session.scalar(
        select(func.count(SceneModel.id)).where(SceneModel.storyboard_id == storyboard.id)
    )
    session.add(
        ApprovalModel(
            target_type="storyboard_version",
            target_id=version.id,
            target_version=version.version_number,
            target_hash=version.content_hash,
            decision="approved",
            comment=payload.comment,
            policy_snapshot={"scene_count": scene_count},
            supersedes_approval_id=None,
            actor_id=actor.id,
            correlation_id=request.state.correlation_id,
            created_at=now,
        )
    )
    storyboard.status = "approved"
    storyboard.version += 1
    storyboard.updated_at = now
    await append_audit(
        session,
        action="storyboard.approved",
        actor_id=actor.id,
        target_type="storyboard_version",
        target_id=str(version.id),
        correlation_id=request.state.correlation_id,
        context={"version": version.version_number, "content_hash": version.content_hash},
    )
    existing_production = await session.scalar(
        select(MediaProductionModel.id).where(
            MediaProductionModel.storyboard_version_id == version.id,
            MediaProductionModel.state.notin_({"failed", "cancelled"}),
        )
    )
    if existing_production:
        continuation = AutomaticContinuation(
            state="completed",
            action="media_production",
            message="Media production already exists for this approved storyboard version.",
        )
    else:
        unresolved = await _storyboard_placeholder_tokens(session, version.id)
        if unresolved:
            continuation = AutomaticContinuation(
                state="awaiting_input",
                action="media_production",
                message=(
                    "Storyboard approval is recorded, but media production is blocked until "
                    f"unresolved placeholders are replaced: {', '.join(unresolved[:8])}."
                ),
            )
        else:
            if script_record and script_record.production_brief_id:
                channel = await session.scalar(
                    select(ChannelProfileModel)
                    .join(
                        ProductionBriefModel,
                        ProductionBriefModel.channel_profile_id == ChannelProfileModel.id,
                    )
                    .where(ProductionBriefModel.id == script_record.production_brief_id)
                )
            elif script_record and script_record.opportunity_id:
                channel = await session.scalar(
                    select(ChannelProfileModel)
                    .join(
                        SubjectProfileModel,
                        SubjectProfileModel.channel_profile_id == ChannelProfileModel.id,
                    )
                    .join(
                        OpportunityModel,
                        OpportunityModel.subject_profile_id == SubjectProfileModel.id,
                    )
                    .where(OpportunityModel.id == script_record.opportunity_id)
                )
            else:
                channel = None
            render_settings = channel.default_render_settings if channel else {}
            workflow_key = str(render_settings.get("workflow_key", ""))
            workflow_statement = (
                select(ComfyWorkflowVersionModel)
                .join(
                    ComfyWorkflowHeadModel,
                    ComfyWorkflowHeadModel.active_version_id == ComfyWorkflowVersionModel.id,
                )
                .where(ComfyWorkflowVersionModel.approval_state == "approved")
                .order_by(ComfyWorkflowVersionModel.workflow_key)
            )
            if workflow_key:
                workflow_statement = workflow_statement.where(
                    ComfyWorkflowVersionModel.workflow_key == workflow_key
                )
            comfy_workflow = await session.scalar(workflow_statement.limit(1))
            voice_key = str(render_settings.get("voice_profile_key", ""))
            voice_statement = (
                select(VoiceProfileVersionModel)
                .join(
                    VoiceProfileHeadModel,
                    VoiceProfileHeadModel.active_version_id == VoiceProfileVersionModel.id,
                )
                .where(VoiceProfileVersionModel.enabled.is_(True))
                .order_by(VoiceProfileVersionModel.profile_key)
            )
            if voice_key:
                voice_statement = voice_statement.where(
                    VoiceProfileVersionModel.profile_key == voice_key
                )
            voice = await session.scalar(voice_statement.limit(1))
            if comfy_workflow is None or voice is None:
                continuation = AutomaticContinuation(
                    state="awaiting_input",
                    action="media_production",
                    message="Storyboard approval is recorded, but an active approved visual workflow and enabled voice profile are required.",
                )
            else:
                render_tier = str(render_settings.get("render_tier", "preview"))
                resolution_key = (
                    "resolution"
                    if render_tier == "full"
                    else "preview_resolution"
                )
                configured_resolution = str(render_settings.get(resolution_key, ""))
                try:
                    width_text, height_text = configured_resolution.lower().split("x", 1)
                    width, height = int(width_text), int(height_text)
                except (AttributeError, TypeError, ValueError):
                    width, height = (1920, 1080) if render_tier == "full" else (854, 480)
                allowed = comfy_workflow.allowed_resolutions or []
                if {"width": width, "height": height} not in allowed and allowed:
                    width, height = int(allowed[0]["width"]), int(allowed[0]["height"])
                try:
                    fps = int(
                        render_settings.get(
                            "fps" if render_tier == "full" else "preview_fps",
                            24 if render_tier == "full" else 12,
                        )
                    )
                except (TypeError, ValueError):
                    fps = 24 if render_tier == "full" else 12
                idempotency_key = f"approval-{version.id.hex}-v{version.version_number}"
                media_workflow_type = str(
                    render_settings.get("media_workflow_type", "narration_timeline")
                )
                if media_workflow_type == "legacy_one_step":
                    from youtuber_api.routers.media import start_production

                    run = await start_production(
                        MediaProductionStart(
                            storyboard_version_id=version.id,
                            expected_storyboard_hash=version.content_hash,
                            render_tier=render_tier,
                            workflow_key=comfy_workflow.workflow_key,
                            voice_profile_key=voice.profile_key,
                            width=width,
                            height=height,
                            fps=fps,
                            idempotency_key=idempotency_key,
                        ),
                        request,
                        actor,
                        session,
                    )
                    continuation = AutomaticContinuation(
                        state="started",
                        action="media_production",
                        workflow_id=run.workflow_id,
                        message="Legacy one-step media production started from the approved storyboard hash because the channel render settings explicitly selected legacy_one_step.",
                    )
                else:
                    from youtuber_api.routers.media import start_timeline_draft

                    run = await start_timeline_draft(
                        MediaTimelineDraftStart(
                            storyboard_version_id=version.id,
                            expected_storyboard_hash=version.content_hash,
                            render_tier=render_tier,
                            workflow_key=comfy_workflow.workflow_key,
                            voice_profile_key=voice.profile_key,
                            width=width,
                            height=height,
                            fps=fps,
                            idempotency_key=idempotency_key,
                        ),
                        request,
                        actor,
                        session,
                    )
                    continuation = AutomaticContinuation(
                        state="started",
                        action="media_timeline_draft",
                        workflow_id=run.workflow_id,
                        message="Narration-first timeline generation started from the approved storyboard hash. Review the timeline before rendering.",
                    )
    await append_audit(
        session,
        action=f"automation.storyboard_{continuation.state}",
        actor_id=actor.id,
        target_type="storyboard_version",
        target_id=str(version.id),
        correlation_id=request.state.correlation_id,
        context=continuation.model_dump(mode="json"),
    )
    await session.commit()
    detail = await _storyboard_detail(session, storyboard)
    return detail.model_copy(update={"automatic_continuation": continuation})
