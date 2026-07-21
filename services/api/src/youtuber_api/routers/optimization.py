from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission
from editorial_core.optimization import (
    BenchmarkCandidate,
    OptimizationPolicyError,
    canonical_hash,
    correction_action,
    enforce_budget,
    evaluate_freshness,
    evaluate_originality,
    recommend_model,
)
from youtuber_api.audit import append_audit
from youtuber_api.db import get_session
from youtuber_api.models import (
    AIModelModel,
    AnalyticsMetricSnapshotModel,
    BudgetPolicyHeadModel,
    BudgetPolicyVersionModel,
    BudgetUsageRecordModel,
    ChannelProfileModel,
    ClaimEvidenceModel,
    ClaimModel,
    CorrectionRecordModel,
    EvidenceExcerptModel,
    ModelBenchmarkRunModel,
    ModelRecommendationApplicationModel,
    ModelRecommendationDecisionModel,
    ModelRecommendationModel,
    OperationalEvidenceModel,
    OriginalityReportModel,
    PublicationModel,
    ScriptVersionModel,
    ScriptSegmentModel,
    SegmentClaimModel,
    SourceFreshnessCheckModel,
    SourceSnapshotModel,
    TaskModelAssignmentHeadModel,
    TaskModelAssignmentModel,
    UserModel,
)
from youtuber_api.schemas import (
    AnalyticsSnapshotWrite,
    BudgetPolicyWrite,
    BudgetUsageWrite,
    CorrectionWrite,
    FreshnessCheckWrite,
    ModelBenchmarkWrite,
    OriginalityReportWrite,
    OperationalEvidenceWrite,
    RecommendationApplyWrite,
    RecommendationDecisionWrite,
)
from youtuber_api.security import require


