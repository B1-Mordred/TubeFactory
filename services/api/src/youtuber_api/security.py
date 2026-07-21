from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission, Role, role_allows
from youtuber_api.config import get_settings
from youtuber_api.db import get_session
from youtuber_api.models import UserModel

SESSION_COOKIE = "editorial_session"
CSRF_COOKIE = "editorial_csrf"

password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def issue_session(user: UserModel) -> tuple[str, str]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    csrf_token = secrets.token_urlsafe(32)
    token = jwt.encode(
        {
            "sub": str(user.id),
            "role": user.role.value,
            "iat": now,
            "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
            "jti": secrets.token_hex(16),
            "csrf": csrf_token,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    return token, csrf_token


async def current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> UserModel:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        user_id = UUID(payload["sub"])
    except (InvalidTokenError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from exc
    user = await session.scalar(
        select(UserModel).where(
            UserModel.id == user_id,
            UserModel.enabled.is_(True),
            UserModel.deleted_at.is_(None),
        )
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User unavailable")
    return user


def require(permission: Permission):
    async def dependency(
        request: Request,
        user: Annotated[UserModel, Depends(current_user)],
        token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> UserModel:
        if not role_allows(Role(user.role.value), permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permission")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
            except (InvalidTokenError, TypeError) as exc:
                raise HTTPException(status_code=401, detail="Invalid session") from exc
            csrf_cookie = request.cookies.get(CSRF_COOKIE)
            expected = payload.get("csrf")
            if not csrf_header or not csrf_cookie or not expected:
                raise HTTPException(status_code=403, detail="CSRF token required")
            if not secrets.compare_digest(csrf_header, csrf_cookie) or not secrets.compare_digest(
                csrf_header, expected
            ):
                raise HTTPException(status_code=403, detail="CSRF token mismatch")
        return user

    return dependency


CurrentUser = Annotated[UserModel, Depends(current_user)]
