from __future__ import annotations

import asyncio
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError

from editorial_core.authorization import Permission
from youtuber_api.audit import append_audit
from youtuber_api.db import get_session
from youtuber_api.models import (
    ChannelProfileModel,
    MediaProductionModel,
    OpportunityModel,
    PublicationModel,
    ResearchDossierModel,
    SceneModel,
    ScriptModel,
    ScriptVersionModel,
    StoryboardModel,
    StoryboardVersionModel,
    SubjectProfileModel,
    UserModel,
    WorkflowControlRecordModel,
    YouTubeConnectionModel,
)
from youtuber_api.schemas import ProbeStart, ProbeView, WorkflowSummaryView
from youtuber_api.security import require
from youtuber_api.temporal_gateway import TemporalProbeGateway

router = APIRouter(prefix="/system", tags=["system"])


def _payload_uuid(payload: dict[str, Any], key: str) -> UUID | None:
    value = payload.get(key)
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _workflow_subject(
    session: AsyncSession, payload: dict[str, Any]
) -> SubjectProfileModel | None:
    subject_id = _payload_uuid(payload, "subject_profile_id")
    if subject_id:
        return await session.get(SubjectProfileModel, subject_id)

    opportunity: OpportunityModel | None = None
    opportunity_id = _payload_uuid(payload, "opportunity_id")
    if opportunity_id:
        opportunity = await session.get(OpportunityModel, opportunity_id)

    dossier_id = _payload_uuid(payload, "dossier_id")
    if dossier_id and opportunity is None:
        dossier = await session.get(ResearchDossierModel, dossier_id)
        opportunity = await session.get(OpportunityModel, dossier.opportunity_id) if dossier else None

    script: ScriptModel | None = None
    script_id = _payload_uuid(payload, "script_id")
    if script_id:
        script = await session.get(ScriptModel, script_id)
    script_version_id = _payload_uuid(payload, "script_version_id")
    if script_version_id and script is None:
        version = await session.get(ScriptVersionModel, script_version_id)
        script = await session.get(ScriptModel, version.script_id) if version else None

    storyboard: StoryboardModel | None = None
    storyboard_id = _payload_uuid(payload, "storyboard_id")
    if storyboard_id:
        storyboard = await session.get(StoryboardModel, storyboard_id)
    storyboard_version_id = _payload_uuid(payload, "storyboard_version_id")
    if storyboard_version_id and storyboard is None:
        version = await session.get(StoryboardVersionModel, storyboard_version_id)
        storyboard = await session.get(StoryboardModel, version.storyboard_id) if version else None
    scene_id = _payload_uuid(payload, "scene_id")
    if scene_id and storyboard is None:
        scene = await session.get(SceneModel, scene_id)
        storyboard = await session.get(StoryboardModel, scene.storyboard_id) if scene else None

    production_id = _payload_uuid(payload, "production_id")
    if production_id and storyboard is None:
        production = await session.get(MediaProductionModel, production_id)
        if production:
            version = await session.get(StoryboardVersionModel, production.storyboard_version_id)
            storyboard = await session.get(StoryboardModel, version.storyboard_id) if version else None

    if storyboard and script is None:
        script = await session.get(ScriptModel, storyboard.script_id)
    if script and opportunity is None:
        opportunity = await session.get(OpportunityModel, script.opportunity_id)
    return await session.get(SubjectProfileModel, opportunity.subject_profile_id) if opportunity else None


async def _workflow_channel_id(
    session: AsyncSession, record: WorkflowControlRecordModel, subject: SubjectProfileModel | None
) -> UUID | None:
    if subject:
        return subject.channel_profile_id
    channel_id = _payload_uuid(record.request_payload, "channel_profile_id")
    if channel_id:
        return channel_id
    connection_id = _payload_uuid(record.request_payload, "connection_id")
    publication_id = _payload_uuid(record.request_payload, "publication_id")
    if publication_id and connection_id is None:
        publication = await session.get(PublicationModel, publication_id)
        connection_id = publication.connection_id if publication else None
    if connection_id:
        connection = await session.get(YouTubeConnectionModel, connection_id)
        return connection.channel_profile_id if connection else None
    return None


async def _execution_status(request: Request, workflow_id: str) -> str:
    try:
        description = await asyncio.wait_for(
            request.app.state.temporal_client.get_workflow_handle(workflow_id).describe(),
            timeout=2.0,
        )
        return (
            "CANCELLED"
            if description.status == WorkflowExecutionStatus.CANCELED
            else description.status.name
        )
    except Exception:
        return "UNKNOWN"


def _gateway(request: Request) -> TemporalProbeGateway:
    return TemporalProbeGateway(request.app.state.temporal_client)


