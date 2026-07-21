from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission
from editorial_core.discovery import SubjectBrief, plan_search
from youtuber_api.audit import append_audit
from youtuber_api.db import get_session
from youtuber_api.models import ChannelProfileModel, SubjectProfileModel, UserModel
from youtuber_api.schemas import (
    ArchivedChannelProfileView,
    ArchivedSubjectProfileView,
    ChannelProfileUpdate,
    ChannelProfileView,
    ChannelProfileWrite,
    ProfileArchiveView,
    SearchPlanView,
    SubjectScheduleView,
    SubjectProfileUpdate,
    SubjectProfileView,
    SubjectProfileWrite,
)
from youtuber_api.schedule_gateway import SubjectScheduleGateway
from youtuber_api.security import require

router = APIRouter(tags=["profiles"])
Viewer = Annotated[UserModel, Depends(require(Permission.VIEW))]
Editor = Annotated[UserModel, Depends(require(Permission.EDIT_EDITORIAL))]
Operator = Annotated[UserModel, Depends(require(Permission.OPERATE_WORKFLOWS))]
ProfileModel = TypeVar("ProfileModel", ChannelProfileModel, SubjectProfileModel)


async def _commit_profile(
    *,
    session: AsyncSession,
    request: Request,
    actor: UserModel,
    profile: ProfileModel,
    action: str,
    context: dict[str, object] | None = None,
) -> ProfileModel:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Profile conflicts with an existing record") from exc
    await append_audit(
        session,
        action=action,
        actor_id=actor.id,
        target_type=profile.__tablename__.removesuffix("s"),
        target_id=str(profile.id),
        correlation_id=request.state.correlation_id,
        context={
            "version": profile.version,
            "enabled": profile.enabled,
            "archived_at": profile.deleted_at.isoformat() if profile.deleted_at else None,
            **(context or {}),
        },
    )
    await session.commit()
    return profile


