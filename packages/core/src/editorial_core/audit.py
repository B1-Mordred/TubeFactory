from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AuditRecord:
    event_id: UUID
    occurred_at: datetime
    action: str
    actor_id: UUID | None
    target_type: str | None
    target_id: str | None
    correlation_id: str
    context: dict[str, Any]
    previous_hash: str | None

    def canonical_payload(self) -> bytes:
        payload = {
            "action": self.action,
            "actor_id": str(self.actor_id) if self.actor_id else None,
            "context": self.context,
            "correlation_id": self.correlation_id,
            "event_id": str(self.event_id),
            "occurred_at": self.occurred_at.isoformat(),
            "previous_hash": self.previous_hash,
            "target_id": self.target_id,
            "target_type": self.target_type,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_payload()).hexdigest()
