from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker
import uvicorn

from publisher_worker.activities import perform_private_upload, reconcile_publication, schedule_publication
from publisher_worker.config import Settings
from publisher_worker.gateway import app
from publisher_worker.workflows import YouTubePrivateUploadWorkflow, YouTubeReconcileWorkflow, YouTubeScheduleWorkflow


async def run() -> None:
    settings = Settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    worker = Worker(
        client, task_queue=settings.task_queue,
        workflows=[YouTubePrivateUploadWorkflow, YouTubeReconcileWorkflow, YouTubeScheduleWorkflow],
        activities=[perform_private_upload, reconcile_publication, schedule_publication],
    )
    web_server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.health_port, log_level="warning"))
    web_task = asyncio.create_task(web_server.serve())
    try:
        await worker.run()
    finally:
        web_server.should_exit = True
        await web_task


if __name__ == "__main__":
    asyncio.run(run())
