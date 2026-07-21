from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Permission
from youtuber_api.audit import append_audit
from youtuber_api.config import get_settings
from youtuber_api.db import get_session
from youtuber_api.identity_crypto import decrypt_identity_secret, encrypt_identity_secret, stable_secret_hash
from youtuber_api.identity_security import role_from_oidc_claim
from youtuber_api.models import (
    OIDCAuthenticationStateModel, OIDCConfigurationHeadModel,
    OIDCConfigurationVersionModel, UserModel, UserRole,
)
from youtuber_api.routers.auth import _set_session_cookies
from youtuber_api.schemas import OIDCConfigurationView, OIDCConfigurationWrite, OIDCStatusView
from youtuber_api.security import issue_session, require


router = APIRouter(prefix="/auth/oidc", tags=["authentication"])
Admin = Annotated[UserModel, Depends(require(Permission.MANAGE_SYSTEM))]
_ALLOWED_ID_TOKEN_ALGORITHMS = {"RS256", "RS384", "RS512", "ES256", "ES384", "EdDSA"}


def _canonical_hash(value: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _view(item: OIDCConfigurationVersionModel | None) -> OIDCConfigurationView:
    if item is None:
        return OIDCConfigurationView(
            version_number=0, enabled=False, issuer="", client_id="", has_client_secret=False,
            authorization_endpoint="", token_endpoint="", jwks_uri="", scopes=["openid", "profile", "email"],
            username_claim="preferred_username", display_name_claim="name", role_claim="groups",
            role_mapping={}, default_role=None,
        )
    return OIDCConfigurationView(
        id=item.id, version_number=item.version_number, enabled=item.enabled, issuer=item.issuer,
        client_id=item.client_id, has_client_secret=bool(item.client_secret_encrypted),
        authorization_endpoint=item.authorization_endpoint, token_endpoint=item.token_endpoint,
        jwks_uri=item.jwks_uri, scopes=item.scopes, username_claim=item.username_claim,
        display_name_claim=item.display_name_claim, role_claim=item.role_claim,
        role_mapping=item.role_mapping, default_role=item.default_role,
        content_hash=item.content_hash, created_at=item.created_at, comment=item.comment,
    )


async def _active_configuration(session: AsyncSession) -> OIDCConfigurationVersionModel | None:
    head = await session.get(OIDCConfigurationHeadModel, True)
    return await session.get(OIDCConfigurationVersionModel, head.active_version_id) if head else None


def _require_transport(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme == "http" and get_settings().app_environment == "production":
        raise HTTPException(status_code=422, detail="Production OIDC endpoints must use HTTPS")


@router.get("/status", response_model=OIDCStatusView)
async def oidc_status(session: Annotated[AsyncSession, Depends(get_session)]) -> OIDCStatusView:
    item = await _active_configuration(session)
    return OIDCStatusView(
        configured=item is not None, enabled=bool(item and item.enabled),
        issuer=item.issuer if item else None, version_number=item.version_number if item else None,
    )


@router.get("/configuration", response_model=OIDCConfigurationView)
async def get_oidc_configuration(_: Admin, session: Annotated[AsyncSession, Depends(get_session)]) -> OIDCConfigurationView:
    return _view(await _active_configuration(session))


@router.post("/configuration", response_model=OIDCConfigurationView)
async def configure_oidc(
    payload: OIDCConfigurationWrite,
    request: Request,
    actor: Admin,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OIDCConfigurationView:
    for endpoint in (payload.issuer, payload.authorization_endpoint, payload.token_endpoint, payload.jwks_uri):
        _require_transport(endpoint)
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('oidc-configuration'))"))
    active = await _active_configuration(session)
    if payload.client_secret is None:
        if active is None:
            raise HTTPException(status_code=422, detail="A client secret is required for the first OIDC configuration")
        secret_value = decrypt_identity_secret(active.client_secret_encrypted)
    else:
        secret_value = payload.client_secret
    document = {
        "enabled": payload.enabled, "issuer": payload.issuer.rstrip("/"), "client_id": payload.client_id,
        "client_secret_fingerprint": stable_secret_hash(secret_value),
        "authorization_endpoint": payload.authorization_endpoint, "token_endpoint": payload.token_endpoint,
        "jwks_uri": payload.jwks_uri, "scopes": payload.scopes,
        "username_claim": payload.username_claim, "display_name_claim": payload.display_name_claim,
        "role_claim": payload.role_claim,
        "role_mapping": {key: value.value for key, value in payload.role_mapping.items()},
        "default_role": payload.default_role.value if payload.default_role else None,
    }
    content_hash = _canonical_hash(document)
    item = await session.scalar(select(OIDCConfigurationVersionModel).where(OIDCConfigurationVersionModel.content_hash == content_hash))
    reused = item is not None
    now = datetime.now(timezone.utc)
    if item is None:
        version_number = (await session.scalar(select(func.max(OIDCConfigurationVersionModel.version_number))) or 0) + 1
        item = OIDCConfigurationVersionModel(
            id=uuid4(), version_number=version_number, enabled=payload.enabled,
            issuer=str(document["issuer"]), client_id=payload.client_id,
            client_secret_encrypted=encrypt_identity_secret(secret_value),
            authorization_endpoint=payload.authorization_endpoint, token_endpoint=payload.token_endpoint,
            jwks_uri=payload.jwks_uri, scopes=payload.scopes,
            username_claim=payload.username_claim, display_name_claim=payload.display_name_claim,
            role_claim=payload.role_claim, role_mapping=document["role_mapping"],
            default_role=document["default_role"], content_hash=content_hash,
            created_by=actor.id, created_at=now, comment=payload.comment,
        )
        session.add(item)
        await session.flush()
    head = await session.get(OIDCConfigurationHeadModel, True, with_for_update=True)
    if head is None:
        session.add(OIDCConfigurationHeadModel(singleton=True, active_version_id=item.id, updated_at=now))
    else:
        head.active_version_id = item.id
        head.updated_at = now
    await append_audit(
        session, action="auth.oidc_configuration_reactivated" if reused else "auth.oidc_configuration_activated",
        actor_id=actor.id, target_type="oidc_configuration_version", target_id=str(item.id),
        correlation_id=request.state.correlation_id,
        context={"version": item.version_number, "enabled": item.enabled, "issuer": item.issuer, "content_hash": item.content_hash},
    )
    await session.commit()
    return _view(item)


@router.get("/start")
async def start_oidc(request: Request, session: Annotated[AsyncSession, Depends(get_session)]):
    config = await _active_configuration(session)
    if config is None or not config.enabled:
        raise HTTPException(status_code=404, detail="OIDC authentication is not enabled")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect_uri = str(request.url_for("oidc_callback"))
    now = datetime.now(timezone.utc)
    session.add(OIDCAuthenticationStateModel(
        state_hash=stable_secret_hash(state), nonce_hash=stable_secret_hash(nonce),
        code_verifier_encrypted=encrypt_identity_secret(verifier), configuration_version_id=config.id,
        redirect_uri=redirect_uri, expires_at=now + timedelta(minutes=10), created_at=now,
    ))
    await session.commit()
    query = urlencode({
        "response_type": "code", "client_id": config.client_id, "redirect_uri": redirect_uri,
        "scope": " ".join(config.scopes), "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return {"authorization_url": f"{config.authorization_endpoint}?{query}"}


def _select_jwk(jwks: dict[str, object], token: str):
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="OIDC ID token header is invalid") from exc
    algorithm = header.get("alg")
    if algorithm not in _ALLOWED_ID_TOKEN_ALGORITHMS:
        raise HTTPException(status_code=401, detail="OIDC ID token uses a disallowed signature algorithm")
    key_id = header.get("kid")
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise HTTPException(status_code=502, detail="OIDC provider returned an invalid JWKS document")
    candidate = next((item for item in keys if isinstance(item, dict) and item.get("kid") == key_id), None)
    if candidate is None:
        raise HTTPException(status_code=401, detail="OIDC signing key was not found")
    try:
        return jwt.PyJWK.from_dict(candidate, algorithm=algorithm).key, algorithm
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="OIDC provider returned an invalid signing key") from exc


@router.get("/callback", name="oidc_callback")
async def oidc_callback(
    request: Request,
    state: str,
    code: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    state_hash = stable_secret_hash(state)
    auth_state = await session.get(OIDCAuthenticationStateModel, state_hash, with_for_update=True)
    now = datetime.now(timezone.utc)
    if auth_state is None or auth_state.expires_at <= now:
        raise HTTPException(status_code=401, detail="OIDC state is invalid or expired")
    config = await session.get(OIDCConfigurationVersionModel, auth_state.configuration_version_id)
    if config is None or not config.enabled:
        raise HTTPException(status_code=401, detail="OIDC configuration is unavailable")
    verifier = decrypt_identity_secret(auth_state.code_verifier_encrypted)
    redirect_uri = auth_state.redirect_uri
    nonce_hash = auth_state.nonce_hash
    await session.delete(auth_state)
    await session.commit()  # consume state before the external exchange so a retry cannot replay it
    _require_transport(config.token_endpoint)
    _require_transport(config.jwks_uri)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False) as client:
            token_response = await client.post(config.token_endpoint, data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": config.client_id, "client_secret": decrypt_identity_secret(config.client_secret_encrypted),
                "code_verifier": verifier,
            }, headers={"Accept": "application/json"})
            token_response.raise_for_status()
            if len(token_response.content) > 1_000_000:
                raise ValueError("token response exceeds limit")
            token_document = token_response.json()
            id_token = token_document.get("id_token")
            if not isinstance(id_token, str):
                raise ValueError("ID token is missing")
            jwks_response = await client.get(config.jwks_uri, headers={"Accept": "application/json"})
            jwks_response.raise_for_status()
            if len(jwks_response.content) > 2_000_000:
                raise ValueError("JWKS response exceeds limit")
            jwks = jwks_response.json()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="OIDC provider exchange failed") from exc
    key, algorithm = _select_jwk(jwks, id_token)
    try:
        claims = jwt.decode(
            id_token, key, algorithms=[algorithm], audience=config.client_id, issuer=config.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="OIDC ID token validation failed") from exc
    if not secrets.compare_digest(stable_secret_hash(str(claims.get("nonce", ""))), nonce_hash):
        raise HTTPException(status_code=401, detail="OIDC nonce validation failed")
    subject = str(claims["sub"])
    if not subject or len(subject) > 255:
        raise HTTPException(status_code=401, detail="OIDC subject is invalid")
    # Bind the provider's opaque subject to its issuer.  A plain `sub` is only
    # unique within an issuer and must never join accounts across providers.
    subject_key = stable_secret_hash(f"{config.issuer}\0{subject}")
    try:
        role = role_from_oidc_claim(claims.get(config.role_claim), config.role_mapping, config.default_role)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    raw_username = str(claims.get(config.username_claim) or "").lower()
    safe_username = re.sub(r"[^a-z0-9_.-]+", "-", raw_username).strip("-.")[:60]
    if len(safe_username) < 3:
        safe_username = f"oidc-{subject_key[:16]}"
    display_name = str(claims.get(config.display_name_claim) or safe_username)[:160]
    user = await session.scalar(select(UserModel).where(UserModel.oidc_subject == subject_key, UserModel.deleted_at.is_(None)))
    if user is None:
        collision = await session.scalar(select(UserModel.id).where(UserModel.username == safe_username))
        username = f"{safe_username[:60]}-{subject_key[:8]}" if collision else safe_username
        user = UserModel(
            id=uuid4(), username=username, display_name=display_name, password_hash=None,
            identity_provider="oidc", oidc_subject=subject_key, role=UserRole(role.value),
            totp_secret_encrypted=None, totp_enabled=False, totp_recovery_hashes=[],
            enabled=True, version=1, created_at=now, updated_at=now, deleted_at=None,
        )
        session.add(user)
        await session.flush()
        action = "auth.oidc_user_provisioned"
    else:
        if not user.enabled or user.identity_provider != "oidc":
            raise HTTPException(status_code=403, detail="OIDC account is disabled")
        user.display_name = display_name
        user.role = UserRole(role.value)
        user.version += 1
        user.updated_at = now
        action = "auth.oidc_login_succeeded"
    await append_audit(
        session, action=action, actor_id=user.id, target_type="user", target_id=str(user.id),
        correlation_id=request.state.correlation_id,
        context={"identity_provider": "oidc", "issuer": config.issuer, "configuration_version": config.version_number},
    )
    await session.commit()
    token, csrf_token = issue_session(user)
    response = RedirectResponse(url="/", status_code=303)
    _set_session_cookies(response, token, csrf_token)
    return response
