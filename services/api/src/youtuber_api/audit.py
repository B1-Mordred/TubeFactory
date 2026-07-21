from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from editorial_core.audit import AuditRecord
from youtuber_api.models import AuditEventModel

_SENSITIVE_PARTS = ("secret", "password", "token", "authorization", "cookie", "credential")


def redact(value: Any, key: str = "") -> Any:
    if any(part in key.lower() for part in _SENSITIVE_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


async def append_audit(
    session: AsyncSession,
    *,
    action: str,
    correlation_id: str,
    actor_id: UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> AuditEventModel:
    # A transaction-scoped advisory lock serializes the hash chain.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('audit_event_chain'))"))
    previous = await session.scalar(
        select(AuditEventModel.event_hash)
        .order_by(AuditEventModel.occurred_at.desc(), AuditEventModel.id.desc())
        .limit(1)
    )
    event_id = uuid4()
    occurred_at = datetime.now(timezone.utc)
    safe_context = redact(context or {})
    record = AuditRecord(
        event_id=event_id,
        occurred_at=occurred_at,
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        correlation_id=correlation_id,
        context=safe_context,
        previous_hash=previous,
    )
    event = AuditEventModel(
        id=event_id,
        occurred_at=occurred_at,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        correlation_id=correlation_id,
        context=safe_context,
        previous_hash=previous,
        event_hash=record.digest(),
    )
    session.add(event)
    await session.flush()
    return event
