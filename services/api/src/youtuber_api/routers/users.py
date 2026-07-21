from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission
from youtuber_api.audit import append_audit
from youtuber_api.db import get_session
from youtuber_api.models import UserModel, UserRole
from youtuber_api.schemas import UserCreate, UserUpdate, UserView
from youtuber_api.security import hash_password, require

router = APIRouter(prefix="/users", tags=["users"])
AdminUser = Annotated[UserModel, Depends(require(Permission.MANAGE_USERS))]


@router.get("", response_model=list[UserView])
async def list_users(
    _: AdminUser, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[UserModel]:
    result = await session.scalars(
        select(UserModel).where(UserModel.deleted_at.is_(None)).order_by(UserModel.username)
    )
    return list(result)


@router.post("", response_model=UserView, status_code=201)
async def create_user(
    payload: UserCreate,
    request: Request,
    actor: AdminUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserModel:
    now = datetime.now(timezone.utc)
    user = UserModel(
        username=payload.username.lower(),
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        identity_provider="local",
        oidc_subject=None,
        role=UserRole(payload.role.value),
        totp_secret_encrypted=None,
        totp_enabled=False,
        totp_recovery_hashes=[],
        enabled=True,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from exc
    await append_audit(
        session,
        action="user.created",
        actor_id=actor.id,
        target_type="user",
        target_id=str(user.id),
        correlation_id=request.state.correlation_id,
        context={"username": user.username, "role": user.role.value},
    )
    await session.commit()
    return user


@router.patch("/{user_id}", response_model=UserView)
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    request: Request,
    actor: AdminUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserModel:
    user = await session.get(UserModel, user_id, with_for_update=True)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="User changed; reload before saving")
    if user.id == actor.id and payload.enabled is False:
        raise HTTPException(status_code=409, detail="An administrator cannot disable their own session")
    changes = payload.model_dump(exclude_none=True, exclude={"expected_version"})
    for key, value in changes.items():
        if key == "role":
            value = UserRole(value.value)
        setattr(user, key, value)
    user.version += 1
    user.updated_at = datetime.now(timezone.utc)
    await append_audit(
        session,
        action="user.updated",
        actor_id=actor.id,
        target_type="user",
        target_id=str(user.id),
        correlation_id=request.state.correlation_id,
        context={"fields": sorted(changes)},
    )
    await session.commit()
    return user
