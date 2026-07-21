from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission
from youtuber_api.db import get_session
from youtuber_api.models import AuditEventModel, UserModel
from youtuber_api.schemas import AuditEventView
from youtuber_api.security import require

router = APIRouter(prefix="/audit-events", tags=["audit"])


@router.get("", response_model=list[AuditEventView])
async def list_audit_events(
    _: Annotated[UserModel, Depends(require(Permission.VIEW))],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditEventModel]:
    events = await session.scalars(
        select(AuditEventModel).order_by(AuditEventModel.occurred_at.desc()).limit(limit)
    )
    return list(events)