router = APIRouter(prefix="/optimization", tags=["optimization"])
Viewer = Annotated[UserModel, Depends(require(Permission.VIEW))]
Operator = Annotated[UserModel, Depends(require(Permission.OPERATE_WORKFLOWS))]
PolicyAdmin = Annotated[UserModel, Depends(require(Permission.MANAGE_POLICIES))]
ProviderAdmin = Annotated[UserModel, Depends(require(Permission.MANAGE_PROVIDERS))]
Reviewer = Annotated[UserModel, Depends(require(Permission.REVIEW))]


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@router.post("/analytics/snapshots", status_code=201)
async def ingest_analytics_snapshot(
    payload: AnalyticsSnapshotWrite,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    if await session.get(ChannelProfileModel, payload.channel_profile_id) is None:
        raise HTTPException(status_code=404, detail="Channel profile not found")
    if payload.publication_id is not None:
        publication = await session.get(PublicationModel, payload.publication_id)
        if publication is None:
            raise HTTPException(status_code=404, detail="Publication not found")
        if publication.youtube_video_id != payload.youtube_video_id:
            raise HTTPException(status_code=422, detail="Publication and analytics video ID do not match")
    document = payload.model_dump(mode="json")
    content_hash = canonical_hash(document)
    existing = await session.scalar(
        select(AnalyticsMetricSnapshotModel).where(
            AnalyticsMetricSnapshotModel.channel_profile_id == payload.channel_profile_id,
            AnalyticsMetricSnapshotModel.youtube_video_id == payload.youtube_video_id,
            AnalyticsMetricSnapshotModel.period_start == payload.period_start,
            AnalyticsMetricSnapshotModel.period_end == payload.period_end,
            AnalyticsMetricSnapshotModel.content_hash == content_hash,
        )
    )
    if existing is not None:
        return {"id": str(existing.id), "content_hash": existing.content_hash, "reconciled": True}
    now = datetime.now(timezone.utc)
    snapshot = AnalyticsMetricSnapshotModel(
        channel_profile_id=payload.channel_profile_id,
        publication_id=payload.publication_id,
        youtube_video_id=payload.youtube_video_id,
        period_start=payload.period_start,
        period_end=payload.period_end,
        metrics=payload.metrics,
        dimensions=payload.dimensions,
        source=payload.source,
        content_hash=content_hash,
        ingested_by=actor.id,
        created_at=now,
    )
    session.add(snapshot)
    await session.flush()
    await append_audit(
        session, action="analytics.snapshot_ingested", actor_id=actor.id,
        target_type="analytics_metric_snapshot", target_id=str(snapshot.id),
        correlation_id=request.state.correlation_id,
        context={"youtube_video_id": payload.youtube_video_id, "content_hash": content_hash, "source": payload.source},
    )
    await session.commit()
    return {"id": str(snapshot.id), "content_hash": content_hash, "reconciled": False}


@router.get("/analytics/dashboard")
async def analytics_dashboard(
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
    channel_profile_id: UUID | None = None,
) -> dict[str, Any]:
    statement = select(AnalyticsMetricSnapshotModel).order_by(AnalyticsMetricSnapshotModel.period_end.desc()).limit(500)
    if channel_profile_id is not None:
        statement = statement.where(AnalyticsMetricSnapshotModel.channel_profile_id == channel_profile_id)
    rows = list(await session.scalars(statement))
    totals: dict[str, float] = {}
    for row in rows:
        for name, value in row.metrics.items():
            if isinstance(value, (int, float)):
                totals[name] = round(totals.get(name, 0) + float(value), 6)
    latest = max((row.period_end for row in rows), default=None)
    return {
        "snapshot_count": len(rows), "video_count": len({row.youtube_video_id for row in rows}),
        "latest_period_end": _iso(latest) if latest else None, "totals": totals,
        "provenance": [
            {"id": str(row.id), "content_hash": row.content_hash, "source": row.source, "created_at": _iso(row.created_at)}
            for row in rows[:25]
        ],
    }


def _recommendation_view(
    recommendation: ModelRecommendationModel,
    decision: ModelRecommendationDecisionModel | None,
    application: ModelRecommendationApplicationModel | None,
) -> dict[str, Any]:
    return {
        "id": str(recommendation.id), "benchmark_run_id": str(recommendation.benchmark_run_id),
        "task_type": recommendation.task_type, "recommended_model_id": str(recommendation.recommended_model_id),
        "recommended_fallback_model_ids": recommendation.recommended_fallback_model_ids,
        "baseline_assignment_id": str(recommendation.baseline_assignment_id) if recommendation.baseline_assignment_id else None,
        "score": recommendation.score, "reasoning": recommendation.reasoning,
        "recommendation_hash": recommendation.recommendation_hash,
        "decision": decision.decision if decision else "pending",
        "decision_id": str(decision.id) if decision else None,
        "applied": application is not None,
        "new_assignment_id": str(application.new_assignment_id) if application else None,
        "created_at": _iso(recommendation.created_at),
    }


async def _latest_decision(session: AsyncSession, recommendation_id: UUID) -> ModelRecommendationDecisionModel | None:
    return await session.scalar(
        select(ModelRecommendationDecisionModel)
        .where(ModelRecommendationDecisionModel.recommendation_id == recommendation_id)
        .order_by(ModelRecommendationDecisionModel.created_at.desc(), ModelRecommendationDecisionModel.id.desc())
        .limit(1)
    )


@router.post("/benchmarks", status_code=201)
async def run_benchmark(
    payload: ModelBenchmarkWrite,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    model_ids = [candidate.model_id for candidate in payload.candidates]
    models = list(await session.scalars(select(AIModelModel).where(AIModelModel.id.in_(model_ids))))
    if len(models) != len(model_ids) or any(not model.enabled or not model.visible or model.deleted_at for model in models):
        raise HTTPException(status_code=422, detail="Every benchmark model must exist, be enabled, and be visible")
    for model in models:
        tasks = model.capabilities.get("tasks", [])
        if tasks and payload.task_type not in tasks:
            raise HTTPException(status_code=422, detail=f"Model {model.display_name} does not enable this task")
    try:
        result = recommend_model(
            [
                BenchmarkCandidate(
                    str(item.model_id), item.quality_score, item.success_rate,
                    item.p95_latency_ms, item.mean_cost_usd, item.policy_eligible,
                )
                for item in payload.candidates
            ],
            quality_weight=payload.weights["quality"], reliability_weight=payload.weights["reliability"],
            latency_weight=payload.weights["latency"], cost_weight=payload.weights["cost"],
        )
    except OptimizationPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    head = await session.get(TaskModelAssignmentHeadModel, payload.task_type)
    baseline_id = head.active_assignment_id if head else None
    benchmark_document = payload.model_dump(mode="json")
    benchmark_hash = canonical_hash(benchmark_document)
    now = datetime.now(timezone.utc)
    run = ModelBenchmarkRunModel(
        task_type=payload.task_type, suite_key=payload.suite_key, suite_version=payload.suite_version,
        candidates=benchmark_document["candidates"], weights=payload.weights, input_hash=benchmark_hash,
        baseline_assignment_id=baseline_id, created_by=actor.id,
        correlation_id=request.state.correlation_id, created_at=now,
    )
    session.add(run)
    await session.flush()
    recommendation_document = {
        "benchmark_run_id": str(run.id), "task_type": payload.task_type,
        "recommended_model_id": result.model_id,
        "recommended_fallback_model_ids": list(result.fallback_model_ids),
        "baseline_assignment_id": str(baseline_id) if baseline_id else None,
        "score": result.score, "reasoning": list(result.reasoning),
    }
    recommendation = ModelRecommendationModel(
        benchmark_run_id=run.id, task_type=payload.task_type,
        recommended_model_id=UUID(result.model_id), baseline_assignment_id=baseline_id,
        recommended_fallback_model_ids=list(result.fallback_model_ids),
        score=result.score, reasoning=list(result.reasoning),
        recommendation_hash=canonical_hash(recommendation_document), created_at=now,
    )
    session.add(recommendation)
    await session.flush()
    await append_audit(
        session, action="model_recommendation.created", actor_id=actor.id,
        target_type="model_recommendation", target_id=str(recommendation.id),
        correlation_id=request.state.correlation_id,
        context={"task_type": payload.task_type, "recommended_model_id": result.model_id, "routing_changed": False},
    )
    await session.commit()
    return _recommendation_view(recommendation, None, None)


@router.get("/recommendations")
async def list_recommendations(
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    recommendations = list(await session.scalars(select(ModelRecommendationModel).order_by(ModelRecommendationModel.created_at.desc()).limit(200)))
    output = []
    for recommendation in recommendations:
        decision = await _latest_decision(session, recommendation.id)
        application = await session.scalar(select(ModelRecommendationApplicationModel).where(ModelRecommendationApplicationModel.recommendation_id == recommendation.id))
        output.append(_recommendation_view(recommendation, decision, application))
    return output


@router.post("/recommendations/{recommendation_id}/decision", status_code=201)
async def decide_recommendation(
    recommendation_id: UUID,
    payload: RecommendationDecisionWrite,
    request: Request,
    actor: Reviewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    recommendation = await session.get(ModelRecommendationModel, recommendation_id)
    if recommendation is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    if await session.scalar(select(ModelRecommendationApplicationModel.id).where(ModelRecommendationApplicationModel.recommendation_id == recommendation_id)):
        raise HTTPException(status_code=409, detail="An applied recommendation cannot be decided again")
    decision = ModelRecommendationDecisionModel(
        recommendation_id=recommendation_id, decision=payload.decision, reason=payload.reason,
        actor_id=actor.id, correlation_id=request.state.correlation_id, created_at=datetime.now(timezone.utc),
    )
    session.add(decision)
    await session.flush()
    await append_audit(
        session, action=f"model_recommendation.{payload.decision}", actor_id=actor.id,
        target_type="model_recommendation", target_id=str(recommendation_id),
        correlation_id=request.state.correlation_id, context={"reason": payload.reason},
    )
    await session.commit()
    return _recommendation_view(recommendation, decision, None)


@router.post("/recommendations/{recommendation_id}/apply", status_code=201)
async def apply_recommendation(
    recommendation_id: UUID,
    payload: RecommendationApplyWrite,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"model-recommendation:{recommendation_id}"})
    recommendation = await session.get(ModelRecommendationModel, recommendation_id)
    if recommendation is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    existing = await session.scalar(select(ModelRecommendationApplicationModel).where(ModelRecommendationApplicationModel.recommendation_id == recommendation_id))
    if existing is not None:
        decision = await session.get(ModelRecommendationDecisionModel, existing.decision_id)
        return _recommendation_view(recommendation, decision, existing)
    decision = await _latest_decision(session, recommendation_id)
    if decision is None or decision.decision != "approved":
        raise HTTPException(status_code=409, detail="The latest recommendation decision must be approved")
    head = await session.get(TaskModelAssignmentHeadModel, recommendation.task_type, with_for_update=True)
    current_id = head.active_assignment_id if head else None
    if payload.expected_baseline_assignment_id != current_id or recommendation.baseline_assignment_id != current_id:
        raise HTTPException(status_code=409, detail="Active routing changed after the benchmark; rerun the benchmark")
    recommended_ids = [recommendation.recommended_model_id, *[UUID(value) for value in recommendation.recommended_fallback_model_ids]]
    recommended_models = list(await session.scalars(select(AIModelModel).where(AIModelModel.id.in_(recommended_ids))))
    if len(recommended_models) != len(recommended_ids) or any(not model.enabled or not model.visible or model.deleted_at for model in recommended_models):
        raise HTTPException(status_code=409, detail="Recommended model is no longer eligible")
    prior = await session.get(TaskModelAssignmentModel, current_id) if current_id else None
    maximum = await session.scalar(select(func.max(TaskModelAssignmentModel.assignment_version)).where(TaskModelAssignmentModel.task_type == recommendation.task_type))
    now = datetime.now(timezone.utc)
    assignment = TaskModelAssignmentModel(
        task_type=recommendation.task_type, assignment_version=(maximum or 0) + 1,
        primary_model_id=recommendation.recommended_model_id,
        fallback_model_ids=recommendation.recommended_fallback_model_ids,
        routing_policy=(prior.routing_policy if prior else {}), budget_policy=(prior.budget_policy if prior else {}),
        created_by=actor.id, created_at=now, comment=payload.comment,
    )
    session.add(assignment)
    await session.flush()
    if head is None:
        head = TaskModelAssignmentHeadModel(task_type=recommendation.task_type, active_assignment_id=assignment.id, updated_at=now)
        session.add(head)
    else:
        head.active_assignment_id = assignment.id
        head.updated_at = now
    application = ModelRecommendationApplicationModel(
        recommendation_id=recommendation_id, decision_id=decision.id, previous_assignment_id=current_id,
        new_assignment_id=assignment.id, actor_id=actor.id, correlation_id=request.state.correlation_id, created_at=now,
    )
    session.add(application)
    await session.flush()
    await append_audit(
        session, action="model_recommendation.applied", actor_id=actor.id,
        target_type="model_recommendation", target_id=str(recommendation_id),
        correlation_id=request.state.correlation_id,
        context={"decision_id": str(decision.id), "previous_assignment_id": str(current_id) if current_id else None, "new_assignment_id": str(assignment.id)},
    )
    await session.commit()
    return _recommendation_view(recommendation, decision, application)


@router.post("/freshness-checks", status_code=201)
async def create_freshness_check(
    payload: FreshnessCheckWrite, request: Request, actor: PolicyAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    snapshot = await session.get(SourceSnapshotModel, payload.source_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Source snapshot not found")
    result = evaluate_freshness(retrieved_at=snapshot.retrieved_at, maximum_age=timedelta(seconds=payload.maximum_age_seconds))
    record = SourceFreshnessCheckModel(
        source_snapshot_id=snapshot.id, maximum_age_seconds=result.maximum_age_seconds,
        age_seconds=result.age_seconds, verdict=result.verdict.value, reason=result.reason,
        policy={"maximum_age_seconds": payload.maximum_age_seconds}, checked_by=actor.id, created_at=datetime.now(timezone.utc),
    )
    session.add(record)
    await session.flush()
    await append_audit(session, action="source.freshness_checked", actor_id=actor.id, target_type="source_snapshot", target_id=str(snapshot.id), correlation_id=request.state.correlation_id, context={"verdict": result.verdict.value, "reason": result.reason})
    await session.commit()
    return {"id": str(record.id), "verdict": record.verdict, "age_seconds": record.age_seconds, "maximum_age_seconds": record.maximum_age_seconds, "reason": record.reason}


@router.post("/originality-reports", status_code=201)
async def create_originality_report(
    payload: OriginalityReportWrite, request: Request, actor: PolicyAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    if await session.get(ScriptVersionModel, payload.script_version_id) is None:
        raise HTTPException(status_code=404, detail="Script version not found")
    result = evaluate_originality(payload.comparisons, review_threshold=payload.review_threshold, block_threshold=payload.block_threshold)
    document = payload.model_dump(mode="json")
    report = OriginalityReportModel(
        script_version_id=payload.script_version_id, comparisons=payload.comparisons,
        maximum_overlap=result.maximum_overlap, verdict=result.verdict.value, reason=result.reason,
        policy={"review_threshold": payload.review_threshold, "block_threshold": payload.block_threshold},
        content_hash=canonical_hash(document), checked_by=actor.id, created_at=datetime.now(timezone.utc),
    )
    session.add(report)
    await session.flush()
    await append_audit(session, action="script.originality_checked", actor_id=actor.id, target_type="script_version", target_id=str(payload.script_version_id), correlation_id=request.state.correlation_id, context={"verdict": report.verdict, "content_hash": report.content_hash})
    await session.commit()
    return {"id": str(report.id), "verdict": report.verdict, "maximum_overlap": report.maximum_overlap, "reason": report.reason, "content_hash": report.content_hash}


@router.post("/corrections", status_code=201)
async def create_correction(
    payload: CorrectionWrite, request: Request, actor: PolicyAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    publication = await session.get(PublicationModel, payload.publication_id) if payload.publication_id else None
    if payload.publication_id and publication is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    if payload.script_version_id and await session.get(ScriptVersionModel, payload.script_version_id) is None:
        raise HTTPException(status_code=404, detail="Script version not found")
    snapshot = await session.get(SourceSnapshotModel, payload.source_snapshot_id) if payload.source_snapshot_id else None
    if payload.source_snapshot_id and snapshot is None:
        raise HTTPException(status_code=404, detail="Source snapshot not found")
    affected_claims: list[dict[str, Any]] = []
    affected_timecodes: list[dict[str, Any]] = []
    if snapshot is not None:
        claim_rows = list(
            await session.execute(
                select(ClaimModel.id, ClaimModel.normalized_statement, ClaimEvidenceModel.relationship)
                .join(ClaimEvidenceModel, ClaimEvidenceModel.claim_id == ClaimModel.id)
                .join(EvidenceExcerptModel, EvidenceExcerptModel.id == ClaimEvidenceModel.evidence_excerpt_id)
                .where(EvidenceExcerptModel.source_snapshot_id == snapshot.id)
                .distinct()
            )
        )
        affected_claims = [
            {"claim_id": str(claim_id), "statement": statement, "evidence_relationship": relationship}
            for claim_id, statement, relationship in claim_rows
        ]
        claim_ids = [claim_id for claim_id, _, _ in claim_rows]
        if claim_ids:
            statement = (
                select(ScriptSegmentModel, SegmentClaimModel.claim_id)
                .join(SegmentClaimModel, SegmentClaimModel.script_segment_id == ScriptSegmentModel.id)
                .where(SegmentClaimModel.claim_id.in_(claim_ids))
            )
            if payload.script_version_id:
                statement = statement.where(ScriptSegmentModel.script_version_id == payload.script_version_id)
            segment_rows = list(await session.execute(statement))
            starts: dict[UUID, dict[UUID, float]] = {}
            for version_id in {segment.script_version_id for segment, _ in segment_rows}:
                ordered = list(await session.scalars(select(ScriptSegmentModel).where(ScriptSegmentModel.script_version_id == version_id).order_by(ScriptSegmentModel.segment_order)))
                cursor = 0.0
                starts[version_id] = {}
                for segment in ordered:
                    starts[version_id][segment.id] = cursor
                    cursor += segment.duration_seconds
            affected_timecodes = [
                {
                    "script_version_id": str(segment.script_version_id), "script_segment_id": str(segment.id),
                    "segment_key": segment.segment_key, "claim_id": str(claim_id),
                    "start_seconds": round(starts[segment.script_version_id][segment.id], 3),
                    "end_seconds": round(starts[segment.script_version_id][segment.id] + segment.duration_seconds, 3),
                }
                for segment, claim_id in segment_rows
            ]
    action = correction_action(payload.severity, published=bool(publication and publication.youtube_video_id))
    proposal = {
        "change_kind": payload.change_kind, "required_action": action,
        "steps": [
            "reacquire_or_replace_the_changed_source", "review_each_affected_claim_and_contradiction",
            "regenerate_only_affected_unlocked_script_segments", "rerun_verification_qa_and_exact_version_approvals",
        ],
        "affected_claim_count": len(affected_claims), "affected_timecode_count": len(affected_timecodes),
    }
    record = CorrectionRecordModel(
        publication_id=payload.publication_id, script_version_id=payload.script_version_id,
        source_snapshot_id=payload.source_snapshot_id,
        severity=payload.severity, finding=payload.finding, required_action=action, evidence=payload.evidence,
        affected_claims=affected_claims, affected_timecodes=affected_timecodes, proposal=proposal,
        actor_id=actor.id, correlation_id=request.state.correlation_id, created_at=datetime.now(timezone.utc),
    )
    session.add(record)
    await session.flush()
    await append_audit(session, action="correction.recorded", actor_id=actor.id, target_type="correction", target_id=str(record.id), correlation_id=request.state.correlation_id, context={"severity": payload.severity, "required_action": action})
    await session.commit()
    return {"id": str(record.id), "severity": record.severity, "required_action": action, "affected_claims": affected_claims, "affected_timecodes": affected_timecodes, "proposal": proposal}


@router.get("/operational-evidence")
async def list_operational_evidence(
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    rows = list(await session.scalars(select(OperationalEvidenceModel).order_by(OperationalEvidenceModel.created_at.desc()).limit(200)))
    return [
        {"id": str(row.id), "evidence_kind": row.evidence_kind, "document": row.document,
         "content_hash": row.content_hash, "artifact_hash": row.artifact_hash,
         "recorded_by": str(row.recorded_by), "correlation_id": row.correlation_id, "created_at": _iso(row.created_at)}
        for row in rows
    ]


@router.post("/operational-evidence", status_code=201)
async def record_operational_evidence(
    payload: OperationalEvidenceWrite, request: Request, actor: PolicyAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    content_hash = canonical_hash(payload.document)
    existing = await session.scalar(select(OperationalEvidenceModel).where(OperationalEvidenceModel.evidence_kind == payload.evidence_kind, OperationalEvidenceModel.content_hash == content_hash))
    if existing is not None:
        return {"id": str(existing.id), "content_hash": existing.content_hash, "reconciled": True}
    row = OperationalEvidenceModel(
        evidence_kind=payload.evidence_kind, document=payload.document, content_hash=content_hash,
        artifact_hash=payload.artifact_hash, recorded_by=actor.id,
        correlation_id=request.state.correlation_id, created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    await append_audit(session, action="operations.evidence_recorded", actor_id=actor.id, target_type="operational_evidence", target_id=str(row.id), correlation_id=request.state.correlation_id, context={"evidence_kind": payload.evidence_kind, "content_hash": content_hash, "artifact_hash": payload.artifact_hash})
    await session.commit()
    return {"id": str(row.id), "content_hash": content_hash, "reconciled": False}


@router.put("/budgets/{scope}")
async def activate_budget_policy(
    scope: str, payload: BudgetPolicyWrite, request: Request, actor: PolicyAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    if scope != payload.scope:
        raise HTTPException(status_code=422, detail="Budget scope path and body must match")
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"budget-policy:{scope}"})
    document = {"scope": scope, "currency": payload.currency, "limit_amount": str(payload.limit_amount), "period": payload.period}
    content_hash = canonical_hash(document)
    policy = await session.scalar(select(BudgetPolicyVersionModel).where(BudgetPolicyVersionModel.scope == scope, BudgetPolicyVersionModel.content_hash == content_hash))
    now = datetime.now(timezone.utc)
    if policy is None:
        maximum = await session.scalar(select(func.max(BudgetPolicyVersionModel.version_number)).where(BudgetPolicyVersionModel.scope == scope))
        policy = BudgetPolicyVersionModel(
            scope=scope, version_number=(maximum or 0) + 1, currency=payload.currency,
            limit_amount=payload.limit_amount, period=payload.period, document=document, content_hash=content_hash,
            created_by=actor.id, created_at=now, comment=payload.comment,
        )
        session.add(policy)
        await session.flush()
    head = await session.get(BudgetPolicyHeadModel, scope, with_for_update=True)
    if head is None:
        head = BudgetPolicyHeadModel(scope=scope, active_version_id=policy.id, updated_at=now)
        session.add(head)
    else:
        head.active_version_id = policy.id
        head.updated_at = now
    await append_audit(session, action="budget.policy_activated", actor_id=actor.id, target_type="budget_policy", target_id=str(policy.id), correlation_id=request.state.correlation_id, context={"scope": scope, "version": policy.version_number, "limit_amount": str(policy.limit_amount), "currency": policy.currency})
    await session.commit()
    return {"id": str(policy.id), "scope": scope, "version_number": policy.version_number, "currency": policy.currency, "limit_amount": str(policy.limit_amount), "period": policy.period, "content_hash": policy.content_hash}


@router.post("/budget-usage", status_code=201)
async def record_budget_usage(
    payload: BudgetUsageWrite, request: Request, actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"budget-usage:{payload.scope}"})
    existing = await session.scalar(select(BudgetUsageRecordModel).where(BudgetUsageRecordModel.scope == payload.scope, BudgetUsageRecordModel.idempotency_key == payload.idempotency_key))
    if existing is not None:
        return {"id": str(existing.id), "allowed": True, "reconciled": True, "amount": str(existing.amount)}
    head = await session.get(BudgetPolicyHeadModel, payload.scope)
    if head is None:
        raise HTTPException(status_code=409, detail="No active budget policy exists for this scope")
    policy = await session.get(BudgetPolicyVersionModel, head.active_version_id)
    if policy is None or policy.currency != payload.currency:
        raise HTTPException(status_code=409, detail="Budget currency does not match the active policy")
    spent = await session.scalar(select(func.coalesce(func.sum(BudgetUsageRecordModel.amount), 0)).where(BudgetUsageRecordModel.scope == payload.scope, BudgetUsageRecordModel.policy_version_id == policy.id))
    decision = enforce_budget(limit=Decimal(policy.limit_amount), spent=Decimal(spent), proposed=payload.amount)
    if not decision.allowed:
        await append_audit(session, action="budget.usage_blocked", actor_id=actor.id, target_type="budget_policy", target_id=str(policy.id), correlation_id=request.state.correlation_id, context={"scope": payload.scope, "proposed": str(payload.amount), "remaining": str(decision.remaining)})
        await session.commit()
        raise HTTPException(status_code=409, detail={"reason": decision.reason, "remaining": str(decision.remaining)})
    record = BudgetUsageRecordModel(
        scope=payload.scope, policy_version_id=policy.id, idempotency_key=payload.idempotency_key,
        amount=payload.amount, currency=payload.currency, category=payload.category,
        workflow_id=payload.workflow_id, details=payload.details, recorded_by=actor.id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(record)
    await session.flush()
    await append_audit(session, action="budget.usage_recorded", actor_id=actor.id, target_type="budget_usage", target_id=str(record.id), correlation_id=request.state.correlation_id, context={"scope": payload.scope, "amount": str(payload.amount), "remaining_before": str(decision.remaining)})
    await session.commit()
    return {"id": str(record.id), "allowed": True, "reconciled": False, "amount": str(record.amount), "remaining_before": str(decision.remaining)}
