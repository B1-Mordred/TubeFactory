from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncpg
from temporalio import activity

from editorial_core.explanation_readiness import explanation_policy
from temporalio.exceptions import ApplicationError

from editorial_core.ai_review import hybrid_qualification_score
from editorial_core.research import scholarly_work_identity
from editorial_worker.config import Settings
from editorial_worker.db import append_audit
from editorial_worker.model_activities import invoke_model, load_task_routes


_MAX_SYNTHESIS_SOURCES = 16
_MAX_EXCERPTS_PER_SOURCE = 4
_MAX_EXCERPT_CHARACTERS = 2_400


def _json(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        return json.loads(value)
    return value


def _source_independence_key(source: dict[str, Any]) -> str:
    reputation = _json(source.get("reputation")) or {}
    return str(
        reputation.get("work_identity")
        or scholarly_work_identity(str(source.get("canonical_url", "")))
        or (f"content:{source['content_hash']}" if source.get("content_hash") else "")
        or f"document:{source.get('source_document_id', '')}"
    )


async def _invoke_model_with_heartbeat(request: dict[str, Any]) -> dict[str, Any]:
    """Keep long local-model activities recoverable across worker loss."""

    task = asyncio.create_task(invoke_model(request))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=20)
            if done:
                return task.result()
            activity.heartbeat({"task_type": request.get("task_type"), "state": "waiting_for_model"})
    finally:
        if not task.done():
            task.cancel()


def _normalize_evidence_plan(
    output: dict[str, Any], existing_units: list[dict[str, Any]]
) -> dict[str, Any]:
    """Keep coverage IDs stable after the first planning round."""

    if not existing_units:
        return output
    units = [
        {
            "id": str(item["id"]),
            "question": str(item["question"]),
            "role": str(item["role"]),
            "essential": bool(item.get("essential", True)),
        }
        for item in existing_units
        if item.get("id") and item.get("question") and item.get("role")
    ]
    valid_ids = {item["id"] for item in units}
    gap_ids = [
        str(item["id"])
        for item in existing_units
        if str(item.get("status", "")) != "covered" and str(item.get("id", "")) in valid_ids
    ]
    fallback_ids = gap_ids or sorted(valid_ids)
    queries: list[dict[str, Any]] = []
    for query in output.get("queries", []):
        coverage_ids = [
            str(value)
            for value in query.get("coverage_unit_ids", [])
            if str(value) in valid_ids
        ]
        if not coverage_ids:
            coverage_ids = fallback_ids[: min(3, len(fallback_ids))]
        if coverage_ids:
            queries.append({**query, "coverage_unit_ids": coverage_ids})
    return {**output, "coverage_units": units, "queries": queries}


