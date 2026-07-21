from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import pyotp

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from youtuber_api.audit import append_audit
from youtuber_api.config import get_settings
from youtuber_api.db import get_session
from youtuber_api.models import UserModel, UserRole
from youtuber_api.identity_crypto import encrypt_identity_secret
from youtuber_api.identity_security import (
    authentication_key, clear_login_failures, consume_second_factor,
    generate_recovery_codes, record_login_failure, recovery_code_hash,
    require_login_not_throttled, verify_totp_code,
)
from youtuber_api.schemas import (
    BootstrapRequest, BootstrapStatus, LoginRequest, SessionView,
    TOTPDisable, TOTPEnrollmentConfirm, TOTPEnrollmentStart,
    TOTPEnrollmentView, TOTPRecoveryCodesView, UserView,
)
from editorial_core.authorization import Permission
from youtuber_api.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    CurrentUser,
    hash_password,
    issue_session,
    require,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


def _set_session_cookies(response: Response, token: str, csrf_token: str) -> None:
    secure = get_settings().cookie_secure
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=get_settings().jwt_ttl_minutes * 60,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        httponly=False,
        secure=secure,
        samesite="strict",
        max_age=get_settings().jwt_ttl_minutes * 60,
        path="/",
    )


@router.get("/bootstrap-status", response_model=BootstrapStatus)
async def bootstrap_status(session: Annotated[AsyncSession, Depends(get_session)]) -> BootstrapStatus:
    count = await session.scalar(select(func.count()).select_from(UserModel))
    return BootstrapStatus(required=count == 0)


