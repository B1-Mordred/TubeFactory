from __future__ import annotations

from typing import Any

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from youtuber_api.research_gateway import TemporalResearchGateway


class TemporalEditorialGateway(TemporalResearchGateway):
    async def start(self, workflow_type: str, request: dict[str, Any]) -> str:
        if workflow_type not in {
            "script-generation",
            "script-regeneration",
            "script-verification",
            "scene-alternative-generation",
            "storyboard-generation",
            "media-production",
            "scene-media-regeneration",
            "narration-segment-regeneration",
        }:
            raise ValueError("Unsupported editorial workflow type")
        workflow_id = str(request["workflow_id"])
        try:
            handle = await self.client.start_workflow(
                workflow_type,
                request,
                id=workflow_id,
                task_queue=self.settings.temporal_editorial_task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            return handle.id
        except WorkflowAlreadyStartedError:
            return workflow_id
