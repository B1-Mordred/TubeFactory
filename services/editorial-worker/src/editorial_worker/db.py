from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from editorial_core.audit import AuditRecord


async def append_audit(
    connection: asyncpg.Connection,
    *,
    action: str,
    correlation_id: str,
    actor_id: UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    await connection.execute("SELECT pg_advisory_xact_lock(hashtext('audit_event_chain'))")
    previous = await connection.fetchval(
        "SELECT event_hash FROM audit_events ORDER BY occurred_at DESC,id DESC LIMIT 1"
    )
    event_id = uuid4()
    occurred_at = datetime.now(timezone.utc)
    safe_context = context or {}
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
    await connection.execute(
        """INSERT INTO audit_events
           (id,occurred_at,actor_id,action,target_type,target_id,correlation_id,context,
            previous_hash,event_hash)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)""",
        event_id,
        occurred_at,
        actor_id,
        action,
        target_type,
        target_id,
        correlation_id,
        json.dumps(safe_context),
        previous,
        record.digest(),
    )
