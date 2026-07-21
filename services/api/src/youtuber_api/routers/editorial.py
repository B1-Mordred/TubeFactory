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
from youtuber_api.audit import append_audit
from youtuber_api.db import get_session
from youtuber_api.editorial_gateway import TemporalEditorialGateway
from youtuber_api.models import (
    ApprovalModel,
    ClaimEvidenceModel,
    ClaimModel,
    EvidenceExcerptModel,
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
    UserModel,
    WorkflowControlRecordModel,
    WorkflowTransitionModel,
)
from youtuber_api.schemas import (
    EditorialApprovalWrite,
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
    "script-generation",
    "script-regeneration",
    "script-verification",
    "scene-alternative-generation",
    "storyboard-generation",
    "media-production",
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
    return ScriptDetailView(
        id=script.id,
        dossier_id=script.research_dossier_id,
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


@router.get("/scripts", response_model=list[ScriptSummaryView])
async def list_scripts(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[ScriptSummaryView]:
    scripts = list(
        await session.scalars(
            select(ScriptModel)
            .where(ScriptModel.deleted_at.is_(None))
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
    report = verify_script_draft(
        _edit_core_segments(payload),
        approved_claim_ids=approved_claim_ids,
        central_claim_ids=central_claim_ids,
        evidence_text_by_id=evidence_text,
        evidence_claim_ids_by_id=evidence_claims,
    )
    next_number = parent.version_number + 1
    version_id = uuid4()
    now = datetime.now(timezone.utc)
    document = {
        "title": payload.title,
        "segments": [item.model_dump(mode="json") for item in payload.segments],
    }
    content_hash = hashlib.sha256(_canonical(document).encode()).hexdigest()
    status = "draft" if report.valid else "blocked"
    verification_report = {
        "valid": False,
        "deterministic_valid": report.valid,
        "requires_independent_verification": report.valid,
        "issues": [item.__dict__ for item in report.issues],
        "edit_comment": payload.comment,
    }
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
            coverage_percent=report.coverage_percent,
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
            "deterministic_valid": report.valid,
            "requires_independent_verification": report.valid,
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
    await session.commit()
    return await _script_detail(session, script)


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
        created_at=version.created_at,
        script_version_id=version.script_version_id,
        scenes=scenes,
    )


@router.get("/storyboards", response_model=list[StoryboardSummaryView])
async def list_storyboards(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[StoryboardSummaryView]:
    storyboards = list(
        await session.scalars(
            select(StoryboardModel)
            .where(StoryboardModel.deleted_at.is_(None))
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
    await session.commit()
    return await _storyboard_detail(session, storyboard)
