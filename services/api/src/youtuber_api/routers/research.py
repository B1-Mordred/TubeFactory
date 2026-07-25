from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.service import RPCError

from editorial_core.authorization import Permission
from editorial_core.explanation_readiness import evaluate_explanation_readiness, explanation_policy
from editorial_core.operating_policy import evaluate_operating_policy
from editorial_core.workflow import ProductionStage, validate_transition
from youtuber_api.audit import append_audit
from youtuber_api.config import get_settings
from youtuber_api.db import get_session
from youtuber_api.editorial_gateway import TemporalEditorialGateway
from youtuber_api.models import (
    ClaimEvidenceModel,
    ClaimModel,
    EvidenceExcerptModel,
    OpportunityModel,
    OpportunityAIQualificationModel,
    OpportunitySourceModel,
    OpportunityScoreModel,
    ResearchDossierModel,
    DossierAIAssessmentModel,
    ResearchRunModel,
    SemanticChunkModel,
    ScriptModel,
    SourceDocumentModel,
    SourceRelationshipModel,
    SourceSnapshotModel,
    SubjectProfileModel,
    UserModel,
    WorkflowControlRecordModel,
    WorkflowTransitionModel,
)
from youtuber_api.research_gateway import TemporalResearchGateway
from youtuber_api.schemas import (
    AllOpportunityArchiveView,
    AllOpportunityArchiveWrite,
    AutomaticContinuation,
    ArchivedOpportunityListItem,
    ClaimLedgerItem,
    DossierDetail,
    DossierListItem,
    FixtureResearchStart,
    LiveDiscoveryStart,
    ManualDossierWrite,
    ManualOpportunityWrite,
    OpportunityAcquisitionStart,
    OpportunityDecisionView,
    OpportunityDecisionWrite,
    OpportunityListItem,
    OpportunityResearchStart,
    ResearchWorkflowView,
    ResearchWorkflowCancel,
    ResearchWorkflowLogEntry,
    ResearchWorkflowRetry,
    ReviewDecision,
    ScoredOpportunityArchiveView,
    ScoredOpportunityArchiveWrite,
    SourceBrowserItem,
    SourceIndexStart,
    SourcePreview,
    SourceRelationshipView,
    SourceRelationshipWrite,
    SourceSnapshotView,
)
from youtuber_api.security import require

router = APIRouter(prefix="/research", tags=["research"])
Viewer = Annotated[UserModel, Depends(require(Permission.VIEW))]
Operator = Annotated[UserModel, Depends(require(Permission.OPERATE_WORKFLOWS))]
Reviewer = Annotated[UserModel, Depends(require(Permission.REVIEW))]
Editor = Annotated[UserModel, Depends(require(Permission.EDIT_EDITORIAL))]
_WORKFLOW_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]{1,240}$")
_TERMINAL_EXECUTION_STATUSES = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TERMINATED",
    "TIMED_OUT",
}


def _gateway(request: Request) -> TemporalResearchGateway:
    return TemporalResearchGateway(request.app.state.temporal_client)


def _subject_policy(subject: SubjectProfileModel) -> dict[str, object]:
    profile = subject.approval_profile or {}
    return evaluate_operating_policy(
        mode=str(profile.get("mode", "assisted")),
        risk=subject.risk,
        sensitive_topics=profile.get("sensitive_topics", []),
    ).as_dict()


def _validated_workflow_id(workflow_id: str) -> str:
    if not _WORKFLOW_ID_PATTERN.fullmatch(workflow_id):
        raise HTTPException(status_code=422, detail="Invalid workflow ID")
    return workflow_id


def _eligible_evidence_source() -> Any:
    """Hide automatically rejected legacy enrichment noise from evidence surfaces."""

    return ~and_(
        SourceDocumentModel.reputation["discovered_via"].as_string()
        == "generic_evidence_search",
        func.coalesce(
            SourceDocumentModel.reputation["relevance_score"].as_string(), ""
        )
        == "",
    )


def _dossier_list_item(
    dossier: ResearchDossierModel,
    continuation: AutomaticContinuation | None = None,
) -> DossierListItem:
    return DossierListItem(
        id=dossier.id,
        opportunity_id=dossier.opportunity_id,
        dossier_version=dossier.dossier_version,
        version=dossier.version,
        status=dossier.status,
        executive_summary=dossier.executive_summary,
        completion_evaluation=dossier.completion_evaluation,
        created_at=dossier.created_at,
        automatic_continuation=continuation,
    )


