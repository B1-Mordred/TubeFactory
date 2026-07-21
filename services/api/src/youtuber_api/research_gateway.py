from __future__ import annotations

import asyncio
from typing import Any

from temporalio.client import Client
from temporalio.client import WorkflowExecutionStatus
from temporalio.api.enums.v1 import EventType
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from youtuber_api.config import get_settings


class TemporalResearchGateway:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.settings = get_settings()

    async def start_fixture(self, request: dict[str, Any]) -> str:
        return await self._start("fixture-research", request)

    async def start_live_discovery(self, request: dict[str, Any]) -> str:
        return await self._start("live-discovery", request)

    async def start_source_acquisition(self, request: dict[str, Any]) -> str:
        return await self._start("source-acquisition", request)

    async def start_live_research(self, request: dict[str, Any]) -> str:
        return await self._start("live-research-dossier", request)

    async def start_source_index(self, request: dict[str, Any]) -> str:
        return await self._start("source-semantic-index", request)

    async def start(self, workflow_type: str, request: dict[str, Any]) -> str:
        if workflow_type not in {
            "fixture-research",
            "live-discovery",
            "source-acquisition",
            "live-research-dossier",
            "source-semantic-index",
        }:
            raise ValueError("Unsupported research workflow type")
        return await self._start(workflow_type, request)

    async def _start(self, workflow_type: str, request: dict[str, Any]) -> str:
        workflow_id = str(request["workflow_id"])
        try:
            handle = await self.client.start_workflow(
                workflow_type,
                request,
                id=workflow_id,
                task_queue=self.settings.temporal_research_task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            return handle.id
        except WorkflowAlreadyStartedError:
            return workflow_id

    async def describe(self, workflow_id: str) -> dict[str, Any]:
        handle = self.client.get_workflow_handle(workflow_id)
        description = await handle.describe()
        execution_status = _execution_status_name(description.status)
        last_error: RPCError | None = None
        query_attempts = 30 if execution_status == "RUNNING" else 1
        for _ in range(query_attempts):
            try:
                status = await handle.query("status")
                if execution_status in {"FAILED", "CANCELLED", "TERMINATED", "TIMED_OUT"}:
                    status["state"] = execution_status
                return {
                    **status,
                    "execution_status": execution_status,
                    "retryable": execution_status
                    in {"FAILED", "CANCELLED", "TERMINATED", "TIMED_OUT"},
                }
            except RPCError as exc:
                last_error = exc
                await asyncio.sleep(0.2)
        if execution_status != "RUNNING":
            return {
                "workflow_id": workflow_id,
                "state": execution_status,
                "progress": 100 if execution_status == "COMPLETED" else 0,
                "result": None,
                "execution_status": execution_status,
                "retryable": execution_status
                in {"FAILED", "CANCELLED", "TERMINATED", "TIMED_OUT"},
            }
        assert last_error is not None
        raise last_error

    async def cancel(self, workflow_id: str) -> None:
        await self.client.get_workflow_handle(workflow_id).cancel()

    async def safe_logs(self, workflow_id: str, maximum_entries: int = 50) -> list[dict[str, Any]]:
        handle = self.client.get_workflow_handle(workflow_id)
        scheduled: dict[int, str] = {}
        entries: list[dict[str, Any]] = []
        event_count = 0
        async for event in handle.fetch_history_events():
            event_count += 1
            if event_count > 10_000:
                entries.append(
                    {
                        "event_id": event.event_id,
                        "occurred_at": event.event_time.ToDatetime(),
                        "level": "warning",
                        "message": "Workflow log view truncated at the safe event limit.",
                    }
                )
                break
            level = "info"
            message: str | None = None
            event_type = event.event_type
            if event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
                message = "Workflow accepted by the durable orchestrator."
            elif event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                activity_type = event.activity_task_scheduled_event_attributes.activity_type.name
                label = _safe_activity_label(activity_type)
                scheduled[event.event_id] = label
                message = f"{label} scheduled."
            elif event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
                scheduled_id = event.activity_task_completed_event_attributes.scheduled_event_id
                message = f"{scheduled.get(scheduled_id, 'Workflow activity')} completed."
            elif event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED:
                scheduled_id = event.activity_task_failed_event_attributes.scheduled_event_id
                level = "warning"
                message = f"{scheduled.get(scheduled_id, 'Workflow activity')} failed; retry policy applied."
            elif event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT:
                scheduled_id = event.activity_task_timed_out_event_attributes.scheduled_event_id
                level = "warning"
                message = f"{scheduled.get(scheduled_id, 'Workflow activity')} timed out; retry policy applied."
            elif event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED:
                level = "warning"
                message = "Workflow cancellation requested."
            elif event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED:
                level = "warning"
                message = "Workflow cancelled."
            elif event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
                message = "Workflow completed."
            elif event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED:
                level = "error"
                message = "Workflow failed after its bounded retry policy."
            elif event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT:
                level = "error"
                message = "Workflow timed out."
            elif event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED:
                level = "error"
                message = "Workflow terminated."
            if message:
                entries.append(
                    {
                        "event_id": event.event_id,
                        "occurred_at": event.event_time.ToDatetime(),
                        "level": level,
                        "message": message,
                    }
                )
        return entries[-max(1, min(maximum_entries, 200)) :]


def _execution_status_name(status: WorkflowExecutionStatus | None) -> str:
    if status is None:
        return "RUNNING"
    if status is WorkflowExecutionStatus.CANCELED:
        return "CANCELLED"
    if status is WorkflowExecutionStatus.CONTINUED_AS_NEW:
        return "RUNNING"
    return status.name


def _safe_activity_label(activity_type: str) -> str:
    return {
        "run-fixture-pipeline": "Deterministic acceptance pipeline",
        "load-live-search-plan": "Validated search plan",
        "search-live-strategy": "Private metasearch query",
        "persist-live-opportunities": "Opportunity clustering and scoring",
        "load-approved-opportunity-sources": "Approved source manifest",
        "acquire-public-source": "Bounded public source retrieval",
        "complete-source-acquisition": "Immutable snapshot reconciliation",
        "load-live-research-plan": "Evidence research plan",
        "extract-snapshot-evidence": "Snapshot evidence extraction",
        "persist-live-research-dossier": "Claim ledger and dossier persistence",
        "index-source-snapshot": "Deterministic semantic source indexing",
        "load-script-generation-context": "Approved dossier and route snapshot",
        "invoke-editorial-model": "Policy-enforced structured model request",
        "verify-script-draft": "Deterministic claim coverage verification",
        "persist-script-result": "Immutable script version persistence",
        "load-script-verification-context": "Current immutable draft snapshot",
        "persist-script-verification": "Immutable verified script version persistence",
        "load-storyboard-generation-context": "Exact approved script snapshot",
        "validate-storyboard-draft": "Strict SceneSpec policy validation",
        "persist-storyboard-result": "Immutable storyboard version persistence",
    }.get(activity_type, "Workflow activity")
