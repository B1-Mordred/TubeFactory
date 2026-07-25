from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from editorial_core.research import scholarly_work_identity


_CLAIM_TOKEN = re.compile(r"[\wÄÖÜäöüß-]{4,}")
_NAVIGATION_TITLES = {
    "skip to main content",
    "home",
    "untitled",
    "document",
}


def _claim_tokens(statement: str) -> set[str]:
    return {token.casefold() for token in _CLAIM_TOKEN.findall(statement)}


def _claims_overlap(left: str, right: str) -> bool:
    left_tokens = _claim_tokens(left)
    right_tokens = _claim_tokens(right)
    if not left_tokens or not right_tokens:
        return left.strip().casefold() == right.strip().casefold()
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= 0.84


def merge_synthesis_claims(
    current: dict[str, Any] | None,
    addition: dict[str, Any] | None,
    *,
    limit: int = 30,
) -> dict[str, Any]:
    """Build a deterministic cumulative claim bank across enrichment rounds."""

    merged: list[dict[str, Any]] = []
    for proposal in [
        *((current or {}).get("claims", [])),
        *((addition or {}).get("claims", [])),
    ]:
        statement = " ".join(str(proposal.get("statement", "")).split())
        if not statement:
            continue
        duplicate = next(
            (item for item in merged if _claims_overlap(item["statement"], statement)),
            None,
        )
        if duplicate is None:
            merged.append(
                {
                    **proposal,
                    "statement": statement,
                    "coverage_unit_ids": sorted(
                        {str(value) for value in proposal.get("coverage_unit_ids", [])}
                    ),
                    "evidence": list(proposal.get("evidence", [])),
                }
            )
            if len(merged) >= limit:
                break
            continue
        duplicate["coverage_unit_ids"] = sorted(
            {
                *duplicate.get("coverage_unit_ids", []),
                *(str(value) for value in proposal.get("coverage_unit_ids", [])),
            }
        )
        seen_evidence = {
            (str(item.get("evidence_id", "")), str(item.get("relationship", "")))
            for item in duplicate.get("evidence", [])
        }
        for evidence in proposal.get("evidence", []):
            key = (
                str(evidence.get("evidence_id", "")),
                str(evidence.get("relationship", "")),
            )
            if key not in seen_evidence:
                duplicate.setdefault("evidence", []).append(evidence)
                seen_evidence.add(key)
        duplicate["central"] = bool(duplicate.get("central") or proposal.get("central"))

    confidences = [
        float(item.get("confidence", 0))
        for item in (current, addition)
        if item and not item.get("abstained")
    ]
    return {
        "claims": merged[:limit],
        "confidence": max(confidences, default=0.0),
        "abstained": not bool(merged),
        "uncertainty": list((addition or current or {}).get("uncertainty", [])),
    }


def target_coverage_units(
    explanation_plan: list[dict[str, Any]],
    readiness: dict[str, Any],
    claim_bank: dict[str, Any],
) -> list[dict[str, Any]]:
    """Select missing or thin units without letting enrichment redefine them."""

    counts: dict[str, int] = {}
    for claim in claim_bank.get("claims", []):
        for unit_id in claim.get("coverage_unit_ids", []):
            counts[str(unit_id)] = counts.get(str(unit_id), 0) + 1
    status_by_id = {
        str(item.get("id")): str(item.get("status", ""))
        for item in readiness.get("coverage_units", [])
    }
    return [
        item
        for item in explanation_plan
        if status_by_id.get(str(item.get("id"))) != "covered"
        or counts.get(str(item.get("id")), 0) < 2
    ]


