from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from research_worker.activities import run_fixture_pipeline
from research_worker.acquisition_activities import (
    acquire_source_candidate,
    complete_source_acquisition,
    discover_linked_primary_sources,
    load_approved_opportunity_sources,
)
from research_worker.config import Settings
from research_worker.live_activities import (
    load_live_search_plan,
    persist_live_opportunities,
    search_live_strategy,
)
from research_worker.research_activities import (
    evaluate_explanation_readiness_activity,
    extract_snapshot_evidence,
    find_next_approved_research_candidate,
    index_source_snapshot,
    load_live_research_plan,
    persist_live_research_dossier,
)
from research_worker.workflows import (
    FixtureResearchWorkflow,
    LiveDiscoveryWorkflow,
    LiveResearchDossierWorkflow,
    SourceSemanticIndexWorkflow,
    ScheduledSubjectDiscoveryWorkflow,
    SourceAcquisitionWorkflow,
)


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
        task_queue=settings.task_queue,
        workflows=[
            FixtureResearchWorkflow,
            LiveDiscoveryWorkflow,
            SourceAcquisitionWorkflow,
            LiveResearchDossierWorkflow,
            SourceSemanticIndexWorkflow,
            ScheduledSubjectDiscoveryWorkflow,
        ],
        activities=[
            run_fixture_pipeline,
            load_live_search_plan,
            search_live_strategy,
            persist_live_opportunities,
            load_approved_opportunity_sources,
            acquire_source_candidate,
            discover_linked_primary_sources,
            complete_source_acquisition,
            load_live_research_plan,
            extract_snapshot_evidence,
            persist_live_research_dossier,
            evaluate_explanation_readiness_activity,
            find_next_approved_research_candidate,
            index_source_snapshot,
        ],
    )
    async with health_server:
        await worker.run()


if __name__ == "__main__":
    asyncio.run(run())
