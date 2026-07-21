from __future__ import annotations

from typing import Any

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError


class TemporalPublishingGateway:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings

    async def start(self, workflow_type: str, request: dict[str, Any]) -> str:
        if workflow_type not in {"youtube-private-upload", "youtube-reconcile", "youtube-schedule"}:
            raise ValueError("Unsupported publishing workflow type")
        workflow_id = str(request["workflow_id"])
        try:
            handle = await self.client.start_workflow(
                workflow_type,
                request,
                id=workflow_id,
                task_queue=self.settings.temporal_publishing_task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            return handle.id
        except WorkflowAlreadyStartedError:
            return workflow_id
