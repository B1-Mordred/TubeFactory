from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission
from youtuber_api.audit import append_audit
from youtuber_api.db import get_session
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
    ProviderView,
    ProviderWrite,
    TaskModelAssignmentView,
    TaskModelAssignmentWrite,
)
from youtuber_api.security import require


router = APIRouter(tags=["editorial-configuration"])
ProviderAdmin = Annotated[UserModel, Depends(require(Permission.MANAGE_PROVIDERS))]


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