def _manual_explanation_plan(
    opportunity: OpportunityModel,
    payload: ManualDossierWrite,
    *,
    minimum_units: int,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(payload.explanation_plan, start=1):
        label = str(item.get("label") or item.get("question") or item.get("id") or "").strip()
        if not label:
            continue
        unit_id = str(item.get("id") or f"manual-{index}").strip()
        normalized.append(
            {
                **item,
                "id": unit_id,
                "label": label,
                "role": str(item.get("role") or "manual_review"),
                "question": str(item.get("question") or label),
                "essential": bool(item.get("essential", True)),
            }
        )
    if normalized:
        return normalized

    seeds = [
        (
            "hook",
            "Why this topic matters",
            opportunity.summary,
            "orientation",
        ),
        (
            "basics",
            "Core terms and mechanism",
            payload.safe_conclusions[0],
            "explanation",
        ),
        (
            "evidence",
            "What the sources support",
            payload.safe_conclusions[min(1, len(payload.safe_conclusions) - 1)],
            "evidence",
        ),
        (
            "limits",
            "Limits and unresolved questions",
            payload.unresolved_questions[0] if payload.unresolved_questions else "Which claims remain uncertain?",
            "limits",
        ),
        (
            "context",
            "Alternative explanations and context",
            payload.alternative_explanations[0] if payload.alternative_explanations else "What context changes the interpretation?",
            "context",
        ),
        (
            "takeaway",
            "Safe conclusion",
            payload.safe_conclusions[-1],
            "takeaway",
        ),
    ]
    while len(seeds) < minimum_units:
        number = len(seeds) + 1
        seeds.append(
            (
                f"supporting-point-{number}",
                f"Supporting explanation point {number}",
                "Which sourced detail helps the audience understand the topic?",
                "support",
            )
        )
    return [
        {"id": unit_id, "label": label, "question": question, "role": role, "essential": True}
        for unit_id, label, question, role in seeds[: max(minimum_units, len(seeds))]
    ]


async def _start_tracked_workflow(
    session: AsyncSession,
    gateway: TemporalResearchGateway,
    *,
    workflow_type: str,
    workflow_request: dict[str, Any],
    actor: UserModel,
    correlation_id: str,
    parent_workflow_id: str | None = None,
) -> tuple[str, bool]:
    workflow_id = str(workflow_request["workflow_id"])
    existing = await session.get(WorkflowControlRecordModel, workflow_id)
    if existing is not None:
        existing_business_request = {
            key: value
            for key, value in existing.request_payload.items()
            if key not in {"actor_id", "correlation_id"}
        }
        proposed_business_request = {
            key: value
            for key, value in workflow_request.items()
            if key not in {"actor_id", "correlation_id"}
        }
        if (
            existing.workflow_type != workflow_type
            or existing_business_request != proposed_business_request
            or existing.parent_workflow_id != parent_workflow_id
        ):
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
            request_payload=workflow_request,
            parent_workflow_id=parent_workflow_id,
            correlation_id=correlation_id,
            started_by=actor.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.flush()
    await gateway.start(workflow_type, workflow_request)
    return workflow_id, False


async def _workflow_view(
    session: AsyncSession,
    gateway: TemporalResearchGateway,
    workflow_id: str,
) -> ResearchWorkflowView:
    status = await gateway.describe(workflow_id)
    record = await session.get(WorkflowControlRecordModel, workflow_id)
    status["correlation_id"] = record.correlation_id if record else None
    return ResearchWorkflowView.model_validate(status)


def _read_normalized_preview(client: Any, bucket: str, key: str, limit: int) -> tuple[str, bool]:
    response = client.get_object(bucket, key)
    try:
        body = response.read(limit + 1)
    finally:
        response.close()
        response.release_conn()
    return body[:limit].decode("utf-8", errors="replace"), len(body) > limit


@router.post("/fixture-runs", response_model=ResearchWorkflowView, status_code=202)
async def start_fixture_run(
    payload: FixtureResearchStart,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    subject = await session.get(SubjectProfileModel, payload.subject_profile_id)
    if subject is None or subject.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Subject profile not found")
    if not subject.enabled:
        raise HTTPException(status_code=409, detail="Enable the subject profile before starting research")
    workflow_id = f"fixture-research-{payload.idempotency_key}"
    gateway = _gateway(request)
    await _start_tracked_workflow(
        session,
        gateway,
        workflow_type="fixture-research",
        workflow_request={
            "workflow_id": workflow_id,
            "idempotency_key": payload.idempotency_key,
            "subject_profile_id": str(subject.id),
            "actor_id": str(actor.id),
            "correlation_id": request.state.correlation_id,
        },
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action="research.fixture_started",
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=workflow_id,
        correlation_id=request.state.correlation_id,
        context={"subject_profile_id": str(subject.id), "idempotency_key": payload.idempotency_key},
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post("/dossier-runs", response_model=ResearchWorkflowView, status_code=202)
async def start_live_research_run(
    payload: OpportunityResearchStart,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    opportunity = await session.get(OpportunityModel, payload.opportunity_id)
    if opportunity is None or opportunity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    if opportunity.decision != "approved":
        raise HTTPException(status_code=409, detail="Opportunity approval is required")
    workflow_id = f"live-research-dossier-{payload.idempotency_key}"
    gateway = _gateway(request)
    workflow_request = {
        "workflow_id": workflow_id,
        "idempotency_key": payload.idempotency_key,
        "opportunity_id": str(opportunity.id),
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    try:
        existing = await gateway.describe(workflow_id)
    except RPCError:
        existing = None
    if existing is not None:
        await _start_tracked_workflow(
            session,
            gateway,
            workflow_type="live-research-dossier",
            workflow_request=workflow_request,
            actor=actor,
            correlation_id=request.state.correlation_id,
        )
        await append_audit(
            session,
            action="research.live_dossier_start_reconciled",
            actor_id=actor.id,
            target_type="temporal_workflow",
            target_id=workflow_id,
            correlation_id=request.state.correlation_id,
            context={"opportunity_id": str(opportunity.id)},
        )
        await session.commit()
        return await _workflow_view(session, gateway, workflow_id)
    latest_run = await session.scalar(
        select(ResearchRunModel)
        .where(
            ResearchRunModel.opportunity_id == opportunity.id,
            ResearchRunModel.deleted_at.is_(None),
        )
        .order_by(ResearchRunModel.created_at.desc())
        .limit(1)
    )
    snapshot_count = await session.scalar(
        select(func.count(func.distinct(SourceSnapshotModel.id)))
        .select_from(OpportunitySourceModel)
        .join(
            SourceSnapshotModel,
            SourceSnapshotModel.source_document_id == OpportunitySourceModel.source_document_id,
        )
        .where(OpportunitySourceModel.opportunity_id == opportunity.id)
    )
    if not snapshot_count:
        raise HTTPException(status_code=409, detail="No immutable source snapshots are available")
    if latest_run is None:
        raise HTTPException(
            status_code=409,
            detail="Complete approved source acquisition before building the dossier",
        )
    if latest_run.state != ProductionStage.RESEARCHING.value:
        if latest_run.state not in {
            "INSUFFICIENT_EXPLANATION_EVIDENCE",
            "DOSSIER_REVIEW",
        }:
            raise HTTPException(
                status_code=409,
                detail=f"Research cannot be restarted from state {latest_run.state}",
            )
        now = datetime.now(timezone.utc)
        revision_run = ResearchRunModel(
            opportunity_id=opportunity.id,
            workflow_id=f"research-revision-{payload.idempotency_key}",
            state=ProductionStage.RESEARCHING.value,
            research_plan={
                **(latest_run.research_plan or {}),
                "revision_of_research_run_id": str(latest_run.id),
                "reason": "automatic explanation-readiness optimization",
            },
            progress={
                "percent": 25,
                "sources_acquired": int(snapshot_count),
                "revision_of_research_run_id": str(latest_run.id),
            },
            correlation_id=request.state.correlation_id,
            started_by=actor.id,
            completed_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(revision_run)
        await session.flush()
        session.add(
            WorkflowTransitionModel(
                aggregate_type="opportunity",
                aggregate_id=opportunity.id,
                from_stage="PAUSED" if latest_run.state == "INSUFFICIENT_EXPLANATION_EVIDENCE" else "DOSSIER_REVIEW",
                to_stage=ProductionStage.RESEARCHING.value,
                reason="automatic evidence enrichment revision started",
                actor_id=actor.id,
                correlation_id=request.state.correlation_id,
                occurred_at=now,
            )
        )
        latest_run = revision_run
    await _start_tracked_workflow(
        session,
        gateway,
        workflow_type="live-research-dossier",
        workflow_request=workflow_request,
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action="research.live_dossier_started",
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=workflow_id,
        correlation_id=request.state.correlation_id,
        context={
            "opportunity_id": str(opportunity.id),
            "research_run_id": str(latest_run.id),
            "snapshot_count": snapshot_count,
            "revision": bool(latest_run.research_plan.get("revision_of_research_run_id")),
        },
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post("/live-discovery-runs", response_model=ResearchWorkflowView, status_code=202)
async def start_live_discovery_run(
    payload: LiveDiscoveryStart,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    subject = await session.get(SubjectProfileModel, payload.subject_profile_id)
    if subject is None or subject.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Subject profile not found")
    if not subject.enabled:
        raise HTTPException(status_code=409, detail="Enable the subject profile before starting discovery")
    workflow_id = f"live-discovery-{payload.idempotency_key}"
    gateway = _gateway(request)
    await _start_tracked_workflow(
        session,
        gateway,
        workflow_type="live-discovery",
        workflow_request={
            "workflow_id": workflow_id,
            "idempotency_key": payload.idempotency_key,
            "subject_profile_id": str(subject.id),
            "actor_id": str(actor.id),
            "correlation_id": request.state.correlation_id,
        },
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action="research.live_discovery_started",
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=workflow_id,
        correlation_id=request.state.correlation_id,
        context={"subject_profile_id": str(subject.id), "idempotency_key": payload.idempotency_key},
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post("/acquisition-runs", response_model=ResearchWorkflowView, status_code=202)
async def start_acquisition_run(
    payload: OpportunityAcquisitionStart,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    opportunity = await session.get(OpportunityModel, payload.opportunity_id)
    if opportunity is None or opportunity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    if opportunity.decision != "approved":
        raise HTTPException(
            status_code=409, detail="Opportunity must be approved before source acquisition"
        )
    source_count = await session.scalar(
        select(func.count(OpportunitySourceModel.id)).where(
            OpportunitySourceModel.opportunity_id == opportunity.id
        )
    )
    bootstrap_sources = not bool(source_count)
    workflow_id = f"source-acquisition-{payload.idempotency_key}"
    workflow_request = {
        "workflow_id": workflow_id,
        "idempotency_key": payload.idempotency_key,
        "opportunity_id": str(opportunity.id),
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    if bootstrap_sources:
        workflow_request["bootstrap_sources"] = True
    gateway = _gateway(request)
    await _start_tracked_workflow(
        session,
        gateway,
        workflow_type="source-acquisition",
        workflow_request=workflow_request,
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action="research.source_acquisition_started",
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=workflow_id,
        correlation_id=request.state.correlation_id,
        context={
            "opportunity_id": str(opportunity.id),
            "source_count": source_count,
            "bootstrap_sources": bootstrap_sources,
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
    _validated_workflow_id(workflow_id)
    try:
        return await _workflow_view(session, _gateway(request), workflow_id)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail="Research workflow not found") from exc


@router.get("/runs/{workflow_id}/logs", response_model=list[ResearchWorkflowLogEntry])
async def get_run_logs(
    workflow_id: str,
    request: Request,
    _: Viewer,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ResearchWorkflowLogEntry]:
    workflow_id = _validated_workflow_id(workflow_id)
    try:
        return [
            ResearchWorkflowLogEntry.model_validate(item)
            for item in await _gateway(request).safe_logs(workflow_id, limit)
        ]
    except RPCError as exc:
        raise HTTPException(status_code=404, detail="Research workflow not found") from exc


@router.get("/runs/{workflow_id}/events")
async def stream_run_events(
    workflow_id: str,
    request: Request,
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    workflow_id = _validated_workflow_id(workflow_id)
    gateway = _gateway(request)
    try:
        initial = await _workflow_view(session, gateway, workflow_id)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail="Research workflow not found") from exc
    # The stream polls Temporal only. Release the request-scoped database connection
    # before holding the HTTP response open so concurrent monitors cannot exhaust the pool.
    await session.close()

    async def events():
        current = initial
        last_payload = ""
        quiet_seconds = 0
        while True:
            payload = current.model_dump_json()
            if payload != last_payload:
                yield f"event: status\ndata: {payload}\n\n"
                last_payload = payload
                quiet_seconds = 0
            elif quiet_seconds >= 15:
                yield ": keepalive\n\n"
                quiet_seconds = 0
            if current.execution_status in _TERMINAL_EXECUTION_STATUSES:
                break
            await asyncio.sleep(1)
            quiet_seconds += 1
            if await request.is_disconnected():
                break
            try:
                status = await gateway.describe(workflow_id)
                status["correlation_id"] = current.correlation_id
                current = ResearchWorkflowView.model_validate(status)
            except RPCError:
                error_payload = json.dumps(
                    {
                        "message": "Workflow status is temporarily unavailable",
                        "correlation_id": current.correlation_id,
                    },
                    separators=(",", ":"),
                )
                yield f"event: status-error\ndata: {error_payload}\n\n"
                break

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{workflow_id}/cancel", response_model=ResearchWorkflowView)
async def cancel_run(
    workflow_id: str,
    payload: ResearchWorkflowCancel,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    workflow_id = _validated_workflow_id(workflow_id)
    gateway = _gateway(request)
    try:
        current = await _workflow_view(session, gateway, workflow_id)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail="Research workflow not found") from exc
    reconciled = current.execution_status != "RUNNING"
    if not reconciled:
        await gateway.cancel(workflow_id)
    await append_audit(
        session,
        action=("research.workflow_cancel_reconciled" if reconciled else "research.workflow_cancelled"),
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=workflow_id,
        correlation_id=request.state.correlation_id,
        context={"reason": payload.reason, "prior_execution_status": current.execution_status},
    )
    await session.commit()
    if reconciled:
        return current
    for _ in range(20):
        await asyncio.sleep(0.1)
        updated = await _workflow_view(session, gateway, workflow_id)
        if updated.execution_status != "RUNNING":
            return updated
    return await _workflow_view(session, gateway, workflow_id)


@router.post("/runs/{workflow_id}/retry", response_model=ResearchWorkflowView, status_code=202)
async def retry_run(
    workflow_id: str,
    payload: ResearchWorkflowRetry,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    workflow_id = _validated_workflow_id(workflow_id)
    original = await session.get(WorkflowControlRecordModel, workflow_id)
    if original is None:
        raise HTTPException(
            status_code=409,
            detail="This workflow predates retry provenance and must be rerun from its source action",
        )
    gateway = _gateway(request)
    try:
        current = await _workflow_view(session, gateway, workflow_id)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail="Research workflow not found") from exc
    if not current.retryable:
        raise HTTPException(status_code=409, detail="Only failed or cancelled workflows can be retried")
    retry_workflow_id = f"{original.workflow_type}-retry-{payload.idempotency_key}"
    retry_request = {
        **original.request_payload,
        "workflow_id": retry_workflow_id,
        "idempotency_key": payload.idempotency_key,
        "actor_id": str(actor.id),
        "correlation_id": request.state.correlation_id,
    }
    _, reconciled = await _start_tracked_workflow(
        session,
        gateway,
        workflow_type=original.workflow_type,
        workflow_request=retry_request,
        actor=actor,
        correlation_id=request.state.correlation_id,
        parent_workflow_id=workflow_id,
    )
    await append_audit(
        session,
        action=("research.workflow_retry_reconciled" if reconciled else "research.workflow_retried"),
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=retry_workflow_id,
        correlation_id=request.state.correlation_id,
        context={"parent_workflow_id": workflow_id, "reason": payload.reason},
    )
    await session.commit()
    return await _workflow_view(session, gateway, retry_workflow_id)


@router.post("/opportunities", response_model=OpportunityListItem, status_code=201)
async def create_manual_opportunity(
    payload: ManualOpportunityWrite,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OpportunityListItem:
    subject = await session.get(SubjectProfileModel, payload.subject_profile_id)
    if subject is None or subject.deleted_at is not None or not subject.enabled:
        raise HTTPException(status_code=409, detail="Select an enabled subject profile")
    now = datetime.now(timezone.utc)
    policy_snapshot = _subject_policy(subject)
    opportunity = OpportunityModel(
        id=uuid4(), version=1,
        subject_profile_id=subject.id, title=payload.title.strip(), summary=payload.summary.strip(),
        editorial_rationale=payload.editorial_rationale.strip(), policy_snapshot=policy_snapshot,
        decision="pending", manual=True,
        grouping_reason=["Manually created by an authorized editor; no automated clustering was asserted."],
        duplicate_of_id=None, estimated_cost=payload.estimated_cost,
        decided_by=None, decided_at=None, decision_reason=None,
        created_at=now, updated_at=now, deleted_at=None,
    )
    session.add(opportunity)
    await session.flush()
    await append_audit(
        session, action="opportunity.manually_created", actor_id=actor.id,
        target_type="opportunity", target_id=str(opportunity.id),
        correlation_id=request.state.correlation_id,
        context={"subject_profile_id": str(subject.id), "policy_snapshot": policy_snapshot},
    )
    await session.commit()
    return OpportunityListItem(
        id=opportunity.id, version=opportunity.version, subject_profile_id=subject.id,
        title=opportunity.title, summary=opportunity.summary,
        editorial_rationale=opportunity.editorial_rationale, policy_snapshot=opportunity.policy_snapshot,
        decision="pending", grouping_reason=opportunity.grouping_reason,
        estimated_cost=opportunity.estimated_cost, score=None, score_components={},
        score_penalties={}, score_reasoning=[], source_count=0, snapshot_count=0,
        research_state=None, created_at=opportunity.created_at,
    )


@router.post(
    "/opportunities/{opportunity_id}/manual-dossier",
    response_model=DossierListItem,
    status_code=201,
)
async def create_manual_dossier(
    opportunity_id: UUID,
    payload: ManualDossierWrite,
    request: Request,
    actor: Reviewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DossierListItem:
    opportunity = await session.get(OpportunityModel, opportunity_id, with_for_update=True)
    if opportunity is None or opportunity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    if opportunity.version != payload.expected_opportunity_version:
        raise HTTPException(status_code=409, detail="Opportunity changed; reload before creating a dossier")
    if opportunity.decision != "approved":
        raise HTTPException(status_code=409, detail="Only approved opportunities can enter dossier review")

    workflow_id = f"manual-dossier-{payload.idempotency_key}"
    existing_run = await session.scalar(
        select(ResearchRunModel).where(ResearchRunModel.workflow_id == workflow_id).limit(1)
    )
    if existing_run is not None:
        if existing_run.opportunity_id != opportunity.id:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key is already associated with another opportunity",
            )
        dossier_id = (existing_run.progress or {}).get("research_dossier_id")
        if not dossier_id:
            raise HTTPException(status_code=409, detail="Manual dossier recovery record is incomplete")
        dossier = await session.get(ResearchDossierModel, UUID(str(dossier_id)))
        if dossier is None or dossier.deleted_at is not None:
            raise HTTPException(status_code=409, detail="Manual dossier recovery target is unavailable")
        return _dossier_list_item(dossier)

    subject = await session.get(SubjectProfileModel, opportunity.subject_profile_id)
    if subject is None or subject.deleted_at is not None:
        raise HTTPException(status_code=409, detail="The opportunity subject profile is unavailable")

    snapshot_rows = (
        await session.execute(
            select(SourceSnapshotModel.id, SourceSnapshotModel.source_document_id)
            .select_from(SourceSnapshotModel)
            .join(
                OpportunitySourceModel,
                OpportunitySourceModel.source_document_id == SourceSnapshotModel.source_document_id,
            )
            .join(SourceDocumentModel, SourceDocumentModel.id == SourceSnapshotModel.source_document_id)
            .where(
                OpportunitySourceModel.opportunity_id == opportunity.id,
                _eligible_evidence_source(),
            )
        )
    ).all()
    snapshot_source_ids = {snapshot_id: source_document_id for snapshot_id, source_document_id in snapshot_rows}
    if not snapshot_source_ids:
        raise HTTPException(status_code=409, detail="No immutable source snapshots are available")

    for claim in payload.claims:
        if claim.source_snapshot_id is not None and claim.source_snapshot_id not in snapshot_source_ids:
            raise HTTPException(
                status_code=409,
                detail="Every claim source snapshot must belong to this opportunity",
            )

    latest_run = await session.scalar(
        select(ResearchRunModel)
        .where(
            ResearchRunModel.opportunity_id == opportunity.id,
            ResearchRunModel.deleted_at.is_(None),
        )
        .order_by(ResearchRunModel.created_at.desc())
        .limit(1)
    )
    latest_version = await session.scalar(
        select(func.max(ResearchDossierModel.dossier_version)).where(
            ResearchDossierModel.opportunity_id == opportunity.id,
            ResearchDossierModel.deleted_at.is_(None),
        )
    )
    try:
        policy = explanation_policy(subject.format_policy or {}, subject.approval_profile or {})
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"Subject explanation policy is invalid: {exc}") from exc
    source_requirements = subject.source_requirements or {}
    minimum_independent_sources = int(source_requirements.get("minimum_independent", 2))
    minimum_primary_sources = int(source_requirements.get("minimum_primary", 1))
    explanation_plan = _manual_explanation_plan(
        opportunity,
        payload,
        minimum_units=policy.minimum_coverage_units,
    )
    now = datetime.now(timezone.utc)
    dossier_id = uuid4()
    claim_specs: list[tuple[Any, UUID, list[dict[str, Any]], str, list[str]]] = []
    readiness_claims: list[dict[str, Any]] = []
    for index, claim in enumerate(payload.claims, start=1):
        claim_id = uuid4()
        evidence: list[dict[str, Any]] = []
        status = "draft"
        if claim.source_snapshot_id is not None:
            source_document_id = snapshot_source_ids[claim.source_snapshot_id]
            evidence = [
                {
                    "relationship": claim.relationship,
                    "source_document_id": str(source_document_id),
                    "source_id": str(source_document_id),
                    "source_snapshot_id": str(claim.source_snapshot_id),
                    "source_independent": claim.source_independent,
                    "direct_evidence": claim.direct_evidence,
                    "primary_source": claim.primary_source,
                }
            ]
            status = (
                "supported"
                if claim.relationship == "supports"
                else "disputed" if claim.relationship == "contradicts" else "draft"
            )
        coverage_unit_ids = [
            value
            for value in claim.coverage_unit_ids
            if any(unit["id"] == value for unit in explanation_plan)
        ] or [str(explanation_plan[(index - 1) % len(explanation_plan)]["id"])]
        readiness_claim = {
            "id": str(claim_id),
            "statement": claim.statement,
            "status": status,
            "central": claim.central,
            "coverage_unit_ids": coverage_unit_ids,
            "evidence": evidence,
        }
        readiness_claims.append(readiness_claim)
        claim_specs.append((claim, claim_id, evidence, status, coverage_unit_ids))

    readiness = evaluate_explanation_readiness(
        policy=policy,
        claims=readiness_claims,
        coverage_units=explanation_plan,
        minimum_independent_sources=minimum_independent_sources,
        minimum_primary_sources=minimum_primary_sources,
        counterevidence_search_completed=payload.counterevidence_search_completed,
    )
    completion_document = {
        "complete": readiness.ready,
        "manual_recovery": True,
        "review_required": True,
        "blockers": list(readiness.gaps),
        "source_snapshot_count": len(snapshot_source_ids),
        "manual_review_note": payload.review_note,
        "explanation_readiness": readiness.as_dict(),
    }
    dossier = ResearchDossierModel(
        id=dossier_id,
        version=1,
        opportunity_id=opportunity.id,
        dossier_version=int(latest_version or 0) + 1,
        status="in_review",
        executive_summary=payload.executive_summary,
        chronology=payload.chronology,
        unresolved_questions=payload.unresolved_questions,
        alternative_explanations=payload.alternative_explanations,
        source_quality_notes=payload.source_quality_notes,
        safe_conclusions=payload.safe_conclusions,
        prohibited_overstatements=payload.prohibited_overstatements,
        proposed_angles=payload.proposed_angles or [opportunity.title],
        explanation_plan=explanation_plan,
        completion_evaluation=completion_document,
        reviewed_by=None,
        reviewed_at=None,
        review_comment=None,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(dossier)

    for claim, claim_id, evidence, status, coverage_unit_ids in claim_specs:
        db_claim = ClaimModel(
            id=claim_id,
            version=1,
            research_dossier_id=dossier.id,
            normalized_statement=claim.statement,
            claim_type=claim.claim_type,
            scope="manual_research_recovery",
            relevant_at=None,
            entities=[],
            coverage_unit_ids=coverage_unit_ids,
            confidence=claim.confidence,
            status=status,
            risk=claim.risk,
            central=claim.central,
            review_comment=None,
            reviewed_by=None,
            reviewed_at=None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        session.add(db_claim)
        if evidence and claim.source_snapshot_id is not None and claim.exact_text:
            excerpt_hash = hashlib.sha256(claim.exact_text.encode("utf-8")).hexdigest()
            location_anchor = f"manual-review:{payload.idempotency_key}:{claim_id.hex[:12]}"
            excerpt = await session.scalar(
                select(EvidenceExcerptModel)
                .where(
                    EvidenceExcerptModel.source_snapshot_id == claim.source_snapshot_id,
                    EvidenceExcerptModel.excerpt_hash == excerpt_hash,
                    EvidenceExcerptModel.location_anchor == location_anchor,
                )
                .limit(1)
            )
            if excerpt is None:
                excerpt = EvidenceExcerptModel(
                    id=uuid4(),
                    source_snapshot_id=claim.source_snapshot_id,
                    exact_text=claim.exact_text,
                    prefix_text="",
                    suffix_text="",
                    location_anchor=location_anchor,
                    start_offset=None,
                    end_offset=None,
                    excerpt_hash=excerpt_hash,
                    created_at=now,
                )
                session.add(excerpt)
            session.add(
                ClaimEvidenceModel(
                    id=uuid4(),
                    claim_id=db_claim.id,
                    evidence_excerpt_id=excerpt.id,
                    relationship=claim.relationship,
                    source_independent=claim.source_independent,
                    direct_evidence=claim.direct_evidence,
                    primary_source=claim.primary_source,
                    notes=payload.review_note,
                    created_at=now,
                )
            )

    manual_run = ResearchRunModel(
        id=uuid4(),
        version=1,
        opportunity_id=opportunity.id,
        workflow_id=workflow_id,
        state="DOSSIER_REVIEW",
        research_plan={
            "manual_recovery": True,
            "manual_recovery_of_research_run_id": str(latest_run.id) if latest_run else None,
            "review_note": payload.review_note,
            "counterevidence_search_completed": payload.counterevidence_search_completed,
        },
        progress={
            "percent": 100,
            "manual_recovery": True,
            "research_dossier_id": str(dossier.id),
            "claim_count": len(payload.claims),
            "source_snapshot_count": len(snapshot_source_ids),
            "explanation_ready": readiness.ready,
        },
        correlation_id=request.state.correlation_id,
        started_by=actor.id,
        completed_at=now,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(manual_run)
    session.add(
        WorkflowTransitionModel(
            aggregate_type="opportunity",
            aggregate_id=opportunity.id,
            from_stage=latest_run.state if latest_run else None,
            to_stage="DOSSIER_REVIEW",
            reason="manual recovery dossier created from failed or incomplete research",
            actor_id=actor.id,
            correlation_id=request.state.correlation_id,
            occurred_at=now,
        )
    )
    await append_audit(
        session,
        action="research.manual_dossier_created",
        actor_id=actor.id,
        target_type="research_dossier",
        target_id=str(dossier.id),
        correlation_id=request.state.correlation_id,
        context={
            "opportunity_id": str(opportunity.id),
            "workflow_id": workflow_id,
            "claim_count": len(payload.claims),
            "completion_ready": readiness.ready,
            "blockers": list(readiness.gaps),
        },
    )
    await session.commit()
    return _dossier_list_item(dossier)


@router.get("/opportunities", response_model=list[OpportunityListItem])
async def list_opportunities(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[OpportunityListItem]:
    source_counts = (
        select(
            OpportunitySourceModel.opportunity_id,
            func.count(OpportunitySourceModel.id).label("source_count"),
        )
        .join(
            SourceDocumentModel,
            SourceDocumentModel.id == OpportunitySourceModel.source_document_id,
        )
        .where(_eligible_evidence_source())
        .group_by(OpportunitySourceModel.opportunity_id)
        .subquery()
    )
    snapshot_counts = (
        select(
            OpportunitySourceModel.opportunity_id,
            func.count(func.distinct(SourceSnapshotModel.id)).label("snapshot_count"),
        )
        .join(
            SourceSnapshotModel,
            SourceSnapshotModel.source_document_id == OpportunitySourceModel.source_document_id,
        )
        .join(
            SourceDocumentModel,
            SourceDocumentModel.id == OpportunitySourceModel.source_document_id,
        )
        .where(_eligible_evidence_source())
        .group_by(OpportunitySourceModel.opportunity_id)
        .subquery()
    )
    latest_runs = (
        select(
            ResearchRunModel.opportunity_id,
            ResearchRunModel.state,
            func.row_number()
            .over(
                partition_by=ResearchRunModel.opportunity_id,
                order_by=ResearchRunModel.created_at.desc(),
            )
            .label("position"),
        )
        .where(ResearchRunModel.deleted_at.is_(None))
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                OpportunityModel,
                OpportunityScoreModel,
                func.coalesce(source_counts.c.source_count, 0),
                func.coalesce(snapshot_counts.c.snapshot_count, 0),
                latest_runs.c.state,
            )
            .outerjoin(OpportunityScoreModel, OpportunityScoreModel.opportunity_id == OpportunityModel.id)
            .outerjoin(source_counts, source_counts.c.opportunity_id == OpportunityModel.id)
            .outerjoin(snapshot_counts, snapshot_counts.c.opportunity_id == OpportunityModel.id)
            .outerjoin(
                latest_runs,
                and_(
                    latest_runs.c.opportunity_id == OpportunityModel.id,
                    latest_runs.c.position == 1,
                ),
            )
            .where(OpportunityModel.deleted_at.is_(None))
            .order_by(OpportunityModel.created_at.desc(), OpportunityScoreModel.score_version.desc())
        )
    ).all()
    seen: set[UUID] = set()
    items: list[OpportunityListItem] = []
    for opportunity, score, source_count, snapshot_count, research_state in rows:
        if opportunity.id in seen:
            continue
        seen.add(opportunity.id)
        qualification = await session.scalar(
            select(OpportunityAIQualificationModel)
            .where(OpportunityAIQualificationModel.opportunity_id == opportunity.id)
            .order_by(OpportunityAIQualificationModel.qualification_version.desc())
            .limit(1)
        )
        items.append(
            OpportunityListItem(
                id=opportunity.id, version=opportunity.version,
                subject_profile_id=opportunity.subject_profile_id,
                title=opportunity.title, summary=opportunity.summary, decision=opportunity.decision,
                editorial_rationale=opportunity.editorial_rationale,
                policy_snapshot=opportunity.policy_snapshot,
                grouping_reason=opportunity.grouping_reason, estimated_cost=opportunity.estimated_cost,
                score=score.total if score else None,
                score_version=score.score_version if score else None,
                score_components=score.positive_components if score else {},
                score_penalties=score.penalties if score else {},
                score_weights=score.weights if score else {},
                score_reasoning=score.reasoning if score else [], source_count=source_count,
                ai_qualification=(
                    {
                        "id": str(qualification.id),
                        "version": qualification.qualification_version,
                        "dimensions": qualification.dimensions,
                        "confidence": qualification.confidence,
                        "abstained": qualification.abstained,
                        "rationale": qualification.rationale,
                        "uncertainty": qualification.uncertainty,
                        "resulting_score_version": qualification.resulting_score_version,
                    }
                    if qualification else None
                ),
                snapshot_count=snapshot_count, research_state=research_state,
                created_at=opportunity.created_at,
            )
        )
    return items


@router.get(
    "/opportunities/archived", response_model=list[ArchivedOpportunityListItem]
)
async def list_archived_opportunities(
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
    subject_profile_id: UUID | None = None,
) -> list[ArchivedOpportunityListItem]:
    score_rows = (
        select(
            OpportunityScoreModel,
            func.row_number()
            .over(
                partition_by=OpportunityScoreModel.opportunity_id,
                order_by=OpportunityScoreModel.score_version.desc(),
            )
            .label("position"),
        )
        .subquery()
    )
    source_counts = (
        select(
            OpportunitySourceModel.opportunity_id,
            func.count(OpportunitySourceModel.id).label("source_count"),
        )
        .group_by(OpportunitySourceModel.opportunity_id)
        .subquery()
    )
    statement = (
        select(
            OpportunityModel,
            score_rows,
            func.coalesce(source_counts.c.source_count, 0),
        )
        .outerjoin(
            score_rows,
            and_(
                score_rows.c.opportunity_id == OpportunityModel.id,
                score_rows.c.position == 1,
            ),
        )
        .outerjoin(source_counts, source_counts.c.opportunity_id == OpportunityModel.id)
        .where(OpportunityModel.deleted_at.is_not(None))
        .order_by(OpportunityModel.deleted_at.desc(), OpportunityModel.created_at.desc())
    )
    if subject_profile_id is not None:
        statement = statement.where(OpportunityModel.subject_profile_id == subject_profile_id)
    rows = (await session.execute(statement)).all()
    items: list[ArchivedOpportunityListItem] = []
    for row in rows:
        opportunity = row[0]
        score = row._mapping
        source_count = int(row[-1])
        items.append(
            ArchivedOpportunityListItem(
                id=opportunity.id,
                version=opportunity.version,
                subject_profile_id=opportunity.subject_profile_id,
                title=opportunity.title,
                summary=opportunity.summary,
                editorial_rationale=opportunity.editorial_rationale,
                policy_snapshot=opportunity.policy_snapshot,
                decision=opportunity.decision,
                grouping_reason=opportunity.grouping_reason,
                estimated_cost=opportunity.estimated_cost,
                score=score.get("total"),
                score_version=score.get("score_version"),
                score_components=score.get("positive_components") or {},
                score_penalties=score.get("penalties") or {},
                score_weights=score.get("weights") or {},
                score_reasoning=score.get("reasoning") or [],
                source_count=source_count,
                snapshot_count=0,
                research_state=None,
                created_at=opportunity.created_at,
                archived_at=opportunity.deleted_at,
            )
        )
    return items


@router.post(
    "/opportunities/archive-scored", response_model=ScoredOpportunityArchiveView
)
async def archive_scored_opportunities(
    payload: ScoredOpportunityArchiveWrite,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScoredOpportunityArchiveView:
    """Remove stale scored findings from the active board without erasing evidence history."""

    subject = await session.get(SubjectProfileModel, payload.subject_profile_id)
    if subject is None or subject.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Subject profile not found")
    candidates = list(
        (
            await session.scalars(
                select(OpportunityModel)
                .join(
                    OpportunityScoreModel,
                    OpportunityScoreModel.opportunity_id == OpportunityModel.id,
                )
                .where(
                    OpportunityModel.subject_profile_id == subject.id,
                    OpportunityModel.deleted_at.is_(None),
                )
                .order_by(OpportunityModel.created_at, OpportunityModel.id)
                .with_for_update()
            )
        ).unique()
    )
    blocked_ids: set[UUID] = set()
    archived_at = datetime.now(timezone.utc)
    archived_ids: list[str] = []
    for opportunity in candidates:
        opportunity.deleted_at = archived_at
        opportunity.updated_at = archived_at
        opportunity.version += 1
        archived_ids.append(str(opportunity.id))
    await append_audit(
        session,
        action="opportunity.scored_batch_archived",
        actor_id=actor.id,
        target_type="subject_profile",
        target_id=str(subject.id),
        correlation_id=request.state.correlation_id,
        context={
            "reason": payload.reason,
            "archived_count": len(archived_ids),
            "archived_opportunity_ids": archived_ids,
            "blocked_opportunity_ids": [str(value) for value in sorted(blocked_ids, key=str)],
            "immutable_scores_retained": True,
            "linked_research_and_editorial_history_retained": True,
        },
    )
    await session.commit()
    return ScoredOpportunityArchiveView(
        subject_profile_id=subject.id,
        archived_count=len(archived_ids),
        blocked_count=len(blocked_ids),
        archived_at=archived_at,
    )


@router.post(
    "/opportunities/archive-all", response_model=AllOpportunityArchiveView
)
async def archive_all_opportunities(
    payload: AllOpportunityArchiveWrite,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AllOpportunityArchiveView:
    """Archive every active finding while retaining its complete immutable lineage."""

    opportunities = list(
        await session.scalars(
            select(OpportunityModel)
            .where(OpportunityModel.deleted_at.is_(None))
            .order_by(OpportunityModel.created_at, OpportunityModel.id)
            .with_for_update()
        )
    )
    archived_at = datetime.now(timezone.utc)
    for opportunity in opportunities:
        opportunity.deleted_at = archived_at
        opportunity.updated_at = archived_at
        opportunity.version += 1
    await append_audit(
        session,
        action="opportunity.systemwide_batch_archived",
        actor_id=actor.id,
        target_type="opportunity_archive",
        target_id="all-active",
        correlation_id=request.state.correlation_id,
        context={
            "reason": payload.reason,
            "archived_count": len(opportunities),
            "immutable_scores_retained": True,
            "linked_research_and_editorial_history_retained": True,
        },
    )
    await session.commit()
    return AllOpportunityArchiveView(
        archived_count=len(opportunities),
        archived_at=archived_at,
    )


@router.post(
    "/opportunities/{opportunity_id}/decision", response_model=OpportunityDecisionView
)
async def decide_opportunity(
    opportunity_id: UUID,
    payload: OpportunityDecisionWrite,
    request: Request,
    actor: Reviewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OpportunityDecisionView:
    opportunity = await session.get(OpportunityModel, opportunity_id, with_for_update=True)
    if opportunity is None or opportunity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    if opportunity.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="Opportunity changed; reload before deciding")
    if opportunity.decision not in {"pending", "deferred"}:
        raise HTTPException(status_code=409, detail="Opportunity decision is already final")
    subject = await session.get(SubjectProfileModel, opportunity.subject_profile_id)
    if subject is None or subject.deleted_at is not None:
        raise HTTPException(status_code=409, detail="Opportunity subject profile is unavailable")
    policy_snapshot = _subject_policy(subject)
    rationale = (payload.editorial_rationale or opportunity.editorial_rationale).strip()
    if payload.decision == "approved" and len(rationale) < 20:
        raise HTTPException(
            status_code=409,
            detail="Explain why this video deserves to exist before shortlisting",
        )
    target = {
        "approved": ProductionStage.SHORTLISTED,
        "rejected": ProductionStage.CANCELLED,
        "deferred": ProductionStage.PAUSED,
    }[payload.decision]
    validate_transition(ProductionStage.DISCOVERY, target)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    opportunity.decision = payload.decision
    opportunity.decided_by = actor.id
    opportunity.decided_at = now
    opportunity.decision_reason = payload.reason
    opportunity.editorial_rationale = rationale
    opportunity.policy_snapshot = policy_snapshot
    opportunity.updated_at = now
    opportunity.version += 1
    transition = WorkflowTransitionModel(
        aggregate_type="opportunity",
        aggregate_id=opportunity.id,
        from_stage=ProductionStage.DISCOVERY.value,
        to_stage=target.value,
        reason=payload.reason,
        actor_id=actor.id,
        correlation_id=request.state.correlation_id,
        occurred_at=now,
    )
    session.add(transition)
    await append_audit(
        session,
        action=f"opportunity.{payload.decision}",
        actor_id=actor.id,
        target_type="opportunity",
        target_id=str(opportunity.id),
        correlation_id=request.state.correlation_id,
        context={
            "version": opportunity.version, "reason": payload.reason,
            "editorial_rationale": rationale, "policy_snapshot": policy_snapshot,
        },
    )
    continuation: AutomaticContinuation | None = None
    if payload.decision == "approved":
        source_count = await session.scalar(
            select(func.count(OpportunitySourceModel.id)).where(
                OpportunitySourceModel.opportunity_id == opportunity.id
            )
        )
        bootstrap_sources = not bool(source_count)
        idempotency_key = f"approval-{opportunity.id.hex}-v{opportunity.version}"
        workflow_id = f"source-acquisition-{idempotency_key}"
        workflow_request = {
            "workflow_id": workflow_id,
            "idempotency_key": idempotency_key,
            "opportunity_id": str(opportunity.id),
            "actor_id": str(actor.id),
            "correlation_id": request.state.correlation_id,
            "auto_continue": True,
        }
        if bootstrap_sources:
            workflow_request["bootstrap_sources"] = True
        _, reconciled = await _start_tracked_workflow(
            session,
            _gateway(request),
            workflow_type="source-acquisition",
            workflow_request=workflow_request,
            actor=actor,
            correlation_id=request.state.correlation_id,
        )
        continuation = AutomaticContinuation(
            state="reconciled" if reconciled else "started",
            action=(
                "source_bootstrap_acquisition_and_dossier"
                if bootstrap_sources
                else "source_acquisition_and_dossier"
            ),
            workflow_id=workflow_id,
            message=(
                "Source discovery/acquisition started from the approved topic and will "
                "continue automatically into dossier generation."
                if bootstrap_sources
                else (
                    "Source acquisition started and will continue automatically into "
                    "dossier generation."
                )
            ),
        )
        await append_audit(
            session,
            action=f"automation.opportunity_{continuation.state}",
            actor_id=actor.id,
            target_type="opportunity",
            target_id=str(opportunity.id),
            correlation_id=request.state.correlation_id,
            context={
                **continuation.model_dump(mode="json"),
                "source_count": source_count,
                "bootstrap_sources": bootstrap_sources,
            },
        )
    await session.commit()
    return OpportunityDecisionView(
        id=opportunity.id,
        decision=opportunity.decision,
        version=opportunity.version,
        decision_reason=opportunity.decision_reason,
        decided_at=opportunity.decided_at,
        automatic_continuation=continuation,
    )


@router.get("/sources", response_model=list[SourceBrowserItem])
async def list_sources(
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
    opportunity_id: UUID | None = Query(default=None),
) -> list[SourceBrowserItem]:
    statement = select(SourceDocumentModel).where(SourceDocumentModel.deleted_at.is_(None))
    statement = statement.where(_eligible_evidence_source())
    if opportunity_id is not None:
        statement = statement.join(
            OpportunitySourceModel,
            OpportunitySourceModel.source_document_id == SourceDocumentModel.id,
        ).where(OpportunitySourceModel.opportunity_id == opportunity_id)
    documents = list(
        await session.scalars(
            statement.distinct().order_by(SourceDocumentModel.updated_at.desc()).limit(200)
        )
    )
    items: list[SourceBrowserItem] = []
    for document in documents:
        snapshots = list(
            await session.scalars(
                select(SourceSnapshotModel)
                .where(SourceSnapshotModel.source_document_id == document.id)
                .order_by(SourceSnapshotModel.snapshot_number.desc())
            )
        )
        snapshot_views: list[SourceSnapshotView] = []
        for snapshot in snapshots:
            chunk_count = await session.scalar(
                select(func.count(SemanticChunkModel.id)).where(
                    SemanticChunkModel.source_snapshot_id == snapshot.id
                )
            )
            snapshot_views.append(
                SourceSnapshotView(
                    id=snapshot.id,
                    snapshot_number=snapshot.snapshot_number,
                    content_hash=snapshot.content_hash,
                    mime_type=snapshot.mime_type,
                    byte_size=snapshot.byte_size,
                    retrieved_at=snapshot.retrieved_at,
                    extraction_metadata=snapshot.extraction_metadata,
                    injection_markers=snapshot.injection_markers,
                    semantic_chunk_count=chunk_count or 0,
                )
            )
        relationships = list(
            await session.scalars(
                select(SourceRelationshipModel)
                .where(
                    (SourceRelationshipModel.source_document_id == document.id)
                    | (SourceRelationshipModel.related_source_document_id == document.id)
                )
                .order_by(SourceRelationshipModel.created_at.desc())
            )
        )
        items.append(
            SourceBrowserItem(
                id=document.id,
                canonical_url=document.canonical_url,
                title=document.title,
                author=document.author,
                publisher=document.publisher,
                source_type=document.source_type,
                publication_at=document.publication_at,
                event_at=document.event_at,
                reputation=document.reputation,
                domain=document.domain,
                snapshots=snapshot_views,
                relationships=[
                    SourceRelationshipView(
                        id=relationship.id,
                        source_document_id=relationship.source_document_id,
                        related_source_document_id=relationship.related_source_document_id,
                        relationship=relationship.relationship,
                        reason=relationship.reason,
                        confidence=relationship.confidence,
                        created_at=relationship.created_at,
                    )
                    for relationship in relationships
                ],
            )
        )
    return items


@router.get("/sources/{source_id}/preview", response_model=SourcePreview)
async def preview_source(
    source_id: UUID,
    request: Request,
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(default=20_000, ge=1_000, le=100_000),
) -> SourcePreview:
    source = await session.get(SourceDocumentModel, source_id)
    if source is None or source.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Source not found")
    snapshot = await session.scalar(
        select(SourceSnapshotModel)
        .where(SourceSnapshotModel.source_document_id == source.id)
        .order_by(SourceSnapshotModel.snapshot_number.desc())
        .limit(1)
    )
    if snapshot is None:
        raise HTTPException(status_code=409, detail="Source has not been acquired")
    text, truncated = await asyncio.to_thread(
        _read_normalized_preview,
        request.app.state.minio_client,
        get_settings().minio_bucket,
        snapshot.normalized_object_key,
        limit,
    )
    return SourcePreview(
        source_document_id=source.id,
        source_snapshot_id=snapshot.id,
        content_hash=snapshot.content_hash,
        text=text,
        truncated=truncated,
    )


@router.post(
    "/sources/{source_id}/index", response_model=ResearchWorkflowView, status_code=202
)
async def start_source_index(
    source_id: UUID,
    payload: SourceIndexStart,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchWorkflowView:
    source = await session.get(SourceDocumentModel, source_id)
    if source is None or source.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Source not found")
    snapshot_count = await session.scalar(
        select(func.count(SourceSnapshotModel.id)).where(
            SourceSnapshotModel.source_document_id == source.id
        )
    )
    if not snapshot_count:
        raise HTTPException(status_code=409, detail="Source has not been acquired")
    workflow_id = f"source-semantic-index-{source.id}-{payload.idempotency_key}"
    gateway = _gateway(request)
    await _start_tracked_workflow(
        session,
        gateway,
        workflow_type="source-semantic-index",
        workflow_request={
            "workflow_id": workflow_id,
            "source_document_id": str(source.id),
            "idempotency_key": payload.idempotency_key,
            "actor_id": str(actor.id),
            "correlation_id": request.state.correlation_id,
        },
        actor=actor,
        correlation_id=request.state.correlation_id,
    )
    await append_audit(
        session,
        action="research.source_index_started",
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=workflow_id,
        correlation_id=request.state.correlation_id,
        context={"source_document_id": str(source.id)},
    )
    await session.commit()
    return await _workflow_view(session, gateway, workflow_id)


@router.post("/source-relationships", response_model=SourceRelationshipView, status_code=201)
async def create_source_relationship(
    payload: SourceRelationshipWrite,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SourceRelationshipView:
    endpoints = list(
        await session.scalars(
            select(SourceDocumentModel).where(
                SourceDocumentModel.id.in_(
                    [payload.source_document_id, payload.related_source_document_id]
                ),
                SourceDocumentModel.deleted_at.is_(None),
            )
        )
    )
    if len(endpoints) != 2:
        raise HTTPException(status_code=422, detail="Both source documents must exist")
    existing = await session.scalar(
        select(SourceRelationshipModel).where(
            SourceRelationshipModel.source_document_id == payload.source_document_id,
            SourceRelationshipModel.related_source_document_id
            == payload.related_source_document_id,
            SourceRelationshipModel.relationship == payload.relationship,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Source relationship already exists")
    from datetime import datetime, timezone

    relationship = SourceRelationshipModel(
        source_document_id=payload.source_document_id,
        related_source_document_id=payload.related_source_document_id,
        relationship=payload.relationship,
        reason=payload.reason,
        confidence=payload.confidence,
        created_at=datetime.now(timezone.utc),
    )
    session.add(relationship)
    await session.flush()
    await append_audit(
        session,
        action="source_relationship.created",
        actor_id=actor.id,
        target_type="source_relationship",
        target_id=str(relationship.id),
        correlation_id=request.state.correlation_id,
        context=payload.model_dump(mode="json"),
    )
    await session.commit()
    return SourceRelationshipView(
        id=relationship.id,
        source_document_id=relationship.source_document_id,
        related_source_document_id=relationship.related_source_document_id,
        relationship=relationship.relationship,
        reason=relationship.reason,
        confidence=relationship.confidence,
        created_at=relationship.created_at,
    )


@router.get("/dossiers", response_model=list[DossierListItem])
async def list_dossiers(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[DossierListItem]:
    dossiers = list(
        await session.scalars(
            select(ResearchDossierModel)
            .join(OpportunityModel, OpportunityModel.id == ResearchDossierModel.opportunity_id)
            .where(ResearchDossierModel.deleted_at.is_(None))
            .where(OpportunityModel.deleted_at.is_(None))
            .order_by(ResearchDossierModel.created_at.desc())
        )
    )
    return [
        DossierListItem(
            id=item.id, opportunity_id=item.opportunity_id, dossier_version=item.dossier_version,
            version=item.version,
            status=item.status, executive_summary=item.executive_summary,
            completion_evaluation=item.completion_evaluation, created_at=item.created_at,
        )
        for item in dossiers
    ]


@router.get("/dossiers/{dossier_id}", response_model=DossierDetail)
async def get_dossier(
    dossier_id: UUID,
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DossierDetail:
    dossier = await session.get(ResearchDossierModel, dossier_id)
    if dossier is None or dossier.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Dossier not found")
    claims = list(
        await session.scalars(
            select(ClaimModel)
            .where(ClaimModel.research_dossier_id == dossier.id, ClaimModel.deleted_at.is_(None))
            .order_by(ClaimModel.central.desc(), ClaimModel.created_at)
        )
    )
    ledger: list[ClaimLedgerItem] = []
    for claim in claims:
        evidence_rows = (
            await session.execute(
                select(ClaimEvidenceModel, EvidenceExcerptModel, SourceSnapshotModel, SourceDocumentModel)
                .join(EvidenceExcerptModel, EvidenceExcerptModel.id == ClaimEvidenceModel.evidence_excerpt_id)
                .join(SourceSnapshotModel, SourceSnapshotModel.id == EvidenceExcerptModel.source_snapshot_id)
                .join(SourceDocumentModel, SourceDocumentModel.id == SourceSnapshotModel.source_document_id)
                .where(ClaimEvidenceModel.claim_id == claim.id)
            )
        ).all()
        evidence: list[dict[str, Any]] = [
            {
                "relationship": link.relationship, "exact_text": excerpt.exact_text,
                "location_anchor": excerpt.location_anchor, "source_independent": link.source_independent,
                "direct_evidence": link.direct_evidence, "primary_source": link.primary_source,
                "source": {"title": source.title, "canonical_url": source.canonical_url, "publisher": source.publisher},
                "snapshot": {"content_hash": snapshot.content_hash, "retrieved_at": snapshot.retrieved_at.isoformat()},
            }
            for link, excerpt, snapshot, source in evidence_rows
        ]
        ledger.append(
            ClaimLedgerItem(
                id=claim.id, normalized_statement=claim.normalized_statement, claim_type=claim.claim_type,
                confidence=claim.confidence, status=claim.status, risk=claim.risk, central=claim.central,
                coverage_unit_ids=claim.coverage_unit_ids, version=claim.version, evidence=evidence,
            )
        )
    assessment = await session.scalar(
        select(DossierAIAssessmentModel)
        .where(DossierAIAssessmentModel.research_dossier_id == dossier.id)
        .order_by(DossierAIAssessmentModel.assessment_version.desc())
        .limit(1)
    )
    return DossierDetail(
        id=dossier.id, opportunity_id=dossier.opportunity_id, dossier_version=dossier.dossier_version,
        version=dossier.version,
        status=dossier.status, executive_summary=dossier.executive_summary,
        completion_evaluation=dossier.completion_evaluation, created_at=dossier.created_at,
        chronology=dossier.chronology, unresolved_questions=dossier.unresolved_questions,
        alternative_explanations=dossier.alternative_explanations,
        source_quality_notes=dossier.source_quality_notes, safe_conclusions=dossier.safe_conclusions,
        prohibited_overstatements=dossier.prohibited_overstatements, proposed_angles=dossier.proposed_angles,
        explanation_plan=dossier.explanation_plan,
        claims=ledger,
        ai_evidence_assessment=(
            {
                "id": str(assessment.id),
                "version": assessment.assessment_version,
                "claim_assessments": assessment.claim_assessments,
                "source_assessments": assessment.source_assessments,
                "methodological_limits": assessment.methodological_limits,
                "counterevidence_gaps": assessment.counterevidence_gaps,
                "confidence": assessment.confidence,
                "abstained": assessment.abstained,
                "uncertainty": assessment.uncertainty,
            }
            if assessment else None
        ),
    )


@router.post("/claims/{claim_id}/review", response_model=ClaimLedgerItem)
async def review_claim(
    claim_id: UUID,
    payload: ReviewDecision,
    request: Request,
    actor: Reviewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ClaimLedgerItem:
    claim = await session.get(ClaimModel, claim_id, with_for_update=True)
    if claim is None or claim.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Claim not found")
    if claim.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="Claim changed; reload before reviewing")
    from datetime import datetime, timezone

    claim.status = payload.decision
    claim.review_comment = payload.comment
    claim.reviewed_by = actor.id
    claim.reviewed_at = datetime.now(timezone.utc)
    claim.updated_at = claim.reviewed_at
    claim.version += 1
    await append_audit(
        session,
        action=f"claim.{payload.decision}", actor_id=actor.id, target_type="claim",
        target_id=str(claim.id), correlation_id=request.state.correlation_id,
        context={"version": claim.version, "comment": payload.comment},
    )
    await session.commit()
    return ClaimLedgerItem(
        id=claim.id, normalized_statement=claim.normalized_statement, claim_type=claim.claim_type,
        confidence=claim.confidence, status=claim.status, risk=claim.risk, central=claim.central,
        coverage_unit_ids=claim.coverage_unit_ids, version=claim.version, evidence=[],
    )


@router.post("/dossiers/{dossier_id}/review", response_model=DossierListItem)
async def review_dossier(
    dossier_id: UUID,
    payload: ReviewDecision,
    request: Request,
    actor: Reviewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DossierListItem:
    dossier = await session.get(ResearchDossierModel, dossier_id, with_for_update=True)
    if dossier is None or dossier.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Dossier not found")
    if dossier.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="Dossier changed; reload before reviewing")
    completion_met = bool(dossier.completion_evaluation.get("complete"))
    if payload.decision == "approved" and not completion_met and payload.override_reason is None:
        raise HTTPException(
            status_code=409,
            detail="Research completion rules are unmet; an authorized reasoned override is required",
        )
    opportunity = await session.get(OpportunityModel, dossier.opportunity_id)
    subject = await session.get(SubjectProfileModel, opportunity.subject_profile_id) if opportunity else None
    if payload.decision == "approved" and (
        opportunity is None or subject is None or len(opportunity.editorial_rationale.strip()) < 20
    ):
        raise HTTPException(status_code=409, detail="The opportunity is missing its required editorial rationale")
    policy_snapshot = _subject_policy(subject) if subject else {}
    from datetime import datetime, timezone

    dossier.status = payload.decision
    dossier.review_comment = payload.comment
    dossier.reviewed_by = actor.id
    dossier.reviewed_at = datetime.now(timezone.utc)
    dossier.updated_at = dossier.reviewed_at
    dossier.version += 1
    approved_claim_ids: list[str] = []
    if payload.decision == "approved":
        supported_claims = list(
            await session.scalars(
                select(ClaimModel).where(
                    ClaimModel.research_dossier_id == dossier.id,
                    ClaimModel.status == "supported",
                    ClaimModel.deleted_at.is_(None),
                )
            )
        )
        for claim in supported_claims:
            claim.status = "approved"
            claim.review_comment = payload.comment
            claim.reviewed_by = actor.id
            claim.reviewed_at = dossier.reviewed_at
            claim.updated_at = dossier.reviewed_at
            claim.version += 1
            approved_claim_ids.append(str(claim.id))
    await append_audit(
        session,
        action=f"dossier.{payload.decision}", actor_id=actor.id, target_type="research_dossier",
        target_id=str(dossier.id), correlation_id=request.state.correlation_id,
        context={
            "version": dossier.version, "comment": payload.comment,
            "completion_met": completion_met, "override_reason": payload.override_reason,
            "policy_snapshot": policy_snapshot,
            "claims_approved_with_dossier": approved_claim_ids,
        },
    )
    if approved_claim_ids:
        await append_audit(
            session,
            action="dossier.supported_claims_approved",
            actor_id=actor.id,
            target_type="research_dossier",
            target_id=str(dossier.id),
            correlation_id=request.state.correlation_id,
            context={
                "dossier_version": dossier.version,
                "claim_ids": approved_claim_ids,
                "comment": payload.comment,
            },
        )
    continuation: AutomaticContinuation | None = None
    if payload.decision == "approved":
        existing_script = await session.scalar(
            select(ScriptModel.id).where(ScriptModel.research_dossier_id == dossier.id)
        )
        readiness = dossier.completion_evaluation.get("explanation_readiness", {})
        approved_claims = await session.scalar(
            select(func.count(ClaimModel.id)).where(
                ClaimModel.research_dossier_id == dossier.id,
                ClaimModel.status == "approved",
                ClaimModel.deleted_at.is_(None),
            )
        )
        if existing_script:
            continuation = AutomaticContinuation(
                state="completed",
                action="script_generation",
                message="A versioned script already exists for this approved dossier.",
            )
        elif not readiness.get("ready"):
            continuation = AutomaticContinuation(
                state="awaiting_input",
                action="script_generation",
                message="Approval is recorded, but explanation-readiness gates still block script generation.",
            )
        elif not approved_claims:
            continuation = AutomaticContinuation(
                state="awaiting_input",
                action="script_generation",
                message="Approval is recorded, but at least one claim must be approved before script generation.",
            )
        else:
            idempotency_key = f"approval-{dossier.id.hex}-v{dossier.version}"
            workflow_id = f"script-generation-{idempotency_key}"
            workflow_payload = {
                "workflow_id": workflow_id,
                "dossier_id": str(dossier.id),
                "expected_dossier_version": dossier.version,
                "sensitivity": "internal",
                "idempotency_key": idempotency_key,
                "actor_id": str(actor.id),
                "correlation_id": request.state.correlation_id,
            }
            existing = await session.get(WorkflowControlRecordModel, workflow_id)
            if existing is None:
                session.add(
                    WorkflowControlRecordModel(
                        workflow_id=workflow_id,
                        workflow_type="script-generation",
                        request_payload=workflow_payload,
                        parent_workflow_id=None,
                        correlation_id=request.state.correlation_id,
                        started_by=actor.id,
                        created_at=datetime.now(timezone.utc),
                    )
                )
                await session.flush()
            await TemporalEditorialGateway(request.app.state.temporal_client).start(
                "script-generation", existing.request_payload if existing else workflow_payload
            )
            continuation = AutomaticContinuation(
                state="reconciled" if existing else "started",
                action="script_generation",
                workflow_id=workflow_id,
                message="Script generation and independent verification started automatically.",
            )
        await append_audit(
            session,
            action=f"automation.dossier_{continuation.state}",
            actor_id=actor.id,
            target_type="research_dossier",
            target_id=str(dossier.id),
            correlation_id=request.state.correlation_id,
            context=continuation.model_dump(mode="json"),
        )
    await session.commit()
    return DossierListItem(
        id=dossier.id, opportunity_id=dossier.opportunity_id, dossier_version=dossier.dossier_version,
        version=dossier.version, status=dossier.status, executive_summary=dossier.executive_summary,
        completion_evaluation=dossier.completion_evaluation, created_at=dossier.created_at,
        automatic_continuation=continuation,
    )