@router.get("/capabilities")
async def capabilities(
    _: Annotated[UserModel, Depends(require(Permission.VIEW))],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "increment": 5,
        "capabilities": {
            "authentication": {"available": True, "modes": ["local", "totp", "oidc"]},
            "operating_policy": {
                "available": True,
                "profiles": ["assisted", "supervised", "trusted"],
                "sensitive_human_gates": True,
            },
            "rbac": {"available": True},
            "audit": {"available": True, "append_only": True},
            "configuration": {"available": True, "versioned": True},
            "durable_workflows": {
                "available": True,
                "types": [
                    "durability-probe",
                    "scheduled-subject-discovery",
                    "live-discovery",
                    "source-acquisition",
                    "live-research-dossier",
                    "source-semantic-index",
                    "fixture-research",
                    "script-generation",
                    "script-verification",
                    "script-regeneration",
                    "storyboard-generation",
                    "scene-alternative-generation",
                    "media-production",
                    "scene-media-regeneration",
                    "narration-segment-regeneration",
                    "youtube-private-upload",
                    "youtube-reconcile",
                    "youtube-schedule",
                ],
            },
            "channel_subject_profiles": {"available": True, "versioned": True},
            "live_discovery": {"available": True, "provider": "searxng"},
            "safe_acquisition_policy": {
                "available": True,
                "network_adapter": True,
                "approval_required": True,
                "robots_enforced": True,
                "dns_connection_pinned": True,
            },
            "research": {
                "available": True,
                "deterministic_extraction": True,
                "immutable_evidence": True,
                "review_required": True,
            },
            "editorial_generation": {"available": True, "versioned": True, "evidence_bound": True},
            "media": {"available": True, "remotion": True, "ffmpeg_qa": True, "exact_hash_approval": True},
            "publishing": {
                "available": True,
                "default_mode": "dry_run",
                "private_first": True,
                "resumable": True,
                "encrypted_oauth": True,
                "separate_public_release_approval": True,
            },
            "optimization": {
                "available": True,
                "analytics_snapshots": True,
                "benchmark_recommendations": True,
                "automatic_routing_changes": False,
                "freshness_corrections_originality": True,
                "budget_enforcement": True,
            },
            "observability_and_backup": {
                "available": True,
                "observability_profile": "optional",
                "restore_drill_required": True,
                "sbom": True,
            },
        },
    }


@router.get("/workflows", response_model=list[WorkflowSummaryView])
async def list_workflows(
    request: Request,
    _: Annotated[UserModel, Depends(require(Permission.VIEW))],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> list[WorkflowSummaryView]:
    bounded_limit = min(max(limit, 1), 100)
    records = list(
        await session.scalars(
            select(WorkflowControlRecordModel)
            .order_by(WorkflowControlRecordModel.created_at.desc())
            .limit(bounded_limit)
        )
    )
    statuses = await asyncio.gather(
        *(_execution_status(request, record.workflow_id) for record in records)
    )
    result: list[WorkflowSummaryView] = []
    for record, status in zip(records, statuses, strict=True):
        subject = await _workflow_subject(session, record.request_payload)
        channel_id = await _workflow_channel_id(session, record, subject)
        channel = await session.get(ChannelProfileModel, channel_id) if channel_id else None
        result.append(
            WorkflowSummaryView(
                workflow_id=record.workflow_id,
                workflow_type=record.workflow_type,
                execution_status=status,
                parent_workflow_id=record.parent_workflow_id,
                correlation_id=record.correlation_id,
                channel_profile_id=channel_id,
                channel_name=channel.name if channel else None,
                subject_profile_id=subject.id if subject else None,
                subject_name=subject.name if subject else None,
                created_at=record.created_at,
            )
        )
    return result


@router.post("/durability-probes", response_model=ProbeView, status_code=202)
async def start_probe(
    payload: ProbeStart,
    request: Request,
    actor: Annotated[UserModel, Depends(require(Permission.OPERATE_WORKFLOWS))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProbeView:
    workflow_id = f"durability-probe-{payload.idempotency_key}"
    gateway = _gateway(request)
    await gateway.start_probe(workflow_id=workflow_id, correlation_id=request.state.correlation_id)
    await append_audit(
        session,
        action="workflow.durability_probe_started",
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=workflow_id,
        correlation_id=request.state.correlation_id,
        context={"idempotency_key": payload.idempotency_key},
    )
    await session.commit()
    return ProbeView.model_validate(await gateway.describe_probe(workflow_id))


@router.get("/durability-probes/{workflow_id}", response_model=ProbeView)
async def get_probe(
    workflow_id: str,
    request: Request,
    _: Annotated[UserModel, Depends(require(Permission.VIEW))],
) -> ProbeView:
    try:
        return ProbeView.model_validate(await _gateway(request).describe_probe(workflow_id))
    except RPCError as exc:
        raise HTTPException(status_code=404, detail="Workflow not found") from exc


@router.post("/durability-probes/{workflow_id}/complete", response_model=ProbeView)
async def complete_probe(
    workflow_id: str,
    request: Request,
    actor: Annotated[UserModel, Depends(require(Permission.OPERATE_WORKFLOWS))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProbeView:
    gateway = _gateway(request)
    current = await gateway.describe_probe(workflow_id)
    if current.get("state") != "COMPLETED":
        await gateway.complete_probe(workflow_id)
    await append_audit(
        session,
        action=(
            "workflow.durability_probe_completion_reconciled"
            if current.get("state") == "COMPLETED"
            else "workflow.durability_probe_completed"
        ),
        actor_id=actor.id,
        target_type="temporal_workflow",
        target_id=workflow_id,
        correlation_id=request.state.correlation_id,
    )
    await session.commit()
    return ProbeView.model_validate(
        current if current.get("state") == "COMPLETED" else await gateway.describe_probe(workflow_id)
    )