@router.get("/channel-profiles", response_model=list[ChannelProfileView])
async def list_channels(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[ChannelProfileModel]:
    return list(
        await session.scalars(
            select(ChannelProfileModel)
            .where(ChannelProfileModel.deleted_at.is_(None))
            .order_by(ChannelProfileModel.name)
        )
    )


@router.get("/channel-profiles/archived", response_model=list[ArchivedChannelProfileView])
async def list_archived_channels(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[ArchivedChannelProfileView]:
    profiles = list(
        await session.scalars(
            select(ChannelProfileModel)
            .where(ChannelProfileModel.deleted_at.is_not(None))
            .order_by(ChannelProfileModel.deleted_at.desc(), ChannelProfileModel.name)
        )
    )
    return [
        ArchivedChannelProfileView(
            **ChannelProfileView.model_validate(profile).model_dump(),
            archived_at=profile.deleted_at,
        )
        for profile in profiles
    ]


@router.post("/channel-profiles", response_model=ChannelProfileView, status_code=201)
async def create_channel(
    payload: ChannelProfileWrite,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChannelProfileModel:
    now = datetime.now(timezone.utc)
    profile = ChannelProfileModel(
        **payload.model_dump(), created_by=actor.id, version=1, created_at=now, updated_at=now
    )
    session.add(profile)
    return await _commit_profile(
        session=session, request=request, actor=actor, profile=profile, action="channel_profile.created"
    )


@router.put("/channel-profiles/{profile_id}", response_model=ChannelProfileView)
async def update_channel(
    profile_id: UUID,
    payload: ChannelProfileUpdate,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChannelProfileModel:
    profile = await session.get(ChannelProfileModel, profile_id, with_for_update=True)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Channel profile not found")
    if profile.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="Profile changed; reload before saving")
    for name, value in payload.model_dump(exclude={"expected_version"}).items():
        setattr(profile, name, value)
    profile.version += 1
    profile.updated_at = datetime.now(timezone.utc)
    return await _commit_profile(
        session=session, request=request, actor=actor, profile=profile, action="channel_profile.updated"
    )


@router.delete("/channel-profiles/{profile_id}", response_model=ProfileArchiveView)
async def archive_channel(
    profile_id: UUID,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
    expected_version: Annotated[int, Query(ge=1)],
) -> ProfileArchiveView:
    profile = await session.get(ChannelProfileModel, profile_id, with_for_update=True)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Channel profile not found")
    if profile.version != expected_version:
        raise HTTPException(status_code=409, detail="Profile changed; reload before archiving")
    active_subjects = list(
        await session.scalars(
            select(SubjectProfileModel)
            .where(
                SubjectProfileModel.channel_profile_id == profile.id,
                SubjectProfileModel.deleted_at.is_(None),
            )
            .order_by(SubjectProfileModel.name)
        )
    )
    if active_subjects:
        names = ", ".join(subject.name for subject in active_subjects[:5])
        suffix = "" if len(active_subjects) <= 5 else f" and {len(active_subjects) - 5} more"
        raise HTTPException(
            status_code=409,
            detail=f"Archive or reassign the channel's subject profiles first: {names}{suffix}",
        )
    archived_at = datetime.now(timezone.utc)
    profile.enabled = False
    profile.deleted_at = archived_at
    profile.updated_at = archived_at
    profile.version += 1
    await _commit_profile(
        session=session,
        request=request,
        actor=actor,
        profile=profile,
        action="channel_profile.archived",
        context={"active_subject_count": 0},
    )
    return ProfileArchiveView(id=profile.id, version=profile.version, archived_at=archived_at)


@router.post("/channel-profiles/{profile_id}/restore", response_model=ChannelProfileView)
async def restore_channel(
    profile_id: UUID,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
    expected_version: Annotated[int, Query(ge=1)],
) -> ChannelProfileModel:
    profile = await session.get(ChannelProfileModel, profile_id, with_for_update=True)
    if profile is None or profile.deleted_at is None:
        raise HTTPException(status_code=404, detail="Archived channel profile not found")
    if profile.version != expected_version:
        raise HTTPException(status_code=409, detail="Profile changed; reload before restoring")
    restored_at = datetime.now(timezone.utc)
    profile.enabled = False
    profile.deleted_at = None
    profile.updated_at = restored_at
    profile.version += 1
    return await _commit_profile(
        session=session,
        request=request,
        actor=actor,
        profile=profile,
        action="channel_profile.restored",
        context={"restored_at": restored_at.isoformat(), "enabled_after_restore": False},
    )


@router.get("/subject-profiles", response_model=list[SubjectProfileView])
async def list_subjects(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[SubjectProfileModel]:
    return list(
        await session.scalars(
            select(SubjectProfileModel)
            .where(SubjectProfileModel.deleted_at.is_(None))
            .order_by(SubjectProfileModel.name)
        )
    )


@router.get("/subject-profiles/archived", response_model=list[ArchivedSubjectProfileView])
async def list_archived_subjects(
    _: Viewer, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[ArchivedSubjectProfileView]:
    profiles = list(
        await session.scalars(
            select(SubjectProfileModel)
            .where(SubjectProfileModel.deleted_at.is_not(None))
            .order_by(SubjectProfileModel.deleted_at.desc(), SubjectProfileModel.name)
        )
    )
    return [
        ArchivedSubjectProfileView(
            **SubjectProfileView.model_validate(profile).model_dump(),
            archived_at=profile.deleted_at,
        )
        for profile in profiles
    ]


@router.post("/subject-profiles", response_model=SubjectProfileView, status_code=201)
async def create_subject(
    payload: SubjectProfileWrite,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SubjectProfileModel:
    channel = await session.get(ChannelProfileModel, payload.channel_profile_id)
    if channel is None or channel.deleted_at is not None:
        raise HTTPException(status_code=422, detail="Channel profile does not exist")
    now = datetime.now(timezone.utc)
    profile = SubjectProfileModel(
        **payload.model_dump(), created_by=actor.id, version=1, created_at=now, updated_at=now
    )
    session.add(profile)
    return await _commit_profile(
        session=session, request=request, actor=actor, profile=profile, action="subject_profile.created"
    )


@router.put("/subject-profiles/{profile_id}", response_model=SubjectProfileView)
async def update_subject(
    profile_id: UUID,
    payload: SubjectProfileUpdate,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SubjectProfileModel:
    profile = await session.get(SubjectProfileModel, profile_id, with_for_update=True)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Subject profile not found")
    if profile.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="Profile changed; reload before saving")
    channel = await session.get(ChannelProfileModel, payload.channel_profile_id)
    if channel is None or channel.deleted_at is not None:
        raise HTTPException(status_code=422, detail="Channel profile does not exist")
    for name, value in payload.model_dump(exclude={"expected_version"}).items():
        setattr(profile, name, value)
    profile.version += 1
    profile.updated_at = datetime.now(timezone.utc)
    return await _commit_profile(
        session=session, request=request, actor=actor, profile=profile, action="subject_profile.updated"
    )


@router.delete("/subject-profiles/{profile_id}", response_model=ProfileArchiveView)
async def archive_subject(
    profile_id: UUID,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
    expected_version: Annotated[int, Query(ge=1)],
) -> ProfileArchiveView:
    profile = await session.get(SubjectProfileModel, profile_id, with_for_update=True)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Subject profile not found")
    if profile.version != expected_version:
        raise HTTPException(status_code=409, detail="Profile changed; reload before archiving")
    policy = profile.schedule or {"cron": None, "timezone": "UTC"}
    schedule_state = await SubjectScheduleGateway(request.app.state.temporal_client).reconcile(
        subject_profile_id=profile.id,
        cron=policy.get("cron"),
        timezone_name=policy.get("timezone", "UTC"),
        enabled=False,
    )
    archived_at = datetime.now(timezone.utc)
    profile.enabled = False
    profile.deleted_at = archived_at
    profile.updated_at = archived_at
    profile.version += 1
    await _commit_profile(
        session=session,
        request=request,
        actor=actor,
        profile=profile,
        action="subject_profile.archived",
        context={
            "schedule_id": schedule_state["schedule_id"],
            "schedule_exists": schedule_state["exists"],
            "schedule_paused": schedule_state["paused"],
        },
    )
    return ProfileArchiveView(id=profile.id, version=profile.version, archived_at=archived_at)


@router.post("/subject-profiles/{profile_id}/restore", response_model=SubjectProfileView)
async def restore_subject(
    profile_id: UUID,
    request: Request,
    actor: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
    expected_version: Annotated[int, Query(ge=1)],
) -> SubjectProfileModel:
    profile = await session.get(SubjectProfileModel, profile_id, with_for_update=True)
    if profile is None or profile.deleted_at is None:
        raise HTTPException(status_code=404, detail="Archived subject profile not found")
    if profile.version != expected_version:
        raise HTTPException(status_code=409, detail="Profile changed; reload before restoring")
    channel = await session.get(ChannelProfileModel, profile.channel_profile_id, with_for_update=True)
    if channel is None or channel.deleted_at is not None:
        raise HTTPException(status_code=409, detail="Restore the subject's channel first")
    restored_at = datetime.now(timezone.utc)
    profile.enabled = False
    profile.deleted_at = None
    profile.updated_at = restored_at
    profile.version += 1
    return await _commit_profile(
        session=session,
        request=request,
        actor=actor,
        profile=profile,
        action="subject_profile.restored",
        context={
            "restored_at": restored_at.isoformat(),
            "enabled_after_restore": False,
            "schedule_kept_paused": True,
        },
    )


@router.post("/subject-profiles/{profile_id}/test-search-plan", response_model=SearchPlanView)
async def test_search_plan(
    profile_id: UUID,
    _: Editor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SearchPlanView:
    profile = await session.get(SubjectProfileModel, profile_id)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Subject profile not found")
    plan = plan_search(
        SubjectBrief(
            topic=profile.topic,
            research_goal=profile.research_goal,
            seed_queries=tuple(profile.seed_queries),
            related_concepts=tuple(profile.related_concepts),
            negative_keywords=tuple(profile.negative_keywords),
            languages=tuple(profile.languages),
            regions=tuple(profile.regions),
            expected_primary_source_types=tuple(
                profile.source_requirements.get("expected_primary_types", ("official record", "original study"))
            ),
        )
    )
    return SearchPlanView(
        subject_topic=plan.subject_topic,
        strategies=[strategy.__dict__ for strategy in plan.strategies],
        expected_primary_source_types=list(plan.expected_primary_source_types),
        falsification_queries=list(plan.falsification_queries),
    )


@router.get(
    "/subject-profiles/{profile_id}/schedule", response_model=SubjectScheduleView
)
async def get_subject_schedule(
    profile_id: UUID,
    request: Request,
    _: Viewer,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SubjectScheduleView:
    profile = await session.get(SubjectProfileModel, profile_id)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Subject profile not found")
    return SubjectScheduleView.model_validate(
        await SubjectScheduleGateway(request.app.state.temporal_client).describe(profile.id)
    )


@router.post(
    "/subject-profiles/{profile_id}/schedule/reconcile",
    response_model=SubjectScheduleView,
)
async def reconcile_subject_schedule(
    profile_id: UUID,
    request: Request,
    actor: Operator,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SubjectScheduleView:
    profile = await session.get(SubjectProfileModel, profile_id)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Subject profile not found")
    policy = profile.schedule or {"cron": None, "timezone": "UTC"}
    result = await SubjectScheduleGateway(request.app.state.temporal_client).reconcile(
        subject_profile_id=profile.id,
        cron=policy.get("cron"),
        timezone_name=policy.get("timezone", "UTC"),
        enabled=profile.enabled,
    )
    await append_audit(
        session,
        action="subject_schedule.reconciled",
        actor_id=actor.id,
        target_type="temporal_schedule",
        target_id=result["schedule_id"],
        correlation_id=request.state.correlation_id,
        context={
            "subject_profile_id": str(profile.id),
            "profile_version": profile.version,
            "cron": policy.get("cron"),
            "timezone": policy.get("timezone", "UTC"),
            "paused": result["paused"],
        },
    )
    await session.commit()
    return SubjectScheduleView.model_validate(result)