@router.post("/bootstrap", response_model=SessionView, status_code=201)
async def bootstrap(
    payload: BootstrapRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SessionView:
    # Lock a stable PostgreSQL key so two first-user requests cannot both win.
    await session.execute(select(func.pg_advisory_xact_lock(73927401)))
    count = await session.scalar(select(func.count()).select_from(UserModel))
    if count:
        raise HTTPException(status_code=409, detail="Bootstrap is already complete")
    now = datetime.now(timezone.utc)
    user = UserModel(
        username=payload.username.lower(),
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        identity_provider="local",
        role=UserRole.ADMIN,
        enabled=True,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    await session.flush()
    await append_audit(
        session,
        action="auth.bootstrap_admin_created",
        actor_id=user.id,
        target_type="user",
        target_id=str(user.id),
        correlation_id=request.state.correlation_id,
        context={"username": user.username},
    )
    await session.commit()
    token, csrf_token = issue_session(user)
    _set_session_cookies(response, token, csrf_token)
    return SessionView(user=UserView.model_validate(user), csrf_token=csrf_token)


@router.post("/login", response_model=SessionView)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SessionView:
    client_host = request.client.host if request.client else "unknown"
    rate_key = authentication_key(payload.username, client_host)
    await require_login_not_throttled(session, rate_key)
    user = await session.scalar(
        select(UserModel).where(UserModel.username == payload.username.lower(), UserModel.deleted_at.is_(None))
    )
    password_ok = bool(user and user.enabled and user.identity_provider == "local" and verify_password(user.password_hash, payload.password))
    second_factor_ok, used_recovery = consume_second_factor(user, payload.totp_code) if password_ok and user else (False, False)
    if not password_ok or not second_factor_ok:
        await record_login_failure(session, rate_key)
        await append_audit(
            session,
            action="auth.login_failed",
            correlation_id=request.state.correlation_id,
            context={"username": payload.username.lower(), "factor": "password_or_totp"},
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    await clear_login_failures(session, rate_key)
    if user.password_hash and password_hasher_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        user.version += 1
        user.updated_at = datetime.now(timezone.utc)
    await append_audit(
        session,
        action="auth.login_succeeded",
        actor_id=user.id,
        target_type="user",
        target_id=str(user.id),
        correlation_id=request.state.correlation_id,
        context={"identity_provider": "local", "used_recovery_code": used_recovery},
    )
    await session.commit()
    token, csrf_token = issue_session(user)
    _set_session_cookies(response, token, csrf_token)
    return SessionView(user=UserView.model_validate(user), csrf_token=csrf_token)


@router.post("/totp/enroll", response_model=TOTPEnrollmentView)
async def start_totp_enrollment(
    payload: TOTPEnrollmentStart,
    request: Request,
    actor: Annotated[UserModel, Depends(require(Permission.VIEW))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TOTPEnrollmentView:
    if actor.identity_provider != "local" or not verify_password(actor.password_hash, payload.current_password):
        raise HTTPException(status_code=401, detail="Current password is invalid")
    secret = pyotp.random_base32()
    actor.totp_secret_encrypted = encrypt_identity_secret(secret)
    actor.totp_enabled = False
    actor.totp_recovery_hashes = []
    actor.version += 1
    actor.updated_at = datetime.now(timezone.utc)
    await append_audit(
        session, action="auth.totp_enrollment_started", actor_id=actor.id,
        target_type="user", target_id=str(actor.id), correlation_id=request.state.correlation_id,
    )
    await session.commit()
    return TOTPEnrollmentView(
        secret=secret,
        provisioning_uri=pyotp.TOTP(secret).provisioning_uri(name=actor.username, issuer_name=get_settings().app_name),
    )


@router.post("/totp/confirm", response_model=TOTPRecoveryCodesView)
async def confirm_totp_enrollment(
    payload: TOTPEnrollmentConfirm,
    request: Request,
    actor: Annotated[UserModel, Depends(require(Permission.VIEW))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TOTPRecoveryCodesView:
    if actor.totp_secret_encrypted is None or actor.totp_enabled:
        raise HTTPException(status_code=409, detail="No pending TOTP enrollment")
    if not verify_totp_code(actor.totp_secret_encrypted, payload.code):
        raise HTTPException(status_code=422, detail="TOTP code is invalid")
    codes = generate_recovery_codes()
    actor.totp_enabled = True
    actor.totp_recovery_hashes = [recovery_code_hash(code) for code in codes]
    actor.version += 1
    actor.updated_at = datetime.now(timezone.utc)
    await append_audit(
        session, action="auth.totp_enabled", actor_id=actor.id,
        target_type="user", target_id=str(actor.id), correlation_id=request.state.correlation_id,
        context={"recovery_code_count": len(codes)},
    )
    await session.commit()
    return TOTPRecoveryCodesView(recovery_codes=codes)


@router.post("/totp/disable", status_code=204, response_class=Response, response_model=None)
async def disable_totp(
    payload: TOTPDisable,
    request: Request,
    actor: Annotated[UserModel, Depends(require(Permission.VIEW))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    if actor.identity_provider != "local" or not verify_password(actor.password_hash, payload.current_password):
        raise HTTPException(status_code=401, detail="Current password is invalid")
    accepted, _ = consume_second_factor(actor, payload.code)
    if not actor.totp_enabled or not accepted:
        raise HTTPException(status_code=422, detail="TOTP or recovery code is invalid")
    actor.totp_secret_encrypted = None
    actor.totp_enabled = False
    actor.totp_recovery_hashes = []
    actor.version += 1
    actor.updated_at = datetime.now(timezone.utc)
    await append_audit(
        session, action="auth.totp_disabled", actor_id=actor.id,
        target_type="user", target_id=str(actor.id), correlation_id=request.state.correlation_id,
    )
    await session.commit()


def password_hasher_needs_rehash(password_hash: str) -> bool:
    from youtuber_api.security import password_hasher

    return password_hasher.check_needs_rehash(password_hash)


@router.get("/session", response_model=SessionView)
async def get_session_view(user: CurrentUser, request: Request) -> SessionView:
    csrf_token = request.cookies.get(CSRF_COOKIE, "")
    return SessionView(user=UserView.model_validate(user), csrf_token=csrf_token)


@router.post("/logout", status_code=204, response_class=Response, response_model=None)
async def logout(
    request: Request,
    response: Response,
    actor: Annotated[UserModel, Depends(require(Permission.VIEW))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    await append_audit(
        session,
        action="auth.logout",
        actor_id=actor.id,
        target_type="user",
        target_id=str(actor.id),
        correlation_id=request.state.correlation_id,
    )
    await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
