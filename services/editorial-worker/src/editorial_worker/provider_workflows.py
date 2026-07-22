from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name="provider-model-discovery")
class ProviderModelDiscoveryWorkflow:
    def __init__(self) -> None:
        self._state = "CREATED"
        self._result: dict[str, Any] | None = None

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._state = "DISCOVERING_MODELS"
        self._result = await workflow.execute_activity(
            "discover-provider-models",
            request,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        self._state = "MODELS_DISCOVERED"
        return self._result

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "workflow_id": workflow.info().workflow_id,
            "state": self._state,
            "progress": 100 if self._result is not None else 20,
            "result": self._result,
        }
