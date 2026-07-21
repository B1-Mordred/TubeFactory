from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
from fastapi import HTTPException, status
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.authorization import Role
from youtuber_api.config import get_settings
from youtuber_api.identity_crypto import decrypt_identity_secret, stable_secret_hash
from youtuber_api.models import AuthenticationRateLimitModel, UserModel


def authentication_key(username: str, client_host: str) -> str:
    return stable_secret_hash(f"{username.strip().lower()}\0{client_host}")


async def require_login_not_throttled(session: AsyncSession, key_hash: str) -> None:
    item = await session.get(AuthenticationRateLimitModel, key_hash)
    now = datetime.now(timezone.utc)
    if item and item.blocked_until and item.blocked_until > now:
        retry_after = max(1, int((item.blocked_until - now).total_seconds()))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )


async def record_login_failure(session: AsyncSession, key_hash: str) -> None:
    settings = get_settings()
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"auth-rate:{key_hash}"})
    now = datetime.now(timezone.utc)
    item = await session.get(AuthenticationRateLimitModel, key_hash, with_for_update=True)
    if item is None or now - item.window_started_at >= timedelta(seconds=settings.auth_rate_limit_window_seconds):
        if item is None:
            item = AuthenticationRateLimitModel(
                key_hash=key_hash, failure_count=0, window_started_at=now,
                blocked_until=None, updated_at=now,
            )
            session.add(item)
        else:
            item.failure_count = 0
            item.window_started_at = now
            item.blocked_until = None
    item.failure_count += 1
    item.updated_at = now
    if item.failure_count >= settings.auth_rate_limit_failures:
        item.blocked_until = now + timedelta(seconds=settings.auth_rate_limit_block_seconds)


async def clear_login_failures(session: AsyncSession, key_hash: str) -> None:
    await session.execute(delete(AuthenticationRateLimitModel).where(AuthenticationRateLimitModel.key_hash == key_hash))


def generate_recovery_codes(count: int = 10) -> list[str]:
    return [f"{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}" for _ in range(count)]


def recovery_code_hash(code: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", code).upper()
    return stable_secret_hash(normalized)


def verify_totp_code(encrypted_secret: bytes, code: str) -> bool:
    if not code.isdigit():
        return False
    return pyotp.TOTP(decrypt_identity_secret(encrypted_secret)).verify(code, valid_window=1)


def consume_second_factor(user: UserModel, code: str | None) -> tuple[bool, bool]:
    """Return (accepted, used_recovery_code), mutating only an accepted recovery list."""
    if not user.totp_enabled:
        return True, False
    if not code or user.totp_secret_encrypted is None:
        return False, False
    if verify_totp_code(user.totp_secret_encrypted, code):
        return True, False
    candidate = recovery_code_hash(code)
    hashes = list(user.totp_recovery_hashes or [])
    if candidate not in hashes:
        return False, False
    hashes.remove(candidate)
    user.totp_recovery_hashes = hashes
    return True, True


def role_from_oidc_claim(claim: object, mapping: dict[str, str], default_role: str | None) -> Role:
    values = claim if isinstance(claim, list) else [claim] if isinstance(claim, str) else []
    mapped_roles = [Role(mapping[str(value)]) for value in values if str(value) in mapping]
    if mapped_roles:
        # Group order is provider-controlled and often unstable. Use a fixed
        # application privilege order when an identity belongs to many groups.
        privilege = {
            Role.VIEWER: 0, Role.REVIEWER: 1, Role.EDITOR: 2,
            Role.OPERATOR: 3, Role.ADMIN: 4,
        }
        return max(mapped_roles, key=privilege.__getitem__)
    if default_role:
        return Role(default_role)
    raise ValueError("OIDC identity has no explicitly mapped application role")
