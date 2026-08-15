from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from editorial_worker.config import Settings
from editorial_worker.model_activities import invoke_editorial_model
from editorial_worker.provider_activities import discover_provider_models
from editorial_worker.provider_workflows import ProviderModelDiscoveryWorkflow
from editorial_worker.review_activities import (
    assess_dossier_evidence_with_ai,
    plan_evidence_search_with_ai,
    qualify_opportunities_with_ai,
    review_research_synthesis_with_ai,
    synthesize_research_evidence_with_ai,
)
from editorial_worker.media_activities import (
    assemble_and_qa_production,
    generate_production_narration,
    generate_scene_media_assets,
    load_media_production_context,
    persist_media_regeneration,
    persist_media_production,
    synchronize_media_timing,
)
from editorial_worker.script_activities import (
    assemble_script_draft,
    load_script_generation_context,
    load_script_regeneration_context,
    load_script_verification_context,
    merge_script_regeneration,
    persist_direct_scripted_video_import,
    persist_script_regeneration,
    persist_script_result,
    persist_script_verification,
    verify_script_activity,
)
from editorial_worker.storyboard_activities import (
    assemble_storyboard_draft,
    load_scene_alternative_context,
    load_storyboard_generation_context,
    persist_scene_alternative,
    persist_storyboard_result,
    validate_scene_alternative,
    validate_storyboard_activity,
)
from editorial_worker.workflows import (
    DirectScriptedVideoImportWorkflow,
    ExistingResearchScriptImportWorkflow,
    SceneAlternativeGenerationWorkflow,
    ScriptGenerationWorkflow,
    ScriptRegenerationWorkflow,
    ScriptVerificationWorkflow,
    StoryboardGenerationWorkflow,
    MediaProductionWorkflow,
    NarrationSegmentRegenerationWorkflow,
    SceneMediaRegenerationWorkflow,
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
            ProviderModelDiscoveryWorkflow,
            DirectScriptedVideoImportWorkflow,
            ExistingResearchScriptImportWorkflow,
            ScriptGenerationWorkflow,
            ScriptRegenerationWorkflow,
            ScriptVerificationWorkflow,
            StoryboardGenerationWorkflow,
            SceneAlternativeGenerationWorkflow,
            MediaProductionWorkflow,
            SceneMediaRegenerationWorkflow,
            NarrationSegmentRegenerationWorkflow,
        ],
        activities=[
            discover_provider_models,
            qualify_opportunities_with_ai,
            plan_evidence_search_with_ai,
            synthesize_research_evidence_with_ai,
            review_research_synthesis_with_ai,
            assess_dossier_evidence_with_ai,
            load_script_generation_context,
            load_script_regeneration_context,
            persist_direct_scripted_video_import,
            assemble_script_draft,
            invoke_editorial_model,
            merge_script_regeneration,
            verify_script_activity,
            persist_script_result,
            persist_script_regeneration,
            load_script_verification_context,
            persist_script_verification,
            load_scene_alternative_context,
            load_storyboard_generation_context,
            assemble_storyboard_draft,
            validate_scene_alternative,
            persist_scene_alternative,
            validate_storyboard_activity,
            persist_storyboard_result,
            load_media_production_context,
            generate_scene_media_assets,
            generate_production_narration,
            assemble_and_qa_production,
            persist_media_production,
            persist_media_regeneration,
            synchronize_media_timing,
        ],
    )
    async with health_server:
        await worker.run()


if __name__ == "__main__":
    asyncio.run(run())
