from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol

from editorial_core.audit import AuditRecord


class Clock(Protocol):
    def now(self) -> datetime: ...


class AuditSink(Protocol):
    async def append(self, record: AuditRecord) -> str: ...


class ObjectStorage(Protocol):
    async def put_immutable(
        self, *, key: str, body: AsyncIterator[bytes], content_type: str, sha256: str
    ) -> None: ...

    async def signed_read_url(self, *, key: str, expires_seconds: int) -> str: ...


class DurableWorkflowGateway(Protocol):
    async def start_probe(self, *, workflow_id: str, correlation_id: str) -> str: ...

    async def describe_probe(self, workflow_id: str) -> dict[str, Any]: ...

    async def complete_probe(self, workflow_id: str) -> None: ...