def _bounded_extracted_sources(
    sources: list[dict[str, Any]], *, limit: int = 24, include_chunks: bool = True
) -> list[dict[str, Any]]:
    """Keep a deterministic, diverse evidence set below Temporal payload limits."""

    ranked = sorted(
        sources,
        key=lambda source: (
            str(source.get("source_type", "")).casefold()
            in {"primary", "official", "dataset", "filing"},
            max(
                (
                    int(item.get("relevance_score", 0))
                    for item in source.get("evidence", [])
                ),
                default=0,
            ),
            len(source.get("evidence", [])),
            str(source.get("source_document_id", "")),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in ranked:
        title = " ".join(str(source.get("title", "")).split()).casefold()
        if title in _NAVIGATION_TITLES:
            continue
        reputation = source.get("reputation") or {}
        if not isinstance(reputation, dict):
            reputation = {}
        identity = str(
            reputation.get("work_identity")
            or scholarly_work_identity(str(source.get("canonical_url", "")))
            or source.get("content_hash")
            or source.get("source_document_id")
            or ""
        )
        if not identity or identity in seen:
            continue
        seen.add(identity)
        selected.append(source if include_chunks else {**source, "chunks": []})
        if len(selected) >= limit:
            break
    return selected


async def _execute_live_search_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Run metasearch strategies gently and preserve safe backend diagnostics."""

    result_sets: list[dict[str, Any]] = []
    strategies = plan["strategies"]
    for index, strategy in enumerate(strategies):
        try:
            result_sets.append(
                await workflow.execute_activity(
                    "search-live-strategy",
                    {
                        "strategy": strategy,
                        "domain_policy": plan["domain_policy"],
                        "freshness_policy": plan["freshness_policy"],
                    },
                    start_to_close_timeout=timedelta(seconds=75),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=5),
                        backoff_coefficient=2,
                        maximum_interval=timedelta(seconds=20),
                        maximum_attempts=3,
                    ),
                )
            )
        except ActivityError as error:
            health: dict[str, Any] = {
                "search_health": "unavailable",
                "message": "Search sources were unavailable after bounded retries.",
                "failed_strategy": {
                    "purpose": str(strategy.get("purpose", "discovery")),
                    "query": str(strategy.get("query", ""))[:500],
                },
            }
            cause = error.__cause__
            if (
                isinstance(cause, ApplicationError)
                and cause.type == "SearchBackendUnavailable"
                and cause.details
                and isinstance(cause.details[0], dict)
            ):
                health["source_health"] = cause.details[0]
            raise ApplicationError(
                "Live discovery stopped because source coverage could not be verified",
                health,
                type="LiveDiscoverySourceUnavailable",
                non_retryable=True,
            ) from error
        if index + 1 < len(strategies):
            await workflow.sleep(timedelta(seconds=2))
    return result_sets


def _completed_discovery_state(result: dict[str, Any]) -> str:
    if result.get("search_health") == "degraded":
        return "OPPORTUNITY_REVIEW_DEGRADED"
    if not result.get("opportunity_ids"):
        return "NO_NEW_OPPORTUNITIES"
    return "OPPORTUNITY_REVIEW"


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
        try:
            result_sets = await _execute_live_search_plan(plan)
        except ApplicationError as error:
            self._state = "SOURCE_UNAVAILABLE"
            self._progress = 25
            self._result = error.details[0] if error.details else None
            raise
        self._state = "CLUSTERING"
        self._progress = 80
        self._result = await workflow.execute_activity(
            "persist-live-opportunities",
            {
                **request,
                "opportunity_weights": plan["opportunity_weights"],
                "topic": plan["topic"],
                "risk": plan["risk"],
                "freshness_policy": plan["freshness_policy"],
                "format_policy": plan.get("format_policy", {}),
                "editorial_profile": plan.get("editorial_profile", {}),
                "approval_profile": plan.get("approval_profile", {}),
                "policy_snapshot": plan.get("policy_snapshot", {}),
                "discovery_disabled": plan.get("discovery_disabled", False),
                "result_sets": result_sets,
            },
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        if self._result.get("opportunity_ids"):
            self._state = "AI_TOPIC_QUALIFICATION"
            self._progress = 90
            qualification = await workflow.execute_activity(
                "qualify-opportunities-with-ai",
                {**request, "opportunity_ids": self._result["opportunity_ids"]},
                task_queue="editorial-production-v2",
                start_to_close_timeout=timedelta(minutes=35),
                heartbeat_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=45),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=15),
                    maximum_interval=timedelta(minutes=2),
                    maximum_attempts=12,
                ),
            )
            eligible_ids = qualification.get("eligible_opportunity_ids")
            self._result = {
                **self._result,
                **({"opportunity_ids": eligible_ids} if eligible_ids is not None else {}),
                "ai_qualification": qualification,
            }
        self._state = _completed_discovery_state(self._result)
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
            start_to_close_timeout=timedelta(minutes=4),
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
        self._state = "PLANNING_EVIDENCE_SEARCH"
        self._progress = 85
        evidence_search_plan = await workflow.execute_activity(
            "plan-evidence-search-with-ai",
            request,
            task_queue="editorial-production-v2",
            start_to_close_timeout=timedelta(minutes=35),
            heartbeat_timeout=timedelta(minutes=1),
            schedule_to_close_timeout=timedelta(minutes=45),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=15),
                maximum_interval=timedelta(minutes=2),
                maximum_attempts=12,
            ),
        )
        self._state = "ENRICHING_SOURCES"
        self._progress = 86
        enrichment = await workflow.execute_activity(
            "discover-linked-primary-sources",
            {
                **request,
                "snapshots": snapshots,
                "evidence_search_plan": evidence_search_plan,
            },
            start_to_close_timeout=timedelta(minutes=4),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        linked_sources = enrichment.get("sources", [])
        if linked_sources:
            self._state = "ACQUIRING_LINKED_EVIDENCE"
            linked_outcomes = await asyncio.gather(
                *[
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
                    for source in linked_sources
                ],
                return_exceptions=True,
            )
            for source, outcome in zip(linked_sources, linked_outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    failures.append(
                        {
                            "source_document_id": source["source_document_id"],
                            "error": str(outcome)[:500],
                        }
                    )
                else:
                    snapshots.append(outcome)
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
            "linked_primary_source_count": enrichment.get("linked_source_count", 0),
            "evidence_search_health": enrichment.get("search_health", []),
            "evidence_search_plan_id": evidence_search_plan.get("plan_id"),
            "bootstrap_sources": bool(plan.get("bootstrap_sources")),
            "bootstrap_linked_source_count": plan.get("bootstrap_linked_source_count", 0),
            "bootstrap_queries": plan.get("bootstrap_queries", []),
            "bootstrap_search_health": plan.get("bootstrap_search_health", []),
        }
        if request.get("auto_continue"):
            self._state = "STARTING_DOSSIER"
            self._progress = 95
            child_key = f"{request['idempotency_key']}.dossier"
            dossier_result = await workflow.execute_child_workflow(
                "live-research-dossier",
                {
                    "workflow_id": f"live-research-dossier-{child_key}",
                    "idempotency_key": child_key,
                    "opportunity_id": request["opportunity_id"],
                    "actor_id": request["actor_id"],
                    "correlation_id": request["correlation_id"],
                },
                id=f"live-research-dossier-{child_key}",
                task_queue=workflow.info().task_queue,
            )
            self._result = {
                **self._result,
                "automatic_continuation": {
                    "workflow_id": f"live-research-dossier-{child_key}",
                    "state": "completed",
                },
                "dossier_result": dossier_result,
            }
        self._state = (
            "DOSSIER_REVIEW"
            if request.get("auto_continue") and self._result.get("dossier_result", {}).get("completion_met")
            else "INSUFFICIENT_EXPLANATION_EVIDENCE"
            if request.get("auto_continue")
            else "RESEARCHING"
        )
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
        self._state = "PLANNING_EXPLANATION"
        initial_plan = await workflow.execute_activity(
            "plan-evidence-search-with-ai",
            {**request, "enrichment_round": 0},
            task_queue="editorial-production-v2",
            start_to_close_timeout=timedelta(minutes=35),
            heartbeat_timeout=timedelta(minutes=1),
            schedule_to_close_timeout=timedelta(minutes=45),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if initial_plan.get("coverage_units"):
            plan["explanation_plan"] = initial_plan.get("coverage_units", [])
        stable_explanation_plan = list(plan.get("explanation_plan", []))
        maximum_rounds = int(plan.get("format_policy", {}).get("max_enrichment_rounds", 3))
        synthesis: dict[str, Any] = {}
        review_feedback: dict[str, Any] = {}
        readiness: dict[str, Any] = {"ready": False, "gaps": ["not_evaluated"]}
        extracted_sources: list[dict[str, Any]] = []
        counterevidence_search_completed = any(
            item.get("role") == "counterevidence"
            for item in plan.get("evidence_search_queries", [])
        )
        enrichment_round = 0
        while True:
            self._state = "EXTRACTING_EVIDENCE"
            self._progress = min(70, 20 + enrichment_round * 12)
            extracted_sources = []
            sources = plan["sources"]
            for offset in range(0, len(sources), 4):
                batch = sources[offset : offset + 4]
                outcomes = await asyncio.gather(
                    *[
                        workflow.execute_activity(
                            "extract-snapshot-evidence",
                            {
                                "source": source,
                                "topic": f"{plan['opportunity_title']} {plan['opportunity_summary']}",
                                "research_goal": f"{plan['research_goal']} {plan['research_plan']['proposed_angle']}",
                            },
                            start_to_close_timeout=timedelta(minutes=1),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        )
                        for source in batch
                    ]
                )
                extracted_sources.extend(outcomes)
            compact_sources = _bounded_extracted_sources(
                extracted_sources, include_chunks=False
            )
            self._state = "AI_EVIDENCE_SYNTHESIS"
            round_synthesis = await workflow.execute_activity(
                "synthesize-research-evidence-with-ai",
                {
                    **request,
                    "plan": plan,
                    "extracted_sources": compact_sources,
                    "enrichment_round": enrichment_round,
                    "current_claim_bank": synthesis.get("claims", []),
                    "review_feedback": review_feedback,
                    "target_coverage_units": target_coverage_units(
                        stable_explanation_plan, readiness, synthesis
                    ),
                },
                task_queue="editorial-production-v2",
                start_to_close_timeout=timedelta(minutes=35),
                heartbeat_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=45),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=15),
                    maximum_interval=timedelta(minutes=2),
                    maximum_attempts=12,
                ),
            )
            synthesis = merge_synthesis_claims(synthesis, round_synthesis)
            self._state = "EVALUATING_READINESS"
            readiness = await workflow.execute_activity(
                "evaluate-explanation-readiness",
                {
                    "plan": plan,
                    "extracted_sources": compact_sources,
                    "ai_synthesis": synthesis,
                    "enrichment_round": enrichment_round,
                    "counterevidence_search_completed": counterevidence_search_completed,
                },
                start_to_close_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if readiness.get("ready") or enrichment_round >= maximum_rounds:
                break
            enrichment_round += 1
            self._state = "ENRICHING_GAPS"
            try:
                review_feedback = await workflow.execute_activity(
                    "review-research-synthesis-with-ai",
                    {
                        **request,
                        "plan": plan,
                        "extracted_sources": compact_sources,
                        "ai_synthesis": synthesis,
                        "readiness": readiness,
                        "enrichment_round": enrichment_round,
                    },
                    task_queue="editorial-production-v2",
                    start_to_close_timeout=timedelta(minutes=35),
                    heartbeat_timeout=timedelta(minutes=1),
                    schedule_to_close_timeout=timedelta(minutes=45),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError:
                review_feedback = {
                    "abstained": True,
                    "counterevidence_gaps": readiness.get("gaps", []),
                    "uncertainty": ["advisory evidence review unavailable"],
                }
            gap_plan = await workflow.execute_activity(
                "plan-evidence-search-with-ai",
                {
                    **request,
                    "enrichment_round": enrichment_round,
                    "coverage_gaps": readiness.get("gaps", []),
                    "coverage_units": readiness.get("coverage_units", []),
                    "review_feedback": review_feedback,
                },
                task_queue="editorial-production-v2",
                start_to_close_timeout=timedelta(minutes=35),
                heartbeat_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=45),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            counterevidence_search_completed = counterevidence_search_completed or any(
                item.get("role") == "counterevidence" for item in gap_plan.get("queries", [])
            )
            enrichment = await workflow.execute_activity(
                "discover-linked-primary-sources",
                {**request, "snapshots": [], "evidence_search_plan": gap_plan},
                start_to_close_timeout=timedelta(minutes=4),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            linked_sources = enrichment.get("sources", [])
            if linked_sources:
                await asyncio.gather(
                    *[
                        workflow.execute_activity(
                            "acquire-public-source",
                            {
                                **source,
                                "workflow_id": request["workflow_id"],
                                "domain_policy": plan.get("domain_policy", {"allow": [], "block": []}),
                            },
                            start_to_close_timeout=timedelta(minutes=2),
                            retry_policy=RetryPolicy(maximum_attempts=4),
                        )
                        for source in linked_sources
                    ],
                    return_exceptions=True,
                )
            plan = await workflow.execute_activity(
                "load-live-research-plan",
                request,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            plan["explanation_plan"] = stable_explanation_plan
        self._state = "SYNTHESIZING_DOSSIER"
        self._progress = 85
        self._result = await workflow.execute_activity(
            "persist-live-research-dossier",
            {
                **request,
                "plan": plan,
                # Source acquisition already persists semantic chunks.  Keeping the
                # embeddings in this cross-activity command can exceed Temporal's
                # payload limit once enrichment has collected many sources.
                "extracted_sources": compact_sources,
                "ai_synthesis": synthesis,
                "enrichment_round": enrichment_round,
                "counterevidence_search_completed": counterevidence_search_completed,
            },
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        self._state = "AI_EVIDENCE_REVIEW"
        self._progress = 92
        try:
            assessment = await workflow.execute_activity(
                "assess-dossier-evidence-with-ai",
                {**request, "research_dossier_id": self._result["dossier_id"]},
                task_queue="editorial-production-v2",
                start_to_close_timeout=timedelta(minutes=35),
                heartbeat_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=45),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=15),
                    maximum_interval=timedelta(minutes=2),
                    maximum_attempts=2,
                ),
            )
        except ActivityError as exc:
            assessment = {
                "abstained": True,
                "confidence": 0.0,
                "claim_assessments": [],
                "source_assessments": [],
                "methodological_limits": [],
                "counterevidence_gaps": [],
                "uncertainty": [
                    "advisory AI evidence review unavailable; deterministic readiness result preserved"
                ],
                "error": str(exc)[:500],
            }
        self._result = {**self._result, "ai_evidence_assessment": assessment}
        if self._result.get("completion_met") and self._result.get("automatic_script_request"):
            self._state = "HANDOFF_SCRIPT_GENERATION"
            self._progress = 96
            script_request = dict(self._result["automatic_script_request"])
            script_result = await workflow.execute_child_workflow(
                "script-generation",
                script_request,
                id=script_request["workflow_id"],
                task_queue="editorial-production-v2",
            )
            self._result = {**self._result, "script_result": script_result}
            self._state = (
                "SCRIPT_REVIEW"
                if script_result.get("status") == "verified"
                else "SCRIPT_BLOCKED"
            )
            self._progress = 100
            return self._result
        switch_count = int(request.get("candidate_switch_count", 0))
        switch_limit = min(3, max(0, int(request.get("candidate_switch_limit", 2))))
        excluded_opportunity_ids = [
            str(value) for value in request.get("excluded_opportunity_ids", [])
        ]
        if str(plan["opportunity_id"]) not in excluded_opportunity_ids:
            excluded_opportunity_ids.append(str(plan["opportunity_id"]))
        if not self._result.get("completion_met") and switch_count < switch_limit:
            self._state = "SELECTING_NEXT_APPROVED_CANDIDATE"
            self._progress = 95
            candidate = await workflow.execute_activity(
                "find-next-approved-research-candidate",
                {
                    "opportunity_id": plan["opportunity_id"],
                    "excluded_opportunity_ids": excluded_opportunity_ids,
                },
                start_to_close_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if candidate is not None:
                next_number = switch_count + 1
                child_key = f"{request['idempotency_key']}.candidate-{next_number}"
                child_common = {
                    "idempotency_key": child_key,
                    "opportunity_id": candidate["opportunity_id"],
                    "actor_id": request["actor_id"],
                    "correlation_id": request["correlation_id"],
                }
                # A child dossier run needs a RESEARCHING row whose workflow lineage
                # belongs to this autonomous series. Existing snapshots alone do not
                # provide that invariant (for example after a previously blocked run),
                # so acquisition also creates a fresh revision boundary in that case.
                if (
                    not candidate.get("snapshot_count")
                    or candidate.get("research_state") != "RESEARCHING"
                ):
                    self._state = "HANDOFF_SOURCE_ACQUISITION"
                    await workflow.execute_child_workflow(
                        "source-acquisition",
                        {**child_common, "workflow_id": f"source-acquisition-{child_key}"},
                        id=f"source-acquisition-{child_key}",
                        task_queue=workflow.info().task_queue,
                    )
                self._state = "HANDOFF_RESEARCH_DOSSIER"
                child_result = await workflow.execute_child_workflow(
                    "live-research-dossier",
                    {
                        **child_common,
                        "workflow_id": f"live-research-dossier-{child_key}",
                        "candidate_switch_count": next_number,
                        "candidate_switch_limit": switch_limit,
                        "excluded_opportunity_ids": excluded_opportunity_ids,
                    },
                    id=f"live-research-dossier-{child_key}",
                    task_queue=workflow.info().task_queue,
                )
                self._result = {
                    **self._result,
                    "candidate_handoff": candidate,
                    "series_result": child_result,
                    "completion_met": bool(child_result.get("completion_met")),
                }
        self._state = (
            "DOSSIER_REVIEW"
            if self._result.get("completion_met")
            else "INSUFFICIENT_EXPLANATION_EVIDENCE"
        )
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
        try:
            result_sets = await _execute_live_search_plan(plan)
        except ApplicationError as error:
            self._state = "SOURCE_UNAVAILABLE"
            self._progress = 25
            self._result = error.details[0] if error.details else None
            raise
        self._state = "CLUSTERING"
        self._progress = 80
        self._result = await workflow.execute_activity(
            "persist-live-opportunities",
            {
                **execution_request,
                "opportunity_weights": plan["opportunity_weights"],
                "topic": plan["topic"],
                "risk": plan["risk"],
                "freshness_policy": plan["freshness_policy"],
                "format_policy": plan.get("format_policy", {}),
                "editorial_profile": plan.get("editorial_profile", {}),
                "approval_profile": plan.get("approval_profile", {}),
                "policy_snapshot": plan.get("policy_snapshot", {}),
                "discovery_disabled": plan.get("discovery_disabled", False),
                "result_sets": result_sets,
            },
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        if self._result.get("opportunity_ids"):
            self._state = "AI_TOPIC_QUALIFICATION"
            self._progress = 90
            qualification = await workflow.execute_activity(
                "qualify-opportunities-with-ai",
                {**execution_request, "opportunity_ids": self._result["opportunity_ids"]},
                task_queue="editorial-production-v2",
                start_to_close_timeout=timedelta(minutes=35),
                heartbeat_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=45),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=15),
                    maximum_interval=timedelta(minutes=2),
                    maximum_attempts=12,
                ),
            )
            self._result = {**self._result, "ai_qualification": qualification}
        self._state = _completed_discovery_state(self._result)
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
