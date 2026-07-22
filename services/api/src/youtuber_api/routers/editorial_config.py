from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import WorkflowFailureError

from editorial_core.authorization import Permission
from youtuber_api.audit import append_audit
from youtuber_api.db import get_session
from youtuber_api.editorial_gateway import TemporalEditorialGateway
from youtuber_api.models import (
    AIModelModel,
    AIUsageRecordModel,
    PromptTemplateHeadModel,
    PromptTemplateModel,
    ProviderModel,
    TaskModelAssignmentHeadModel,
    TaskModelAssignmentModel,
    UserModel,
)
from youtuber_api.schemas import (
    AIModelUpdate,
    AIModelView,
    AIModelWrite,
    AIUsageView,
    PromptTemplateView,
    PromptTemplateWrite,
    ProviderUpdate,
    ProviderModelDiscoveryView,
    ProviderView,
    ProviderWrite,
    TaskModelAssignmentView,
    TaskModelAssignmentWrite,
)
from youtuber_api.security import require


router = APIRouter(tags=["editorial-configuration"])
ProviderAdmin = Annotated[UserModel, Depends(require(Permission.MANAGE_PROVIDERS))]


_ADVISORY_PROMPTS: dict[str, dict[str, Any]] = {
    "topic_qualifier": {
        "key": "system.topic_qualifier",
        "system": (
            "You are an advisory topic qualifier for an evidence-first explainer channel. "
            "Judge only the supplied candidate and channel brief. Do not invent facts, sources, "
            "or a final score. Semantic duplication and evidence gates are handled outside the model. "
            "Use abstained=true when the input is too thin. Return only schema-valid JSON."
        ),
        "input": {
            "type": "object",
            "required": ["candidate", "channel", "subject", "deterministic_trace"],
            "properties": {
                "candidate": {"type": "object"},
                "channel": {"type": "object"},
                "subject": {"type": "object"},
                "deterministic_trace": {"type": "object"},
            },
            "additionalProperties": False,
        },
        "response": {
            "type": "object",
            "required": ["dimensions", "confidence", "abstained", "rationale", "uncertainty"],
            "properties": {
                "dimensions": {
                    "type": "object",
                    "required": ["explainer_need", "audience_relevance", "video_suitability", "channel_fit", "angle_originality"],
                    "properties": {name: {"type": "number", "minimum": 0, "maximum": 100} for name in ("explainer_need", "audience_relevance", "video_suitability", "channel_fit", "angle_originality")},
                    "additionalProperties": False,
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "abstained": {"type": "boolean"},
                "rationale": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "uncertainty": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            },
            "additionalProperties": False,
        },
    },
    "evidence_reviewer": {
        "key": "system.evidence_reviewer",
        "system": (
            "You are a source-bound evidence reviewer. Assess each supplied claim only against its "
            "exact excerpts and source metadata. Distinguish support, context, and contradiction; "
            "flag causal overreach, method and transferability limits, dependence, and missing "
            "counterevidence. Never approve a claim or dossier and never invent an excerpt. Use "
            "abstained=true if the excerpts are insufficient. Return one plain JSON object only, "
            "without markdown or reasoning outside the fields. Express every claim and source "
            "assessment as one concise string beginning with the supplied ID."
        ),
        "input": {
            "type": "object",
            "required": ["dossier", "claims"],
            "properties": {"dossier": {"type": "object"}, "claims": {"type": "array"}},
            "additionalProperties": False,
        },
        "response": {
            "type": "object",
            "required": ["claim_assessments", "source_assessments", "methodological_limits", "counterevidence_gaps", "confidence", "abstained", "uncertainty"],
            "properties": {
                "claim_assessments": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                "source_assessments": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                "methodological_limits": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                "counterevidence_gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "abstained": {"type": "boolean"},
                "uncertainty": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
            },
            "additionalProperties": False,
        },
    },
    "evidence_synthesizer": {
        "key": "system.evidence_synthesizer",
        "system": (
            "You synthesize review candidates only from supplied exact excerpts. Create atomic, "
            "cautious subject-matter claims that directly answer the research question and assigned "
            "explanation coverage units. Treat current_claim_bank as already retained work: do not "
            "repeat or lightly paraphrase it. Work primarily on target_coverage_units when supplied, "
            "and aim for two or three non-overlapping factual atoms per target unit when the excerpts "
            "support them. Never return an editorial or document-description meta-claim "
            "such as what a learning item could include, what a paper discusses, or what could be "
            "compared. Reserve central=true for only three to five thesis-level claims across the "
            "complete claim bank; mark mechanisms, examples, limits, definitions, and other useful "
            "details central=false. Every central "
            "claim must cite at least two supporting excerpt IDs with different independence_key "
            "values. Use context or contradicts when an excerpt does not directly support the claim. "
            "Never invent evidence, IDs, numbers, quotations, or source independence. Do not approve "
            "anything; a human reviewer remains mandatory. A title, catalog field, identifier, or other "
            "metadata is context only and cannot directly support a content claim. Every supporting "
            "excerpt must itself contain the factual substance of the claim. Never list examples, "
            "methods, numbers, or effects unless the cited excerpts explicitly contain them. Prefer "
            "qualified wording over causal or universal claims. When readiness_policy is supplied, "
            "attempt at least its minimum_factual_claims as distinct atomic claims and cover every "
            "essential explanation unit; never pad the count with paraphrases or meta-claims. Set "
            "abstained=true if the required direct evidence is unavailable. Even when abstained=true, "
            "return claims as an empty array and include every other required response field. "
            "Return one plain schema-valid JSON object without markdown."
        ),
        "input": {
            "type": "object",
            "required": ["research_question", "explanation_plan", "sources"],
            "properties": {
                "research_question": {"type": "object"},
                "explanation_plan": {"type": "array", "maxItems": 20},
                "readiness_policy": {"type": "object"},
                "current_claim_bank": {"type": "array", "maxItems": 30},
                "target_coverage_units": {"type": "array", "maxItems": 20},
                "review_feedback": {"type": "object"},
                "sources": {"type": "array", "maxItems": 20},
            },
            "additionalProperties": False,
        },
        "response": {
            "type": "object",
            "required": ["claims", "confidence", "abstained", "uncertainty"],
            "properties": {
                "claims": {
                    "type": "array",
                    "maxItems": 30,
                    "items": {
                        "type": "object",
                            "required": ["statement", "claim_type", "central", "coverage_unit_ids", "evidence"],
                        "properties": {
                            "statement": {"type": "string", "minLength": 20, "maxLength": 700},
                            "claim_type": {"enum": ["fact", "inference", "opinion"]},
                            "central": {"type": "boolean"},
                            "coverage_unit_ids": {
                                "type": "array", "minItems": 1, "maxItems": 4,
                                "items": {"type": "string", "minLength": 3, "maxLength": 80},
                            },
                            "evidence": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 12,
                                "items": {
                                    "type": "object",
                                    "required": ["evidence_id", "relationship"],
                                    "properties": {
                                        "evidence_id": {"type": "string", "minLength": 10, "maxLength": 180},
                                        "relationship": {"enum": ["supports", "contradicts", "context"]},
                                    },
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "abstained": {"type": "boolean"},
                "uncertainty": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            },
            "additionalProperties": False,
        },
    },
    "research_query_planner": {
        "key": "system.research_query_planner",
        "system": (
            "Plan a topic-specific explanation before planning evidence searches. Produce at least "
            "six concise coverage units that together support a no-prior-knowledge explanation: "
            "foundation, mechanism, evidence, limits, alternatives or implications, and open questions. "
            "When current_readiness.existing_coverage_units is non-empty, this is an enrichment round: "
            "preserve those coverage unit IDs and meanings exactly, do not redesign the explanation "
            "plan, and focus searches on uncovered units and the supplied review_feedback. "
            "Then return concise search-engine queries, not answers, and bind every query to one or more "
            "coverage unit IDs. The first two queries must be English-language scholarly-catalog queries: "
            "first an original/primary-research query and then an independent review query. Translate "
            "the topic's central technical concepts into established English terminology for both, "
            "while retaining the concrete subject instead of broadening to a merely adjacent field. "
            "Then cover the subject language when useful. Include distinct roles "
            "for original or primary research, official data or documentation, an independent review, "
            "and limitations or counterevidence. Use concrete concepts and synonyms instead of quoting "
            "the complete candidate title. Every query must retain at least two central topic concepts; "
            "country names and generic words such as study, challenge, technology, or analysis do not "
            "count as central concepts. Do not invent source URLs, findings, or facts. Set "
            "abstained=true if the topic is too ambiguous. Return one plain schema-valid JSON object."
        ),
        "input": {
            "type": "object",
            "required": ["candidate", "subject", "current_readiness"],
            "properties": {
                "candidate": {"type": "object"},
                "subject": {"type": "object"},
                "current_readiness": {"type": "object"},
            },
            "additionalProperties": False,
        },
        "response": {
            "type": "object",
            "required": ["coverage_units", "queries", "confidence", "abstained", "uncertainty"],
            "properties": {
                "coverage_units": {
                    "type": "array", "minItems": 6, "maxItems": 9,
                    "items": {
                        "type": "object",
                        "required": ["id", "question", "role", "essential"],
                        "properties": {
                            "id": {"type": "string", "minLength": 3, "maxLength": 80},
                            "question": {"type": "string", "minLength": 10, "maxLength": 500},
                            "role": {"enum": ["foundation", "mechanism", "evidence", "limits", "alternatives", "implications", "open_questions"]},
                            "essential": {"type": "boolean"},
                        },
                        "additionalProperties": False,
                    },
                },
                "queries": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "required": ["query", "role", "language", "coverage_unit_ids"],
                        "properties": {
                            "query": {"type": "string", "minLength": 5, "maxLength": 300},
                            "role": {"enum": ["primary", "official", "independent", "counterevidence"]},
                            "language": {"type": "string", "minLength": 2, "maxLength": 20},
                            "coverage_unit_ids": {
                                "type": "array", "minItems": 1, "maxItems": 6,
                                "items": {"type": "string", "minLength": 3, "maxLength": 80},
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "abstained": {"type": "boolean"},
                "uncertainty": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            },
            "additionalProperties": False,
        },
    },
}


async def _ensure_advisory_prompt(session: AsyncSession, task_type: str, actor_id: UUID) -> None:
    definition = _ADVISORY_PROMPTS.get(task_type)
    if definition is None:
        return
    now = datetime.now(timezone.utc)
    payload = {
        "template_key": definition["key"],
        "task_type": task_type,
        "system_instructions": definition["system"],
        "template": "Structured input:\n{{structured_input_json}}\n\nRequired response schema:\n{{response_schema_json}}",
        "input_schema": definition["input"],
        "response_schema": definition["response"],
    }
    content_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    template = await session.scalar(
        select(PromptTemplateModel).where(
            PromptTemplateModel.template_key == definition["key"],
            PromptTemplateModel.content_hash == content_hash,
        )
    )
    if template is None:
        maximum = await session.scalar(
            select(func.max(PromptTemplateModel.template_version)).where(
                PromptTemplateModel.template_key == definition["key"]
            )
        )
        template = PromptTemplateModel(
            **payload,
            template_version=(maximum or 0) + 1,
            content_hash=content_hash,
            created_by=actor_id,
            created_at=now,
            comment="Reconciled system default for deterministic hybrid AI review",
        )
        session.add(template)
        await session.flush()
    head = await session.get(
        PromptTemplateHeadModel, definition["key"], with_for_update=True
    )
    if head is None:
        session.add(
            PromptTemplateHeadModel(
                template_key=definition["key"],
                active_template_id=template.id,
                updated_at=now,
            )
        )
    elif head.active_template_id != template.id:
        head.active_template_id = template.id
        head.updated_at = now


def _provider_view(provider: ProviderModel) -> ProviderView:
    return ProviderView(
        id=provider.id,
        version=provider.version,
        slug=provider.slug,
        name=provider.name,
        driver_type=provider.driver_type,
        endpoint=provider.endpoint,
        enabled=provider.enabled,
        location=provider.location,
        authentication_scheme=provider.authentication_scheme,
        has_secret=provider.secret_reference is not None,
        data_policy=provider.data_policy,
        residency_policy=provider.residency_policy,
        capabilities=provider.capabilities,
        concurrency_limit=provider.concurrency_limit,
        requests_per_minute=provider.requests_per_minute,
        health_status=provider.health_status,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


async def _commit_or_conflict(session: AsyncSession, detail: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=detail) from exc


async def _flush_or_conflict(session: AsyncSession, detail: str) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=detail) from exc


@router.get("/providers", response_model=list[ProviderView])
async def list_providers(
    _: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ProviderView]:
    providers = list(
        await session.scalars(
            select(ProviderModel)
            .where(ProviderModel.deleted_at.is_(None))
            .order_by(ProviderModel.name)
        )
    )
    return [_provider_view(provider) for provider in providers]


@router.post(
    "/providers/{provider_id}/discover-models",
    response_model=ProviderModelDiscoveryView,
)
async def discover_models(
    provider_id: UUID,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProviderModelDiscoveryView:
    provider = await session.get(ProviderModel, provider_id)
    if provider is None or provider.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Provider not found")
    workflow_id = f"provider-model-discovery-{uuid4()}"
    try:
        result = await TemporalEditorialGateway(
            request.app.state.temporal_client
        ).discover_provider_models(
            {"workflow_id": workflow_id, "provider_id": str(provider_id)}
        )
    except WorkflowFailureError as exc:
        raise HTTPException(
            status_code=503,
            detail="The provider inventory is temporarily unreachable; the stored configuration was not changed.",
        ) from exc
    await append_audit(
        session,
        action="provider.models_discovered",
        actor_id=actor.id,
        target_type="provider",
        target_id=str(provider_id),
        correlation_id=request.state.correlation_id,
        context={
            "workflow_id": workflow_id,
            "model_count": result["model_count"],
        },
    )
    await session.commit()
    return ProviderModelDiscoveryView.model_validate(result)


@router.post("/providers", response_model=ProviderView, status_code=201)
async def create_provider(
    payload: ProviderWrite,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProviderView:
    now = datetime.now(timezone.utc)
    provider = ProviderModel(
        **payload.model_dump(),
        health_status={"state": "unverified", "checked_at": None},
        created_by=actor.id,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(provider)
    await _flush_or_conflict(session, "Provider slug already exists")
    await append_audit(
        session,
        action="provider.created",
        actor_id=actor.id,
        target_type="provider",
        target_id=str(provider.id),
        correlation_id=request.state.correlation_id,
        context={
            "driver_type": provider.driver_type,
            "location": provider.location,
            "data_policy": provider.data_policy,
            "enabled": provider.enabled,
            "secret_reference": provider.secret_reference,
        },
    )
    await _commit_or_conflict(session, "Provider slug already exists")
    return _provider_view(provider)


@router.put("/providers/{provider_id}", response_model=ProviderView)
async def update_provider(
    provider_id: UUID,
    payload: ProviderUpdate,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProviderView:
    provider = await session.get(ProviderModel, provider_id, with_for_update=True)
    if provider is None or provider.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="Provider changed; reload before saving")
    for name, value in payload.model_dump(exclude={"expected_version"}).items():
        setattr(provider, name, value)
    provider.health_status = {"state": "unverified", "checked_at": None}
    provider.version += 1
    provider.updated_at = datetime.now(timezone.utc)
    await append_audit(
        session,
        action="provider.updated",
        actor_id=actor.id,
        target_type="provider",
        target_id=str(provider.id),
        correlation_id=request.state.correlation_id,
        context={"version": provider.version, "enabled": provider.enabled},
    )
    await _commit_or_conflict(session, "Provider conflicts with an existing provider")
    return _provider_view(provider)


async def _validate_model_provider(
    session: AsyncSession, payload: AIModelWrite
) -> ProviderModel:
    provider = await session.get(ProviderModel, payload.provider_id)
    if provider is None or provider.deleted_at is not None:
        raise HTTPException(status_code=422, detail="Provider does not exist")
    if payload.enabled and not provider.enabled:
        raise HTTPException(status_code=409, detail="Enable the provider before enabling a model")
    policy_order = {"local_only": 0, "remote_after_redaction": 1, "remote_allowed": 2}
    if (
        payload.data_policy_override is not None
        and policy_order[payload.data_policy_override] > policy_order[provider.data_policy]
    ):
        raise HTTPException(status_code=422, detail="Model data policy cannot loosen provider policy")
    return provider


@router.get("/models", response_model=list[AIModelView])
async def list_models(
    _: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AIModelModel]:
    return list(
        await session.scalars(
            select(AIModelModel)
            .where(AIModelModel.deleted_at.is_(None))
            .order_by(AIModelModel.display_name)
        )
    )


@router.get("/ai-usage", response_model=list[AIUsageView])
async def list_ai_usage(
    _: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AIUsageView]:
    rows = (
        await session.execute(
            select(
                AIUsageRecordModel,
                ProviderModel.name.label("provider_name"),
                AIModelModel.display_name.label("model_name"),
            )
            .join(ProviderModel, ProviderModel.id == AIUsageRecordModel.provider_id)
            .join(AIModelModel, AIModelModel.id == AIUsageRecordModel.model_id)
            .order_by(AIUsageRecordModel.created_at.desc())
            .limit(100)
        )
    ).all()
    return [
        AIUsageView(
            id=item.id,
            workflow_id=item.workflow_id,
            activity_id=item.activity_id,
            task_type=item.task_type,
            provider_id=item.provider_id,
            provider_name=provider_name,
            model_id=item.model_id,
            model_name=model_name,
            prompt_template_id=item.prompt_template_id,
            request_hash=item.request_hash,
            response_hash=item.response_hash,
            input_tokens=item.input_tokens,
            output_tokens=item.output_tokens,
            latency_ms=item.latency_ms,
            cost=item.cost,
            redaction_summary=item.redaction_summary,
            correlation_id=item.correlation_id,
            created_at=item.created_at,
        )
        for item, provider_name, model_name in rows
    ]


@router.post("/models", response_model=AIModelView, status_code=201)
async def create_model(
    payload: AIModelWrite,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AIModelModel:
    await _validate_model_provider(session, payload)
    now = datetime.now(timezone.utc)
    model = AIModelModel(
        **payload.model_dump(),
        created_by=actor.id,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(model)
    await _flush_or_conflict(session, "Provider model name already exists")
    await append_audit(
        session,
        action="model.created",
        actor_id=actor.id,
        target_type="model",
        target_id=str(model.id),
        correlation_id=request.state.correlation_id,
        context={
            "provider_id": str(model.provider_id),
            "model_name": model.model_name,
            "visible": model.visible,
            "enabled": model.enabled,
        },
    )
    await _commit_or_conflict(session, "Provider model name already exists")
    return model


@router.put("/models/{model_id}", response_model=AIModelView)
async def update_model(
    model_id: UUID,
    payload: AIModelUpdate,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AIModelModel:
    model = await session.get(AIModelModel, model_id, with_for_update=True)
    if model is None or model.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Model not found")
    if model.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="Model changed; reload before saving")
    await _validate_model_provider(session, payload)
    for name, value in payload.model_dump(exclude={"expected_version"}).items():
        setattr(model, name, value)
    model.version += 1
    model.updated_at = datetime.now(timezone.utc)
    await append_audit(
        session,
        action="model.updated",
        actor_id=actor.id,
        target_type="model",
        target_id=str(model.id),
        correlation_id=request.state.correlation_id,
        context={"version": model.version, "visible": model.visible, "enabled": model.enabled},
    )
    await _commit_or_conflict(session, "Model conflicts with an existing provider model")
    return model


def _assignment_view(
    assignment: TaskModelAssignmentModel, active_id: UUID | None
) -> TaskModelAssignmentView:
    return TaskModelAssignmentView(
        id=assignment.id,
        task_type=assignment.task_type,
        assignment_version=assignment.assignment_version,
        primary_model_id=assignment.primary_model_id,
        fallback_model_ids=assignment.fallback_model_ids,
        routing_policy=assignment.routing_policy,
        budget_policy=assignment.budget_policy,
        created_by=assignment.created_by,
        created_at=assignment.created_at,
        comment=assignment.comment,
        active=assignment.id == active_id,
    )


@router.get("/task-model-assignments", response_model=list[TaskModelAssignmentView])
async def list_task_assignments(
    _: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TaskModelAssignmentView]:
    heads = {
        head.task_type: head.active_assignment_id
        for head in await session.scalars(select(TaskModelAssignmentHeadModel))
    }
    assignments = list(
        await session.scalars(
            select(TaskModelAssignmentModel).order_by(
                TaskModelAssignmentModel.task_type,
                TaskModelAssignmentModel.assignment_version.desc(),
            )
        )
    )
    return [_assignment_view(item, heads.get(item.task_type)) for item in assignments]


@router.put("/task-model-assignments/{task_type}", response_model=TaskModelAssignmentView)
async def write_task_assignment(
    task_type: str,
    payload: TaskModelAssignmentWrite,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskModelAssignmentView:
    if task_type != payload.task_type:
        raise HTTPException(status_code=422, detail="Task type path and body must match")
    await _ensure_advisory_prompt(session, task_type, actor.id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"task-model-assignment:{task_type}"},
    )
    model_ids = [payload.primary_model_id, *payload.fallback_model_ids]
    models = list(
        await session.scalars(
            select(AIModelModel).where(
                AIModelModel.id.in_(model_ids),
                AIModelModel.deleted_at.is_(None),
                AIModelModel.visible.is_(True),
                AIModelModel.enabled.is_(True),
            )
        )
    )
    if len(models) != len(model_ids):
        raise HTTPException(status_code=422, detail="Every assigned model must be visible and enabled")
    providers = list(
        await session.scalars(
            select(ProviderModel).where(
                ProviderModel.id.in_({model.provider_id for model in models}),
                ProviderModel.enabled.is_(True),
                ProviderModel.deleted_at.is_(None),
            )
        )
    )
    if len({provider.id for provider in providers}) != len({model.provider_id for model in models}):
        raise HTTPException(status_code=422, detail="Every assigned model provider must be enabled")
    for model in models:
        tasks = model.capabilities.get("tasks", [])
        if tasks and task_type not in tasks:
            raise HTTPException(status_code=422, detail=f"Model {model.display_name} does not enable this task")
    if task_type == "script_verifier":
        writer_head = await session.get(TaskModelAssignmentHeadModel, "script_writer")
        if writer_head is not None:
            writer = await session.get(TaskModelAssignmentModel, writer_head.active_assignment_id)
            if (
                writer is not None
                and writer.primary_model_id == payload.primary_model_id
                and not payload.routing_policy.get("allow_same_model_if_no_alternative", False)
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Verifier must differ from the writer unless the no-alternative policy is explicit",
                )
    maximum = await session.scalar(
        select(func.max(TaskModelAssignmentModel.assignment_version)).where(
            TaskModelAssignmentModel.task_type == task_type
        )
    )
    now = datetime.now(timezone.utc)
    assignment = TaskModelAssignmentModel(
        task_type=task_type,
        assignment_version=(maximum or 0) + 1,
        primary_model_id=payload.primary_model_id,
        fallback_model_ids=[str(value) for value in payload.fallback_model_ids],
        routing_policy=payload.routing_policy,
        budget_policy=payload.budget_policy,
        created_by=actor.id,
        created_at=now,
        comment=payload.comment,
    )
    session.add(assignment)
    await session.flush()
    head = await session.get(TaskModelAssignmentHeadModel, task_type, with_for_update=True)
    if head is None:
        head = TaskModelAssignmentHeadModel(
            task_type=task_type,
            active_assignment_id=assignment.id,
            updated_at=now,
        )
        session.add(head)
    else:
        head.active_assignment_id = assignment.id
        head.updated_at = now
    await append_audit(
        session,
        action="task_model_assignment.activated",
        actor_id=actor.id,
        target_type="task_model_assignment",
        target_id=str(assignment.id),
        correlation_id=request.state.correlation_id,
        context={
            "task_type": task_type,
            "assignment_version": assignment.assignment_version,
            "primary_model_id": str(assignment.primary_model_id),
        },
    )
    await session.commit()
    return _assignment_view(assignment, assignment.id)


def _prompt_hash(payload: PromptTemplateWrite) -> str:
    canonical = json.dumps(
        payload.model_dump(exclude={"comment"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _prompt_view(
    template: PromptTemplateModel, active_id: UUID | None
) -> PromptTemplateView:
    return PromptTemplateView(
        id=template.id,
        template_key=template.template_key,
        task_type=template.task_type,
        template_version=template.template_version,
        system_instructions=template.system_instructions,
        template=template.template,
        input_schema=template.input_schema,
        response_schema=template.response_schema,
        content_hash=template.content_hash,
        created_by=template.created_by,
        created_at=template.created_at,
        comment=template.comment,
        active=template.id == active_id,
    )


@router.get("/prompt-templates", response_model=list[PromptTemplateView])
async def list_prompt_templates(
    _: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[PromptTemplateView]:
    heads = {
        head.template_key: head.active_template_id
        for head in await session.scalars(select(PromptTemplateHeadModel))
    }
    templates = list(
        await session.scalars(
            select(PromptTemplateModel).order_by(
                PromptTemplateModel.template_key,
                PromptTemplateModel.template_version.desc(),
            )
        )
    )
    return [_prompt_view(item, heads.get(item.template_key)) for item in templates]


@router.put("/prompt-templates/{template_key}", response_model=PromptTemplateView)
async def write_prompt_template(
    template_key: str,
    payload: PromptTemplateWrite,
    request: Request,
    actor: ProviderAdmin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PromptTemplateView:
    if template_key != payload.template_key:
        raise HTTPException(status_code=422, detail="Template key path and body must match")
    serialized_size = len(json.dumps(payload.model_dump(), ensure_ascii=False).encode())
    if serialized_size > 1_000_000:
        raise HTTPException(status_code=422, detail="Prompt template document exceeds one megabyte")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"prompt-template:{template_key}"},
    )
    prior_task_type = await session.scalar(
        select(PromptTemplateModel.task_type)
        .where(PromptTemplateModel.template_key == template_key)
        .limit(1)
    )
    if prior_task_type is not None and prior_task_type != payload.task_type:
        raise HTTPException(status_code=409, detail="A prompt template key cannot change task type")
    content_hash = _prompt_hash(payload)
    existing = await session.scalar(
        select(PromptTemplateModel).where(
            PromptTemplateModel.template_key == template_key,
            PromptTemplateModel.content_hash == content_hash,
        )
    )
    now = datetime.now(timezone.utc)
    if existing is None:
        maximum = await session.scalar(
            select(func.max(PromptTemplateModel.template_version)).where(
                PromptTemplateModel.template_key == template_key
            )
        )
        existing = PromptTemplateModel(
            template_key=template_key,
            task_type=payload.task_type,
            template_version=(maximum or 0) + 1,
            system_instructions=payload.system_instructions,
            template=payload.template,
            input_schema=payload.input_schema,
            response_schema=payload.response_schema,
            content_hash=content_hash,
            created_by=actor.id,
            created_at=now,
            comment=payload.comment,
        )
        session.add(existing)
        await session.flush()
    head = await session.get(PromptTemplateHeadModel, template_key, with_for_update=True)
    if head is None:
        head = PromptTemplateHeadModel(
            template_key=template_key,
            active_template_id=existing.id,
            updated_at=now,
        )
        session.add(head)
    else:
        head.active_template_id = existing.id
        head.updated_at = now
    await append_audit(
        session,
        action="prompt_template.activated",
        actor_id=actor.id,
        target_type="prompt_template",
        target_id=str(existing.id),
        correlation_id=request.state.correlation_id,
        context={
            "template_key": template_key,
            "template_version": existing.template_version,
            "content_hash": content_hash,
        },
    )
    await session.commit()
    return _prompt_view(existing, existing.id)
