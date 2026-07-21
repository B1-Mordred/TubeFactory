from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from workflow_worker.config import Settings
from workflow_worker.workflows import DurableProbeWorkflow


async def health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.read(4096)
        body = b'{"status":"ready"}\n'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def run() -> None:
    settings = Settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    health_server = await asyncio.start_server(health_handler, "0.0.0.0", settings.health_port)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[DurableProbeWorkflow],
    )
    async with health_server:
        await worker.run()


if __name__ == "__main__":
    asyncio.run(run())
