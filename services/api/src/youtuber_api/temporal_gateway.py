from __future__ import annotations

from typing import Any

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from youtuber_api.config import get_settings


class TemporalProbeGateway:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.settings = get_settings()

    async def start_probe(self, *, workflow_id: str, correlation_id: str) -> str:
        handle = await self.client.start_workflow(
            "durable-probe",
            {"correlation_id": correlation_id},
            id=workflow_id,
            task_queue=self.settings.temporal_task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        return handle.id

    async def describe_probe(self, workflow_id: str) -> dict[str, Any]:
        handle = self.client.get_workflow_handle(workflow_id)
        return await handle.query("status")

    async def complete_probe(self, workflow_id: str) -> None:
        handle = self.client.get_workflow_handle(workflow_id)
        await handle.signal("complete")
