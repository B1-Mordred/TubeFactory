from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


@workflow.defn(name="fixture-research")
class FixtureResearchWorkflow:
    def __init__(self) -> None:
        self._state = "CREATED"
        self._progress = 0
        self._result: dict[str, Any] | None = None

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._state = "ACQUIRING"
        self._progress = 10
        self._result = await workflow.execute_activity(
            "run-fixture-pipeline",
            request,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        self._progress = 100
        self._state = "DOSSIER_REVIEW"
        return self._result

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "workflow_id": workflow.info().workflow_id,
            "state": self._state,
            "progress": self._progress,
            "result": self._result,
        }


@workflow.defn(name="live-discovery")
class LiveDiscoveryWorkflow:
    def __init__(self) -> None:
        self._state = "CREATED"
        self._progress = 0
        self._result: dict[str, Any] | None = None

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._state = "PLANNING"
        self._progress = 10
        plan = await workflow.execute_activity(
            "load-live-search-plan",
            request,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self._state = "SEARCHING"
        self._progress = 25
        searches = [
            workflow.execute_activity(
                "search-live-strategy",
                {
                    "strategy": strategy,
                    "domain_policy": plan["domain_policy"],
                },
                start_to_close_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            for strategy in plan["strategies"]
        ]
        result_sets = await asyncio.gather(*searches)
        self._state = "CLUSTERING"
        self._progress = 80
        self._result = await workflow.execute_activity(
            "persist-live-opportunities",
            {
                **request,
                "opportunity_weights": plan["opportunity_weights"],
                "result_sets": result_sets,
            },
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        self._state = "OPPORTUNITY_REVIEW"
        self._progress = 100
        return self._result

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "workflow_id": workflow.info().workflow_id,
            "state": self._state,
            "progress": self._progress,
            "result": self._result,
        }


@workflow.defn(name="source-acquisition")
class SourceAcquisitionWorkflow:
    def __init__(self) -> None:
        self._state = "CREATED"
        self._progress = 0
        self._result: dict[str, Any] | None = None

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._state = "VALIDATING_APPROVAL"
        self._progress = 5
        plan = await workflow.execute_activity(
            "load-approved-opportunity-sources",
            request,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self._state = "ACQUIRING"
        snapshots: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        sources = plan["sources"]
        for offset in range(0, len(sources), 4):
            batch = sources[offset : offset + 4]
            tasks = [
                workflow.execute_activity(
                    "acquire-public-source",
                    {
                        **source,
                        "workflow_id": request["workflow_id"],
                        "domain_policy": plan["domain_policy"],
                    },
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=4),
                )
                for source in batch
            ]
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for source, outcome in zip(batch, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    failures.append(
                        {
                            "source_document_id": source["source_document_id"],
                            "error": str(outcome)[:500],
                        }
                    )
                else:
                    snapshots.append(outcome)
            self._progress = min(85, 10 + round((offset + len(batch)) / len(sources) * 75))
        if not snapshots:
            raise ApplicationError("no approved source could be acquired", non_retryable=True)
        self._state = "RECONCILING"
        self._progress = 90
        completion = await workflow.execute_activity(
            "complete-source-acquisition",
            {**request, "snapshots": snapshots, "failures": failures},
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        self._result = {
            **completion,
            "snapshot_ids": [item["source_snapshot_id"] for item in snapshots],
            "failure_count": len(failures),
            "failures": failures,
        }
        self._state = "RESEARCHING"
        self._progress = 100
        return self._result

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "workflow_id": workflow.info().workflow_id,
            "state": self._state,
            "progress": self._progress,
            "result": self._result,
        }


@workflow.defn(name="live-research-dossier")
class LiveResearchDossierWorkflow:
    def __init__(self) -> None:
        self._state = "CREATED"
        self._progress = 0
        self._result: dict[str, Any] | None = None

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._state = "PLANNING"
        self._progress = 10
        plan = await workflow.execute_activity(
            "load-live-research-plan",
            request,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self._state = "EXTRACTING_EVIDENCE"
        extracted_sources: list[dict[str, Any]] = []
        sources = plan["sources"]
        for offset in range(0, len(sources), 4):
            batch = sources[offset : offset + 4]
            outcomes = await asyncio.gather(
                *[
                    workflow.execute_activity(
                        "extract-snapshot-evidence",
                        {
                            "source": source,
                            "topic": plan["topic"],
                            "research_goal": plan["research_goal"],
                        },
                        start_to_close_timeout=timedelta(minutes=1),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                    for source in batch
                ]
            )
            extracted_sources.extend(outcomes)
            self._progress = min(75, 15 + round((offset + len(batch)) / len(sources) * 60))
        self._state = "SYNTHESIZING_DOSSIER"
        self._progress = 85
        self._result = await workflow.execute_activity(
            "persist-live-research-dossier",
            {**request, "plan": plan, "extracted_sources": extracted_sources},
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        self._state = "DOSSIER_REVIEW"
        self._progress = 100
        return self._result

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "workflow_id": workflow.info().workflow_id,
            "state": self._state,
            "progress": self._progress,
            "result": self._result,
        }


@workflow.defn(name="source-semantic-index")
class SourceSemanticIndexWorkflow:
    def __init__(self) -> None:
        self._state = "CREATED"
        self._progress = 0
        self._result: dict[str, Any] | None = None

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._state = "INDEXING"
        self._progress = 20
        self._result = await workflow.execute_activity(
            "index-source-snapshot",
            request,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        self._state = "INDEXED"
        self._progress = 100
        return self._result

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "workflow_id": workflow.info().workflow_id,
            "state": self._state,
            "progress": self._progress,
            "result": self._result,
        }


@workflow.defn(name="scheduled-subject-discovery")
class ScheduledSubjectDiscoveryWorkflow:
    def __init__(self) -> None:
        self._state = "CREATED"
        self._progress = 0
        self._result: dict[str, Any] | None = None

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        info = workflow.info()
        execution_request = {
            "workflow_id": info.workflow_id,
            "idempotency_key": f"{request['schedule_id']}:{info.run_id}",
            "subject_profile_id": request["subject_profile_id"],
            "correlation_id": f"schedule:{request['schedule_id']}:{info.run_id}",
        }
        self._state = "PLANNING"
        self._progress = 10
        plan = await workflow.execute_activity(
            "load-live-search-plan",
            execution_request,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self._state = "SEARCHING"
        self._progress = 25
        result_sets = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "search-live-strategy",
                    {"strategy": strategy, "domain_policy": plan["domain_policy"]},
                    start_to_close_timeout=timedelta(seconds=45),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                for strategy in plan["strategies"]
            ]
        )
        self._state = "CLUSTERING"
        self._progress = 80
        self._result = await workflow.execute_activity(
            "persist-live-opportunities",
            {
                **execution_request,
                "opportunity_weights": plan["opportunity_weights"],
                "result_sets": result_sets,
            },
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        self._state = "OPPORTUNITY_REVIEW"
        self._progress = 100
        return self._result

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "workflow_id": workflow.info().workflow_id,
            "state": self._state,
            "progress": self._progress,
            "result": self._result,
        }