def _structured_synthesis_sources(
    extracted_sources: list[dict[str, Any]],
    current_claim_bank: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose new exact excerpts in later rounds instead of inviting repetition."""

    used_evidence_ids = {
        str(link.get("evidence_id", ""))
        for claim in current_claim_bank
        for link in claim.get("evidence", [])
    }
    used_source_ids = {
        evidence_id.split(":", 1)[0]
        for evidence_id in used_evidence_ids
        if ":" in evidence_id
    }
    unique_sources: dict[str, dict[str, Any]] = {}
    for source in extracted_sources:
        key = _source_independence_key(source)
        current = unique_sources.get(key)
        if current is None or max(
            (int(item.get("relevance_score", 0)) for item in source.get("evidence", [])),
            default=0,
        ) > max(
            (int(item.get("relevance_score", 0)) for item in current.get("evidence", [])),
            default=0,
        ):
            unique_sources[key] = source
    used_independence_keys = {
        _source_independence_key(source)
        for source in extracted_sources
        if str(source.get("source_document_id", "")) in used_source_ids
    }
    # Stable-sort unseen works ahead of already-used works.  Used works remain a
    # fallback when enrichment did not acquire enough genuinely new material.
    ordered_sources = sorted(
        unique_sources.items(),
        key=lambda item: item[0] in used_independence_keys,
    )
    structured_sources: list[dict[str, Any]] = []
    for independence_key, source in ordered_sources[:_MAX_SYNTHESIS_SOURCES]:
        evidence = []
        for item in source.get("evidence", [])[:10]:
            evidence_id = f"{source['source_document_id']}:{item['excerpt_hash']}"
            if evidence_id in used_evidence_ids:
                continue
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "exact_excerpt": str(item["exact_text"])[
                        :_MAX_EXCERPT_CHARACTERS
                    ],
                    "relevance_score": item["relevance_score"],
                }
            )
            if len(evidence) >= _MAX_EXCERPTS_PER_SOURCE:
                break
        if evidence:
            structured_sources.append(
                {
                    "source_document_id": source["source_document_id"],
                    "title": source["title"],
                    "publisher": source["publisher"],
                    "source_type": source["source_type"],
                    "independence_key": independence_key,
                    "evidence": evidence,
                }
            )
    return structured_sources


def _actionable_review_feedback(feedback: dict[str, Any] | None) -> dict[str, Any]:
    """Do not turn a review abstention into a false assertion that sources are absent."""

    if not feedback or feedback.get("abstained"):
        return {}
    return feedback


@activity.defn(name="plan-evidence-search-with-ai")
async def plan_evidence_search_with_ai(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    workflow_id = str(request["workflow_id"])
    opportunity_id = UUID(str(request["opportunity_id"]))
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        routes = await load_task_routes(connection, "research_query_planner")
        row = await connection.fetchrow(
            """SELECT o.title,o.summary,s.topic,s.research_goal,s.languages,s.related_concepts,
                      s.negative_keywords,s.risk
               FROM opportunities o JOIN subject_profiles s ON s.id=o.subject_profile_id
               WHERE o.id=$1 AND o.deleted_at IS NULL AND s.deleted_at IS NULL""",
            opportunity_id,
        )
    finally:
        await connection.close()
    if row is None:
        raise ApplicationError("opportunity does not exist", non_retryable=True)
    model_result = await _invoke_model_with_heartbeat(
        {
            "task_type": "research_query_planner",
            "structured_inputs": {
                "candidate": {"title": row["title"], "summary": row["summary"]},
                "subject": {
                    "topic": row["topic"],
                    "research_goal": row["research_goal"],
                    "languages": _json(row["languages"]),
                    "related_concepts": _json(row["related_concepts"]),
                    "negative_keywords": _json(row["negative_keywords"]),
                    "risk": row["risk"],
                },
                "current_readiness": {
                    "enrichment_round": int(request.get("enrichment_round", 0)),
                    "coverage_gaps": request.get("coverage_gaps", []),
                    "existing_coverage_units": request.get("coverage_units", []),
                    "review_feedback": request.get("review_feedback", {}),
                },
            },
            "routes": routes,
            "sensitivity": "internal",
            "correlation_id": str(request.get("correlation_id", workflow_id)),
            "actor_id": request.get("actor_id"),
        }
    )
    output = _normalize_evidence_plan(
        model_result["output"], list(request.get("coverage_units", []))
    )
    enrichment_round = int(request.get("enrichment_round", 0))
    plan_id = uuid5(
        NAMESPACE_URL,
        f"evidence-search-plan:{workflow_id}:{opportunity_id}:{enrichment_round}",
    )
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"evidence-search-plan:{opportunity_id}"
            )
            existing = await connection.fetchval(
                "SELECT id FROM research_ai_query_plans WHERE id=$1", plan_id
            )
            if existing is None:
                version = await connection.fetchval(
                    "SELECT COALESCE(MAX(plan_version),0)+1 FROM research_ai_query_plans WHERE opportunity_id=$1",
                    opportunity_id,
                )
                await connection.execute(
                    """INSERT INTO research_ai_query_plans
                       (id,opportunity_id,plan_version,workflow_id,model_id,prompt_template_id,
                        rubric_version,queries,coverage_units,confidence,abstained,uncertainty,created_at)
                       VALUES($1,$2,$3,$4,$5,$6,'explanation-coverage-search-v2',$7::jsonb,$8::jsonb,$9,$10,$11::jsonb,$12)""",
                    plan_id,
                    opportunity_id,
                    version,
                    workflow_id,
                    UUID(model_result["model_id"]),
                    UUID(model_result["prompt_id"]),
                    json.dumps(output["queries"]),
                    json.dumps(output["coverage_units"]),
                    float(output["confidence"]),
                    bool(output["abstained"]),
                    json.dumps(output["uncertainty"]),
                    datetime.now(timezone.utc),
                )
                await append_audit(
                    connection,
                    action="research.ai_search_planned",
                    actor_id=UUID(str(request["actor_id"])) if request.get("actor_id") else None,
                    target_type="research_ai_query_plan",
                    target_id=str(plan_id),
                    correlation_id=str(request.get("correlation_id", workflow_id)),
                    context={
                        "opportunity_id": str(opportunity_id),
                        "query_count": len(output["queries"]),
                        "confidence": output["confidence"],
                        "abstained": output["abstained"],
                    },
                )
    finally:
        await connection.close()
    return {
        "plan_id": str(plan_id),
        "coverage_units": output["coverage_units"] if not output["abstained"] else [],
        "queries": output["queries"] if not output["abstained"] else [],
        "confidence": float(output["confidence"]),
        "abstained": bool(output["abstained"]),
        "uncertainty": output["uncertainty"],
    }


@activity.defn(name="synthesize-research-evidence-with-ai")
async def synthesize_research_evidence_with_ai(request: dict[str, Any]) -> dict[str, Any]:
    """Propose source-bound atomic claims without bypassing deterministic gates."""

    settings = Settings()
    workflow_id = str(request["workflow_id"])
    plan = request["plan"]
    research_run_id = UUID(str(plan["research_run_id"]))
    structured_sources = _structured_synthesis_sources(
        list(request.get("extracted_sources", [])),
        list(request.get("current_claim_bank", [])),
    )
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        routes = await load_task_routes(connection, "evidence_synthesizer")
    finally:
        await connection.close()
    model_result = await _invoke_model_with_heartbeat(
        {
            "task_type": "evidence_synthesizer",
            "structured_inputs": {
                "research_question": {
                    "topic": plan["topic"],
                    "goal": plan["research_goal"],
                    "opportunity_title": plan["opportunity_title"],
                    "opportunity_summary": plan["opportunity_summary"],
                    "risk": plan["risk"],
                },
                "explanation_plan": plan.get("explanation_plan", []),
                "readiness_policy": explanation_policy(
                    plan.get("format_policy", {}), plan.get("approval_profile", {})
                ).as_dict(),
                "current_claim_bank": request.get("current_claim_bank", []),
                "target_coverage_units": request.get("target_coverage_units", []),
                "review_feedback": _actionable_review_feedback(
                    request.get("review_feedback")
                ),
                "sources": structured_sources,
            },
            "routes": routes,
            "sensitivity": "restricted",
            "correlation_id": str(request.get("correlation_id", workflow_id)),
            "actor_id": request.get("actor_id"),
        }
    )
    output = model_result["output"]
    enrichment_round = int(request.get("enrichment_round", 0))
    synthesis_id = uuid5(
        NAMESPACE_URL,
        f"evidence-synthesis:{workflow_id}:{research_run_id}:{enrichment_round}",
    )
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"evidence-synthesis:{research_run_id}",
            )
            existing = await connection.fetchval(
                "SELECT id FROM research_ai_syntheses WHERE id=$1", synthesis_id
            )
            if existing is None:
                version = await connection.fetchval(
                    "SELECT COALESCE(MAX(synthesis_version),0)+1 FROM research_ai_syntheses WHERE research_run_id=$1",
                    research_run_id,
                )
                await connection.execute(
                    """INSERT INTO research_ai_syntheses
                       (id,research_run_id,synthesis_version,workflow_id,model_id,prompt_template_id,
                        rubric_version,proposed_claims,confidence,abstained,uncertainty,created_at)
                       VALUES($1,$2,$3,$4,$5,$6,'source-bound-synthesis-v1',$7::jsonb,$8,$9,$10::jsonb,$11)""",
                    synthesis_id,
                    research_run_id,
                    version,
                    workflow_id,
                    UUID(model_result["model_id"]),
                    UUID(model_result["prompt_id"]),
                    json.dumps(output["claims"]),
                    float(output["confidence"]),
                    bool(output["abstained"]),
                    json.dumps(output["uncertainty"]),
                    datetime.now(timezone.utc),
                )
                await append_audit(
                    connection,
                    action="research.ai_evidence_synthesized",
                    actor_id=UUID(str(request["actor_id"])) if request.get("actor_id") else None,
                    target_type="research_ai_synthesis",
                    target_id=str(synthesis_id),
                    correlation_id=str(request.get("correlation_id", workflow_id)),
                    context={
                        "research_run_id": str(research_run_id),
                        "claim_count": len(output["claims"]),
                        "confidence": output["confidence"],
                        "abstained": output["abstained"],
                    },
                )
    finally:
        await connection.close()
    return {
        "synthesis_id": str(synthesis_id),
        "claims": output["claims"],
        "confidence": float(output["confidence"]),
        "abstained": bool(output["abstained"]),
        "uncertainty": output["uncertainty"],
        "model_id": model_result["model_id"],
        "prompt_id": model_result["prompt_id"],
    }


@activity.defn(name="review-research-synthesis-with-ai")
async def review_research_synthesis_with_ai(request: dict[str, Any]) -> dict[str, Any]:
    """Review an in-progress claim bank and feed gaps into the next search round."""

    settings = Settings()
    source_by_evidence: dict[str, dict[str, Any]] = {}
    for source in request.get("extracted_sources", []):
        for evidence in source.get("evidence", []):
            source_by_evidence[
                f"{source['source_document_id']}:{evidence['excerpt_hash']}"
            ] = {"source": source, "evidence": evidence}
    claims: list[dict[str, Any]] = []
    for index, proposal in enumerate(request.get("ai_synthesis", {}).get("claims", [])):
        evidence_items: list[dict[str, Any]] = []
        for link in proposal.get("evidence", []):
            matched = source_by_evidence.get(str(link.get("evidence_id", "")))
            if matched is None:
                continue
            source = matched["source"]
            evidence = matched["evidence"]
            evidence_items.append(
                {
                    "exact_excerpt": str(evidence.get("exact_text", ""))[
                        :_MAX_EXCERPT_CHARACTERS
                    ],
                    "relationship": link.get("relationship", "context"),
                    "source_document_id": source.get("source_document_id"),
                    "source_title": source.get("title"),
                    "publisher": source.get("publisher"),
                    "source_type": source.get("source_type"),
                    "domain": source.get("domain"),
                    "independence_key": _source_independence_key(source),
                }
            )
        claims.append(
            {
                "claim_id": f"proposal-{index + 1}",
                "statement": proposal.get("statement", ""),
                "claim_type": proposal.get("claim_type", "fact"),
                "central": bool(proposal.get("central")),
                "coverage_unit_ids": proposal.get("coverage_unit_ids", []),
                "evidence": evidence_items,
            }
        )
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        routes = await load_task_routes(connection, "evidence_reviewer")
    finally:
        await connection.close()
    model_result = await _invoke_model_with_heartbeat(
        {
            "task_type": "evidence_reviewer",
            "structured_inputs": {
                "dossier": {
                    "research_question": request.get("plan", {}).get("research_goal"),
                    "explanation_plan": request.get("plan", {}).get("explanation_plan", []),
                    "deterministic_readiness": request.get("readiness", {}),
                },
                "claims": claims,
            },
            "routes": routes,
            "sensitivity": "restricted",
            "correlation_id": str(request.get("correlation_id", request["workflow_id"])),
            "actor_id": request.get("actor_id"),
        }
    )
    return {
        **model_result["output"],
        "model_id": model_result["model_id"],
        "prompt_id": model_result["prompt_id"],
    }


def _topic_review_eligible(output: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Decide whether an advisory qualification is strong enough for human review.

    The policy is opt-in per subject. Workflows without configured minimums preserve
    the existing advisory-only behaviour.
    """

    if not policy:
        return True
    if bool(output.get("abstained")):
        return False
    if float(output.get("confidence", 0.0)) < float(policy.get("minimum_confidence", 0.0)):
        return False
    dimensions = output.get("dimensions") or {}
    return all(
        float(dimensions.get(name, 0.0)) >= float(minimum)
        for name, minimum in (policy.get("dimension_minimums") or {}).items()
    )


@activity.defn(name="qualify-opportunities-with-ai")
async def qualify_opportunities_with_ai(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    workflow_id = str(request["workflow_id"])
    opportunity_ids = [UUID(str(value)) for value in request.get("opportunity_ids", [])]
    if not opportunity_ids:
        return {"qualification_ids": [], "rescored": 0, "abstained": 0}
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        routes = await load_task_routes(connection, "topic_qualifier")
        rows = await connection.fetch(
            """SELECT o.id,o.title,o.summary,o.grouping_reason,
                      s.topic,s.research_goal,s.related_concepts,s.negative_keywords,s.format_policy,
                      c.name AS channel_name,c.identity AS channel_identity,
                      c.audience AS channel_audience,c.editorial_rules AS channel_editorial_rules,
                      sc.score_version,sc.total,sc.positive_components,sc.penalties,sc.weights,sc.reasoning
               FROM opportunities o
               JOIN subject_profiles s ON s.id=o.subject_profile_id
               JOIN channel_profiles c ON c.id=s.channel_profile_id
               JOIN LATERAL (
                 SELECT * FROM opportunity_scores x WHERE x.opportunity_id=o.id
                 ORDER BY x.score_version DESC LIMIT 1
               ) sc ON true
               WHERE o.id=ANY($1::uuid[]) AND o.deleted_at IS NULL""",
            opportunity_ids,
        )
    finally:
        await connection.close()

    qualification_ids: list[str] = []
    eligible_opportunity_ids: list[str] = []
    rescored = 0
    abstained = 0
    filtered = 0
    for candidate_index, row in enumerate(rows, start=1):
        activity.heartbeat(
            {
                "task_type": "topic_qualifier",
                "state": "qualifying_candidate",
                "candidate": candidate_index,
                "candidate_count": len(rows),
            }
        )
        structured_inputs = {
            "candidate": {
                "title": row["title"],
                "summary": row["summary"],
                "grouping_reason": _json(row["grouping_reason"]),
            },
            "channel": {
                "name": row["channel_name"],
                "identity": _json(row["channel_identity"]),
                "audience": _json(row["channel_audience"]),
                "editorial_rules": _json(row["channel_editorial_rules"]),
            },
            "subject": {
                "topic": row["topic"],
                "research_goal": row["research_goal"],
                "related_concepts": _json(row["related_concepts"]),
                "negative_keywords": _json(row["negative_keywords"]),
            },
            "deterministic_trace": {
                "score": row["total"],
                "positive_components": _json(row["positive_components"]),
                "penalties": _json(row["penalties"]),
                "reasoning": _json(row["reasoning"]),
            },
        }
        model_result = await _invoke_model_with_heartbeat(
            {
                "task_type": "topic_qualifier",
                "structured_inputs": structured_inputs,
                "routes": routes,
                "sensitivity": "internal",
                "correlation_id": str(request.get("correlation_id", workflow_id)),
                "actor_id": request.get("actor_id"),
            }
        )
        activity.heartbeat(
            {
                "task_type": "topic_qualifier",
                "state": "candidate_qualified",
                "candidate": candidate_index,
                "candidate_count": len(rows),
            }
        )
        output = model_result["output"]
        review_policy = (_json(row["format_policy"]) or {}).get("topic_review_quality", {})
        review_eligible = _topic_review_eligible(output, review_policy)
        hybrid = hybrid_qualification_score(
            _json(row["positive_components"]),
            _json(row["penalties"]),
            output["dimensions"],
            confidence=float(output["confidence"]),
            abstained=bool(output["abstained"]),
            weights=_json(row["weights"]),
        )
        qualification_id = uuid5(NAMESPACE_URL, f"topic-qualification:{workflow_id}:{row['id']}")
        connection = await asyncpg.connect(settings.database_dsn)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"topic-qualification:{row['id']}",
                )
                existing = await connection.fetchval(
                    "SELECT id FROM opportunity_ai_qualifications WHERE id=$1", qualification_id
                )
                if existing:
                    qualification_ids.append(str(existing))
                    if await connection.fetchval(
                        "SELECT deleted_at IS NULL FROM opportunities WHERE id=$1", row["id"]
                    ):
                        eligible_opportunity_ids.append(str(row["id"]))
                    continue
                qualification_version = await connection.fetchval(
                    "SELECT COALESCE(MAX(qualification_version),0)+1 FROM opportunity_ai_qualifications WHERE opportunity_id=$1",
                    row["id"],
                )
                resulting_score_version = None
                if hybrid.applied and hybrid.score is not None:
                    resulting_score_version = await connection.fetchval(
                        "SELECT COALESCE(MAX(score_version),0)+1 FROM opportunity_scores WHERE opportunity_id=$1",
                        row["id"],
                    )
                    await connection.execute(
                        """INSERT INTO opportunity_scores
                           (id,opportunity_id,score_version,total,positive_components,penalties,weights,reasoning,created_at)
                           VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8::jsonb,$9)""",
                        uuid5(NAMESPACE_URL, f"hybrid-score:{workflow_id}:{row['id']}"),
                        row["id"], resulting_score_version, hybrid.score.total,
                        json.dumps(dict(hybrid.score.positive)), json.dumps(dict(hybrid.score.penalties)),
                        json.dumps(_json(row["weights"])),
                        json.dumps([
                            f"Hybrid score uses deterministic score version {row['score_version']} and advisory qualification {qualification_id}.",
                            f"AI confidence {float(output['confidence']):.2f}; model did not supply the total.",
                            *hybrid.score.reasoning,
                        ]),
                        datetime.now(timezone.utc),
                    )
                    rescored += 1
                else:
                    abstained += 1
                await connection.execute(
                    """INSERT INTO opportunity_ai_qualifications
                       (id,opportunity_id,qualification_version,workflow_id,model_id,prompt_template_id,
                        rubric_version,dimensions,confidence,abstained,rationale,uncertainty,
                        resulting_score_version,created_at)
                       VALUES($1,$2,$3,$4,$5,$6,'hybrid-topic-v1',$7::jsonb,$8,$9,$10::jsonb,$11::jsonb,$12,$13)""",
                    qualification_id, row["id"], qualification_version, workflow_id,
                    UUID(model_result["model_id"]), UUID(model_result["prompt_id"]),
                    json.dumps(output["dimensions"]), float(output["confidence"]), bool(output["abstained"]),
                    json.dumps(output["rationale"]), json.dumps(output["uncertainty"]),
                    resulting_score_version, datetime.now(timezone.utc),
                )
                if review_eligible:
                    eligible_opportunity_ids.append(str(row["id"]))
                else:
                    filtered += 1
                    archived_at = datetime.now(timezone.utc)
                    await connection.execute(
                        """UPDATE opportunities
                           SET deleted_at=$2,updated_at=$2
                           WHERE id=$1 AND deleted_at IS NULL""",
                        row["id"], archived_at,
                    )
                    await append_audit(
                        connection,
                        action="opportunity.ai_filtered_from_review",
                        actor_id=UUID(str(request["actor_id"])) if request.get("actor_id") else None,
                        target_type="opportunity",
                        target_id=str(row["id"]),
                        correlation_id=str(request.get("correlation_id", workflow_id)),
                        context={
                            "confidence": output["confidence"],
                            "abstained": output["abstained"],
                            "policy": review_policy,
                        },
                    )
                await append_audit(
                    connection,
                    action="opportunity.ai_qualified",
                    actor_id=UUID(str(request["actor_id"])) if request.get("actor_id") else None,
                    target_type="opportunity_ai_qualification",
                    target_id=str(qualification_id),
                    correlation_id=str(request.get("correlation_id", workflow_id)),
                    context={"opportunity_id": str(row["id"]), "confidence": output["confidence"], "abstained": output["abstained"], "resulting_score_version": resulting_score_version},
                )
        finally:
            await connection.close()
        qualification_ids.append(str(qualification_id))
    return {
        "qualification_ids": qualification_ids,
        "eligible_opportunity_ids": eligible_opportunity_ids,
        "rescored": rescored,
        "abstained": abstained,
        "filtered": filtered,
    }


@activity.defn(name="assess-dossier-evidence-with-ai")
async def assess_dossier_evidence_with_ai(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    dossier_id = UUID(str(request["research_dossier_id"]))
    workflow_id = str(request["workflow_id"])
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        routes = await load_task_routes(connection, "evidence_reviewer")
        dossier = await connection.fetchrow(
            """SELECT id,dossier_version,executive_summary,completion_evaluation,
                      unresolved_questions,alternative_explanations,source_quality_notes,
                      safe_conclusions,prohibited_overstatements
               FROM research_dossiers WHERE id=$1""", dossier_id
        )
        evidence = await connection.fetch(
            """SELECT c.id AS claim_id,c.normalized_statement,c.claim_type,c.central,c.risk,
                      ce.relationship,ce.source_independent AS independent,
                      ce.direct_evidence AS direct,e.exact_text,
                      sd.id AS source_document_id,sd.title AS source_title,sd.publisher,
                      sd.source_type,sd.domain,ss.content_hash
               FROM claims c
               LEFT JOIN claim_evidence ce ON ce.claim_id=c.id
               LEFT JOIN evidence_excerpts e ON e.id=ce.evidence_excerpt_id
               LEFT JOIN source_snapshots ss ON ss.id=e.source_snapshot_id
               LEFT JOIN source_documents sd ON sd.id=ss.source_document_id
               WHERE c.research_dossier_id=$1 ORDER BY c.central DESC,c.id LIMIT 200""",
            dossier_id,
        )
    finally:
        await connection.close()
    if dossier is None:
        raise ApplicationError("research dossier not found", non_retryable=True)
    claims: dict[str, dict[str, Any]] = {}
    for item in evidence:
        claim = claims.setdefault(str(item["claim_id"]), {
            "claim_id": str(item["claim_id"]), "statement": item["normalized_statement"],
            "claim_type": item["claim_type"], "central": item["central"], "risk": item["risk"], "evidence": [],
        })
        if item["exact_text"] is not None:
            claim["evidence"].append({
                "exact_excerpt": item["exact_text"], "relationship": item["relationship"],
                "independent": item["independent"], "direct": item["direct"],
                "source_document_id": str(item["source_document_id"]), "source_title": item["source_title"],
                "publisher": item["publisher"], "source_type": item["source_type"], "domain": item["domain"],
                "snapshot_hash": item["content_hash"],
            })
    structured_inputs = {
        "dossier": {key: _json(dossier[key]) for key in (
            "executive_summary", "completion_evaluation", "unresolved_questions",
            "alternative_explanations", "source_quality_notes", "safe_conclusions", "prohibited_overstatements"
        )},
        "claims": list(claims.values()),
    }
    model_result = await _invoke_model_with_heartbeat({
        "task_type": "evidence_reviewer", "structured_inputs": structured_inputs, "routes": routes,
        "sensitivity": "restricted", "correlation_id": str(request.get("correlation_id", workflow_id)),
        "actor_id": request.get("actor_id"),
    })
    output = model_result["output"]
    assessment_id = uuid5(NAMESPACE_URL, f"evidence-assessment:{workflow_id}:{dossier_id}")
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"evidence-assessment:{dossier_id}")
            existing = await connection.fetchval("SELECT id FROM dossier_ai_assessments WHERE id=$1", assessment_id)
            if existing:
                return {"assessment_id": str(existing), "abstained": bool(output["abstained"])}
            version = await connection.fetchval(
                "SELECT COALESCE(MAX(assessment_version),0)+1 FROM dossier_ai_assessments WHERE research_dossier_id=$1", dossier_id
            )
            await connection.execute(
                """INSERT INTO dossier_ai_assessments
                   (id,research_dossier_id,assessment_version,workflow_id,model_id,prompt_template_id,
                    rubric_version,claim_assessments,source_assessments,methodological_limits,
                    counterevidence_gaps,confidence,abstained,uncertainty,created_at)
                   VALUES($1,$2,$3,$4,$5,$6,'source-bound-v1',$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,$11,$12,$13::jsonb,$14)""",
                assessment_id,dossier_id,version,workflow_id,UUID(model_result["model_id"]),UUID(model_result["prompt_id"]),
                json.dumps(output["claim_assessments"]),json.dumps(output["source_assessments"]),
                json.dumps(output["methodological_limits"]),json.dumps(output["counterevidence_gaps"]),
                float(output["confidence"]),bool(output["abstained"]),json.dumps(output["uncertainty"]),datetime.now(timezone.utc),
            )
            await append_audit(
                connection, action="dossier.ai_evidence_assessed",
                actor_id=UUID(str(request["actor_id"])) if request.get("actor_id") else None,
                target_type="dossier_ai_assessment", target_id=str(assessment_id),
                correlation_id=str(request.get("correlation_id", workflow_id)),
                context={"research_dossier_id": str(dossier_id), "confidence": output["confidence"], "abstained": output["abstained"]},
            )
    finally:
        await connection.close()
    return {
        "assessment_id": str(assessment_id),
        "claim_assessments": output["claim_assessments"],
        "source_assessments": output["source_assessments"],
        "methodological_limits": output["methodological_limits"],
        "counterevidence_gaps": output["counterevidence_gaps"],
        "abstained": bool(output["abstained"]),
        "confidence": float(output["confidence"]),
        "uncertainty": output["uncertainty"],
    }
