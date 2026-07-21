from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


_EXTERNAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5), backoff_coefficient=2,
    maximum_interval=timedelta(minutes=2), maximum_attempts=8,
)


@workflow.defn(name="youtube-private-upload")
class YouTubePrivateUploadWorkflow:
    @workflow.run
    async def run(self, request: dict) -> dict:
        return await workflow.execute_activity(
            "perform-youtube-private-upload", request,
            start_to_close_timeout=timedelta(hours=6),
            retry_policy=_EXTERNAL_RETRY,
        )


@workflow.defn(name="youtube-reconcile")
class YouTubeReconcileWorkflow:
    @workflow.run
    async def run(self, request: dict) -> dict:
        return await workflow.execute_activity(
            "reconcile-youtube-publication", request,
            start_to_close_timeout=timedelta(minutes=3), retry_policy=_EXTERNAL_RETRY,
        )


@workflow.defn(name="youtube-schedule")
class YouTubeScheduleWorkflow:
    @workflow.run
    async def run(self, request: dict) -> dict:
        return await workflow.execute_activity(
            "schedule-youtube-publication", request,
            start_to_close_timeout=timedelta(minutes=5), retry_policy=_EXTERNAL_RETRY,
        )
