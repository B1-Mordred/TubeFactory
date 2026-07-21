from __future__ import annotations

from typing import Any

from temporalio import workflow


@workflow.defn(name="durable-probe")
class DurableProbeWorkflow:
    """A deliberately small workflow used to prove durable worker recovery."""

    def __init__(self) -> None:
        self._complete = False
        self._state = "CREATED"
        self._started_at: str | None = None
        self._completed_at: str | None = None

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._state = "WAITING"
        self._started_at = workflow.now().isoformat()
        await workflow.wait_condition(lambda: self._complete)
        self._completed_at = workflow.now().isoformat()
        self._state = "COMPLETED"
        return self._status()

    @workflow.signal(name="complete")
    async def complete(self) -> None:
        self._complete = True

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return self._status()

    def _status(self) -> dict[str, Any]:
        return {
            "workflow_id": workflow.info().workflow_id,
            "state": self._state,
            "started_at": self._started_at,
            "completed_at": self._completed_at,
            "replay_count": max(workflow.info().attempt - 1, 0),
        }
