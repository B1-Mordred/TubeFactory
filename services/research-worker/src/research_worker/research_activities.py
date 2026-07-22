from __future__ import annotations

import asyncio
import hashlib
import json
import re
import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from minio import Minio
from temporalio import activity
from temporalio.exceptions import ApplicationError

from editorial_core.discovery import (
    AtomicClaim,
    ClaimType,
    EvidenceLink,
    EvidenceRelation,
    RiskLevel,
    evaluate_research_completion,
)
from editorial_core.audit import AuditRecord
from editorial_core.explanation_readiness import (
    evaluate_explanation_readiness,
    explanation_policy,
    meta_claim_reason,
)
from editorial_core.research import (
    chunk_semantic_text,
    classify_claim,
    cluster_evidence_statements,
    evidence_relation,
    extract_evidence_sentences,
    scholarly_work_identity,
)
from research_worker.config import Settings


_PRIMARY_TYPES = {"primary", "official", "official record", "original study", "authoritative"}
_NON_PRIMARY_DOMAINS = {
    "reddit.com",
    "www.reddit.com",
    "bibsonomy.org",
    "www.bibsonomy.org",
    "facebook.com",
    "x.com",
    "twitter.com",
}
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _append_audit(
    connection: asyncpg.Connection,
    *,
    action: str,
    correlation_id: str,
    actor_id: UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Append to the shared tamper-evident audit chain inside the caller transaction."""

    await connection.execute("SELECT pg_advisory_xact_lock(hashtext('audit_event_chain'))")
    previous = await connection.fetchval(
        "SELECT event_hash FROM audit_events ORDER BY occurred_at DESC,id DESC LIMIT 1"
    )
    event_id = uuid4()
    occurred_at = datetime.now(timezone.utc)
    safe_context = context or {}
    record = AuditRecord(
        event_id=event_id,
        occurred_at=occurred_at,
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        correlation_id=correlation_id,
        context=safe_context,
        previous_hash=previous,
    )
    await connection.execute(
        """INSERT INTO audit_events
           (id,occurred_at,actor_id,action,target_type,target_id,correlation_id,context,
            previous_hash,event_hash)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)""",
        event_id,
        occurred_at,
        actor_id,
        action,
        target_type,
        target_id,
        correlation_id,
        json.dumps(safe_context),
        previous,
        record.digest(),
    )


def _cosine(left: list[float], right: list[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def detect_dependent_sources(
    extracted_sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return evidence relationships and canonical dependency-group keys.

    Dependence is an equivalence relation used when evaluating one claim. A single
    representative from a duplicate group may count; citing two aliases from the
    same group may not. Distinct scholarly work identities are never collapsed by
    the deliberately coarse local feature-hash embedding alone.
    """

    relationships: list[dict[str, Any]] = []
    parents: dict[str, str] = {}

    def find(item: str) -> str:
        parents.setdefault(item, item)
        while parents[item] != item:
            parents[item] = parents[parents[item]]
            item = parents[item]
        return item

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    for left_index, left in enumerate(extracted_sources):
        for right in extracted_sources[left_index + 1 :]:
            exact_duplicate = left["content_hash"] == right["content_hash"] or bool(
                {item["chunk_hash"] for item in left.get("chunks", [])}
                & {item["chunk_hash"] for item in right.get("chunks", [])}
            )
            similarity = max(
                (
                    _cosine(left_chunk["embedding"], right_chunk["embedding"])
                    for left_chunk in left.get("chunks", [])
                    for right_chunk in right.get("chunks", [])
                ),
                default=0.0,
            )
            left_reputation = _json(left.get("reputation")) or {}
            right_reputation = _json(right.get("reputation")) or {}
            left_work_identity = str(left_reputation.get("work_identity") or "")
            right_work_identity = str(right_reputation.get("work_identity") or "")
            same_work = bool(
                left_work_identity and left_work_identity == right_work_identity
            )
            if not exact_duplicate and not same_work:
                if left_work_identity and right_work_identity:
                    continue
                if similarity < 0.94 or _evidence_token_similarity(left, right) < 0.72:
                    continue
            first, second = sorted(
                [left["source_document_id"], right["source_document_id"]]
            )
            relationship = "exact_duplicate" if exact_duplicate else "near_duplicate"
            relationships.append(
                {
                    "source_document_id": first,
                    "related_source_document_id": second,
                    "relationship": relationship,
                    "confidence": (
                        100
                        if exact_duplicate
                        else 99
                        if same_work
                        else min(99, round(similarity * 100))
                    ),
                    "reason": (
                        "identical acquired content or semantic chunk hash"
                        if exact_duplicate
                        else "same normalized scholarly work identity"
                        if same_work
                        else f"local feature-hash chunk cosine similarity {similarity:.3f}"
                    ),
                }
            )
            union(first, second)
    dependency_groups = {
        item: min(member for member in parents if find(member) == find(item))
        for item in parents
    }
    return relationships, dependency_groups


def _evidence_token_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    def tokens(source: dict[str, Any]) -> set[str]:
        return {
            token.casefold()
            for item in source.get("evidence", [])
            for token in re.findall(r"[\wÄÖÜäöüß-]{4,}", str(item.get("exact_text", "")))
        }

    left_tokens = tokens(left)
    right_tokens = tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.10f}" for value in values) + "]"


def _read_minio_object(client: Minio, bucket: str, key: str, maximum_bytes: int) -> bytes:
    response = client.get_object(bucket, key)
    try:
        body = response.read(maximum_bytes + 1)
        if len(body) > maximum_bytes:
            raise ValueError("normalized source exceeds the evidence extraction limit")
        return body
    finally:
        response.close()
        response.release_conn()


def _source_independence_key(source: dict[str, Any]) -> str:
    reputation = _json(source.get("reputation")) or {}
    return str(
        reputation.get("work_identity")
        or scholarly_work_identity(str(source.get("canonical_url", "")))
        or (f"content:{source['content_hash']}" if source.get("content_hash") else "")
        or f"document:{source.get('source_document_id', '')}"
    )


def _claim_independence_key(
    source: dict[str, Any], dependency_groups: dict[str, str]
) -> str:
    source_id = str(source["source_document_id"])
    if source_id in dependency_groups:
        return f"dependency:{dependency_groups[source_id]}"
    return _source_independence_key(source)


def _is_primary_source(source: dict[str, Any]) -> bool:
    domain = str(source.get("domain", "")).casefold().split(":", 1)[0]
    return (
        str(source.get("source_type", "")).casefold() in _PRIMARY_TYPES
        and domain not in _NON_PRIMARY_DOMAINS
    )


def prepare_ai_synthesized_claims(
    ai_synthesis: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    dependency_groups: dict[str, str],
    minimum_independent: int = 2,
    coverage_unit_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate model proposals against exact locally extracted evidence IDs."""

    if (
        not ai_synthesis
        or ai_synthesis.get("abstained")
        or float(ai_synthesis.get("confidence", 0)) < 0.65
    ):
        return []
    evidence_by_id = {
        f"{item['source']['source_document_id']}:{item['excerpt_hash']}": item
        for item in candidates
    }
    prepared: list[dict[str, Any]] = []
    for proposal in ai_synthesis.get("claims", [])[:30]:
        statement = " ".join(str(proposal.get("statement", "")).split())
        claim_type = str(proposal.get("claim_type", "fact"))
        proposed_units = {
            str(value)
            for value in proposal.get("coverage_unit_ids", [])
            if coverage_unit_ids is None or str(value) in coverage_unit_ids
        }
        if (
            not 20 <= len(statement) <= 700
            or claim_type not in {"fact", "inference", "opinion"}
            or meta_claim_reason(statement)
            or (coverage_unit_ids is not None and not proposed_units)
        ):
            continue
        selected: list[tuple[dict[str, Any], str]] = []
        seen_ids: set[str] = set()
        for item in proposal.get("evidence", [])[:12]:
            evidence_id = str(item.get("evidence_id", ""))
            relationship = str(item.get("relationship", ""))
            candidate = evidence_by_id.get(evidence_id)
            if candidate is None or evidence_id in seen_ids or relationship not in {
                "supports", "contradicts", "context"
            }:
                continue
            seen_ids.add(evidence_id)
            selected.append((candidate, relationship))
        links = [
            {
                "candidate": candidate,
                "relationship": relationship,
                "independence_key": _claim_independence_key(
                    candidate["source"], dependency_groups
                ),
                "independent": True,
                "primary": _is_primary_source(candidate["source"]),
                "direct": relationship != "context",
            }
            for candidate, relationship in selected
        ]
        if not links:
            continue
        central = bool(proposal.get("central"))
        independent_supports = {
            link["independence_key"]
            for link in links
            if link["relationship"] == "supports"
        }
        if not independent_supports:
            continue
        prepared.append(
            {
                "id": uuid4(),
                "statement": statement,
                "claim_type": claim_type,
                "central": central and len(independent_supports) >= minimum_independent,
                "central_eligible": len(independent_supports) >= minimum_independent,
                "independent_support_count": len(independent_supports),
                "confidence": min(95, max(50, round(float(ai_synthesis["confidence"]) * 100))),
                "status": (
                    "disputed"
                    if any(link["relationship"] == "contradicts" for link in links)
                    else "supported"
                ),
                "coverage_unit_ids": sorted(proposed_units),
                "links": links,
            }
        )
    eligible = [claim for claim in prepared if claim["central_eligible"]]
    if not eligible:
        return []
    chosen_central_ids = {
        claim["id"]
        for claim in sorted(
            eligible,
            key=lambda claim: (
                claim["central"],
                claim["independent_support_count"],
                sum(1 for link in claim["links"] if link["primary"]),
                len(claim["coverage_unit_ids"]),
                claim["statement"],
            ),
            reverse=True,
        )[:4]
    }
    for claim in prepared:
        claim["central"] = claim["id"] in chosen_central_ids
        claim.pop("central_eligible", None)
        claim.pop("independent_support_count", None)
    return prepared


def _readiness_claims(prepared_claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(claim["id"]),
            "statement": claim["statement"],
            "status": claim["status"],
            "central": claim["central"],
            "coverage_unit_ids": claim.get("coverage_unit_ids", []),
            "evidence": [
                {
                    "relationship": link["relationship"],
                    "direct": link["direct"],
                    "independent": link["independent"],
                    "primary": link["primary"],
                    "source_id": str(link["candidate"]["source"]["source_document_id"]),
                    "independence_key": link["independence_key"],
                }
                for link in claim["links"]
            ],
        }
        for claim in prepared_claims
    ]


@activity.defn(name="evaluate-explanation-readiness")
async def evaluate_explanation_readiness_activity(request: dict[str, Any]) -> dict[str, Any]:
    plan = request["plan"]
    extracted_sources = request.get("extracted_sources", [])
    candidates = [
        {**evidence, "source": source}
        for source in extracted_sources
        for evidence in source.get("evidence", [])[:6]
    ]
    _, dependency_groups = detect_dependent_sources(extracted_sources)
    coverage_units = list(plan.get("explanation_plan", []))
    prepared_claims = prepare_ai_synthesized_claims(
        request.get("ai_synthesis"),
        candidates,
        dependency_groups,
        minimum_independent=int(plan["completion_criteria"]["minimum_independent"]),
        coverage_unit_ids={str(item.get("id")) for item in coverage_units},
    )
    policy = explanation_policy(plan.get("format_policy", {}), plan.get("approval_profile", {}))
    result = evaluate_explanation_readiness(
        policy=policy,
        claims=_readiness_claims(prepared_claims),
        coverage_units=coverage_units,
        minimum_independent_sources=int(plan["completion_criteria"]["minimum_independent"]),
        minimum_primary_sources=int(plan["completion_criteria"]["minimum_primary"]),
        counterevidence_search_completed=bool(request.get("counterevidence_search_completed")),
    )
    return {
        **result.as_dict(),
        "enrichment_round": int(request.get("enrichment_round", 0)),
    }


@activity.defn(name="find-next-approved-research-candidate")
async def find_next_approved_research_candidate(
    request: dict[str, Any],
) -> dict[str, Any] | None:
    """Select the next human-approved candidate without weakening review gates."""

    settings = Settings()
    current_id = UUID(str(request["opportunity_id"]))
    excluded = [UUID(str(value)) for value in request.get("excluded_opportunity_ids", [])]
    if current_id not in excluded:
        excluded.append(current_id)
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        row = await connection.fetchrow(
            """SELECT candidate.id,candidate.title,
                      count(DISTINCT os.id) AS source_candidate_count,
                      count(DISTINCT ss.id) AS snapshot_count,
                      (SELECT rr.state FROM research_runs rr
                       WHERE rr.opportunity_id=candidate.id AND rr.deleted_at IS NULL
                       ORDER BY rr.created_at DESC LIMIT 1) AS research_state
               FROM opportunities current
               JOIN opportunities candidate
                 ON candidate.subject_profile_id=current.subject_profile_id
               LEFT JOIN opportunity_sources os ON os.opportunity_id=candidate.id
               LEFT JOIN source_snapshots ss ON ss.source_document_id=os.source_document_id
               LEFT JOIN LATERAL (
                   SELECT total FROM opportunity_scores score
                   WHERE score.opportunity_id=candidate.id
                   ORDER BY score.score_version DESC LIMIT 1
               ) latest_score ON true
               WHERE current.id=$1
                 AND candidate.deleted_at IS NULL
                 AND candidate.decision='approved'
                 AND NOT(candidate.id=ANY($2::uuid[]))
                 AND NOT EXISTS (
                     SELECT 1 FROM research_dossiers dossier
                     WHERE dossier.opportunity_id=candidate.id
                       AND dossier.deleted_at IS NULL
                       AND coalesce((dossier.completion_evaluation->'explanation_readiness'->>'ready')::boolean,false)
                 )
               GROUP BY candidate.id,candidate.title,latest_score.total
               HAVING count(DISTINCT os.id)>0
               ORDER BY latest_score.total DESC NULLS LAST,candidate.created_at
               LIMIT 1""",
            current_id,
            excluded,
        )
        if row is None:
            return None
        return {
            "opportunity_id": str(row["id"]),
            "title": row["title"],
            "source_candidate_count": int(row["source_candidate_count"]),
            "snapshot_count": int(row["snapshot_count"]),
            "research_state": row["research_state"],
        }
    finally:
        await connection.close()


@activity.defn(name="index-source-snapshot")
async def index_source_snapshot(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    source_document_id = UUID(str(request["source_document_id"]))
    idempotency_key = f"{source_document_id}:{request['idempotency_key']}"
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        existing = await connection.fetchval(
            """SELECT result FROM idempotency_records
               WHERE scope='research.semantic_index' AND idempotency_key=$1
                 AND status='succeeded'""",
            idempotency_key,
        )
        if existing:
            return _json(existing)
        snapshot = await connection.fetchrow(
            """SELECT id,normalized_object_key,content_hash FROM source_snapshots
               WHERE source_document_id=$1 ORDER BY snapshot_number DESC LIMIT 1""",
            source_document_id,
        )
        if snapshot is None:
            raise ApplicationError("source has no immutable snapshot", non_retryable=True)
    finally:
        await connection.close()
    minio = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    body = await asyncio.to_thread(
        _read_minio_object,
        minio,
        settings.minio_bucket,
        snapshot["normalized_object_key"],
        min(settings.acquisition_max_bytes, 2_000_000),
    )
    chunks = chunk_semantic_text(body.decode("utf-8", errors="replace"))
    if not chunks:
        raise ApplicationError("source normalized text is empty", non_retryable=True)
    now = datetime.now(timezone.utc)
    request_hash = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = {
        "source_document_id": str(source_document_id),
        "source_snapshot_id": str(snapshot["id"]),
        "content_hash": snapshot["content_hash"],
        "semantic_chunk_count": len(chunks),
        "embedding_model": "feature-hash-v1",
    }
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"semantic-index:{source_document_id}"
            )
            existing = await connection.fetchval(
                """SELECT result FROM idempotency_records
                   WHERE scope='research.semantic_index' AND idempotency_key=$1
                     AND status='succeeded'""",
                idempotency_key,
            )
            if existing:
                return _json(existing)
            for chunk_number, chunk in enumerate(chunks, start=1):
                await connection.execute(
                    """INSERT INTO semantic_chunks
                    (id,source_snapshot_id,chunk_number,text,location_anchor,start_offset,
                     end_offset,chunk_hash,token_count,embedding_model,embedding,created_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'feature-hash-v1',$10::vector,$11)
                    ON CONFLICT (source_snapshot_id,chunk_number) DO NOTHING""",
                    uuid4(), snapshot["id"], chunk_number, chunk.text, chunk.location_anchor,
                    chunk.start_offset, chunk.end_offset, chunk.chunk_hash, chunk.token_count,
                    _vector_literal(list(chunk.embedding)), now,
                )
            await connection.execute(
                """INSERT INTO idempotency_records
                (id,scope,idempotency_key,request_hash,status,external_id,result,created_at,updated_at)
                VALUES($1,'research.semantic_index',$2,$3,'succeeded',$4,$5::jsonb,$6,$6)""",
                uuid4(), idempotency_key, request_hash, str(snapshot["id"]), json.dumps(result), now,
            )
        return result
    finally:
        await connection.close()


@activity.defn(name="load-live-research-plan")
async def load_live_research_plan(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    opportunity_id = UUID(str(request["opportunity_id"]))
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        row = await connection.fetchrow(
            """SELECT rr.id AS research_run_id, rr.state, rr.started_by, rr.correlation_id,
                      o.id AS opportunity_id, o.title, o.summary, o.decision,
                      sp.topic, sp.research_goal, sp.risk, sp.source_requirements,
                      sp.format_policy,sp.approval_profile,sp.domain_policy,
                      cp.editorial_rules AS channel_editorial_rules
               FROM opportunities o
               JOIN subject_profiles sp ON sp.id=o.subject_profile_id
               JOIN channel_profiles cp ON cp.id=sp.channel_profile_id
               JOIN LATERAL (
                 SELECT id,state,started_by,correlation_id FROM research_runs
                 WHERE opportunity_id=o.id AND deleted_at IS NULL
                 ORDER BY created_at DESC LIMIT 1
               ) rr ON true
               WHERE o.id=$1 AND o.deleted_at IS NULL AND sp.deleted_at IS NULL""",
            opportunity_id,
        )
        if row is None:
            raise ApplicationError(
                "opportunity has no completed acquisition run", non_retryable=True
            )
        if row["decision"] != "approved":
            raise ApplicationError("opportunity approval is required", non_retryable=True)
        if row["state"] != "RESEARCHING":
            raise ApplicationError(
                f"research run must be RESEARCHING, not {row['state']}", non_retryable=True
            )
        sources = await connection.fetch(
            """SELECT DISTINCT ON (sd.id)
                      sd.id AS source_document_id, sd.title, sd.canonical_url, sd.publisher,
                      sd.source_type, sd.domain, sd.reputation, ss.id AS source_snapshot_id,
                      ss.normalized_object_key, ss.content_hash, ss.retrieved_at
               FROM opportunity_sources os
               JOIN source_documents sd ON sd.id=os.source_document_id
               JOIN source_snapshots ss ON ss.source_document_id=sd.id
               WHERE os.opportunity_id=$1
                 AND NOT (
                   sd.reputation->>'discovered_via' = 'generic_evidence_search'
                   AND coalesce(sd.reputation->>'relevance_score','') = ''
                 )
               ORDER BY sd.id, ss.snapshot_number DESC""",
            opportunity_id,
        )
        if not sources:
            raise ApplicationError(
                "opportunity has no immutable source snapshots", non_retryable=True
            )
        requirements = _json(row["source_requirements"]) or {}
        latest_query_plan = await connection.fetchrow(
            """SELECT coverage_units,queries FROM research_ai_query_plans
               WHERE opportunity_id=$1 AND abstained=false
               ORDER BY plan_version DESC LIMIT 1""",
            opportunity_id,
        )
        channel_rules = _json(row["channel_editorial_rules"]) or {}
        automation_workflow = channel_rules.get("automation_workflow") or {}
        return {
            "research_run_id": str(row["research_run_id"]),
            "opportunity_id": str(row["opportunity_id"]),
            "opportunity_title": row["title"],
            "opportunity_summary": row["summary"],
            "topic": row["topic"],
            "research_goal": row["research_goal"],
            "risk": row["risk"],
            "format_policy": _json(row["format_policy"]) or {},
            "approval_profile": _json(row["approval_profile"]) or {},
            "channel_workflow": {
                "key": str(automation_workflow.get("key") or ""),
                "name": str(automation_workflow.get("name") or ""),
                "research_review": str(
                    automation_workflow.get("research_review") or "human_dossier"
                ),
            },
            "domain_policy": _json(row["domain_policy"]) or {"allow": [], "block": []},
            "explanation_plan": (
                _json(latest_query_plan["coverage_units"]) if latest_query_plan else []
            ) or [],
            "evidence_search_queries": (
                _json(latest_query_plan["queries"]) if latest_query_plan else []
            ) or [],
            "actor_id": str(request.get("actor_id") or row["started_by"]),
            "correlation_id": str(request.get("correlation_id") or row["correlation_id"]),
            "completion_criteria": {
                "minimum_independent": max(1, int(requirements.get("minimum_independent", 2))),
                "minimum_primary": max(0, int(requirements.get("minimum_primary", 1))),
            },
            "research_plan": {
                "central_question": row["topic"],
                "proposed_angle": row["title"],
                "required_factual_questions": [
                    row["research_goal"],
                    "Which source passages directly support or contradict the central claim?",
                    "Which dates, numbers, names, and quotations require confirmation?",
                ],
                "expected_primary_sources": requirements.get(
                    "expected_primary_types", ["official record", "original study"]
                ),
                "falsification_queries": [
                    f'"{row["topic"]}" correction OR retraction OR counterevidence'
                ],
                "disputed_terms": [],
                "completion_criteria": {
                    "minimum_independent": max(
                        1, int(requirements.get("minimum_independent", 2))
                    ),
                    "minimum_primary": max(0, int(requirements.get("minimum_primary", 1))),
                },
            },
            "sources": [
                {
                    key: str(source[key])
                    if key in {"source_document_id", "source_snapshot_id"}
                    else source[key]
                    for key in (
                        "source_document_id",
                        "source_snapshot_id",
                        "normalized_object_key",
                        "content_hash",
                        "title",
                        "canonical_url",
                        "publisher",
                        "source_type",
                        "domain",
                        "reputation",
                    )
                }
                for source in sources
            ],
        }
    finally:
        await connection.close()


@activity.defn(name="extract-snapshot-evidence")
async def extract_snapshot_evidence(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    source = request["source"]
    minio = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    body = await asyncio.to_thread(
        _read_minio_object,
        minio,
        settings.minio_bucket,
        str(source["normalized_object_key"]),
        min(settings.acquisition_max_bytes, 2_000_000),
    )
    text = body.decode("utf-8", errors="replace")
    evidence = extract_evidence_sentences(
        text,
        topic=str(request["topic"]),
        research_goal=str(request.get("research_goal", "")),
        maximum_sentences=10,
    )
    chunks = chunk_semantic_text(text)
    return {
        **source,
        "evidence": [item.__dict__ for item in evidence],
        "chunks": [
            {**item.__dict__, "embedding": list(item.embedding)} for item in chunks
        ],
        "normalized_byte_size": len(body),
    }


@activity.defn(name="persist-live-research-dossier")
async def persist_live_research_dossier(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    workflow_id = str(request["workflow_id"])
    idempotency_key = str(request["idempotency_key"])
    plan = request["plan"]
    extracted_sources = request.get("extracted_sources", [])
    candidates = [
        {**evidence, "source": source}
        for source in extracted_sources
        for evidence in source.get("evidence", [])[:6]
    ]
    if not candidates:
        raise ApplicationError(
            "no bounded evidence sentences could be extracted", non_retryable=True
        )
    clusters = cluster_evidence_statements(item["exact_text"] for item in candidates)
    ordered_clusters = sorted(
        clusters,
        key=lambda cluster: (
            -len({_source_independence_key(candidates[index]["source"]) for index in cluster}),
            -max(int(candidates[index]["relevance_score"]) for index in cluster),
            cluster[0],
        ),
    )[:12]
    near_duplicate_pairs, dependency_groups = detect_dependent_sources(extracted_sources)
    request_hash = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        existing = await connection.fetchval(
            """SELECT result FROM idempotency_records
               WHERE scope='research.live_dossier' AND idempotency_key=$1 AND status='succeeded'""",
            idempotency_key,
        )
        if existing:
            return _json(existing)
        now = datetime.now(timezone.utc)
        opportunity_id = UUID(str(plan["opportunity_id"]))
        research_run_id = UUID(str(plan["research_run_id"]))
        actor_id = UUID(str(plan["actor_id"]))
        criteria = plan["completion_criteria"]
        dossier_id = uuid4()
        dossier_version = 1
        prepared_claims = prepare_ai_synthesized_claims(
            request.get("ai_synthesis"),
            candidates,
            dependency_groups,
            minimum_independent=int(criteria["minimum_independent"]),
            coverage_unit_ids={
                str(item.get("id")) for item in plan.get("explanation_plan", [])
            },
        )
        ai_claims_applied = bool(prepared_claims)
        for position, cluster in enumerate(ordered_clusters if not prepared_claims else []):
            representative_index = max(
                cluster, key=lambda index: int(candidates[index]["relevance_score"])
            )
            representative = candidates[representative_index]
            links: list[dict[str, Any]] = []
            for index in cluster:
                candidate = candidates[index]
                source = candidate["source"]
                relation = evidence_relation(
                    representative["exact_text"], candidate["exact_text"]
                )
                links.append(
                    {
                        "candidate": candidate,
                        "relationship": relation,
                        "independent": True,
                        "primary": _is_primary_source(source),
                        "direct": relation != "context",
                    }
                )
            support_domains = {
                _claim_independence_key(link["candidate"]["source"], dependency_groups)
                for link in links
                if link["relationship"] == "supports"
            }
            has_contradiction = any(
                link["relationship"] == "contradicts" for link in links
            )
            prepared_claims.append(
                {
                    "id": uuid4(),
                    "statement": representative["exact_text"],
                    "claim_type": classify_claim(representative["exact_text"]),
                    "central": position < 3,
                    "confidence": min(95, 48 + len(support_domains) * 16),
                    "status": "disputed" if has_contradiction else "supported",
                    "coverage_unit_ids": [],
                    "links": links,
                }
            )

        policy_claims = tuple(
            AtomicClaim(
                claim_id=str(claim["id"]),
                statement=claim["statement"],
                claim_type=ClaimType(claim["claim_type"]),
                central=claim["central"],
                risk=RiskLevel(str(plan["risk"])),
                evidence=tuple(
                    EvidenceLink(
                        excerpt_id=link["candidate"]["excerpt_hash"],
                        source_id=link["candidate"]["source"]["source_document_id"],
                        relation=EvidenceRelation(link["relationship"]),
                        independent=link["independent"],
                        primary=link["primary"],
                        direct=link["direct"],
                    )
                    for link in claim["links"]
                ),
            )
            for claim in prepared_claims
        )
        completion = evaluate_research_completion(
            policy_claims,
            minimum_independent_sources=int(criteria["minimum_independent"]),
            require_primary_for_high_risk=int(criteria["minimum_primary"]) > 0,
        )
        supporting_sources = {
            link.source_id
            for claim in policy_claims
            for link in claim.evidence
            if link.relation is EvidenceRelation.SUPPORTS and link.independent
        }
        primary_sources = {
            link.source_id
            for claim in policy_claims
            for link in claim.evidence
            if link.primary and link.direct
        }
        readiness = evaluate_explanation_readiness(
            policy=explanation_policy(
                plan.get("format_policy", {}), plan.get("approval_profile", {})
            ),
            claims=_readiness_claims(prepared_claims),
            coverage_units=plan.get("explanation_plan", []),
            minimum_independent_sources=int(criteria["minimum_independent"]),
            minimum_primary_sources=int(criteria["minimum_primary"]),
            counterevidence_search_completed=bool(
                request.get("counterevidence_search_completed")
            ),
        )
        completion_document = {
            "complete": bool(completion.complete and readiness.ready),
            "research_complete": completion.complete,
            "blockers": [*completion.blockers, *readiness.gaps],
            "independent_supporting_sources": len(supporting_sources),
            "primary_sources": len(primary_sources),
            "criteria": criteria,
            "policy": "deterministic-research-completion-v1",
            "claim_synthesis": (
                "source-bound-lan-ai-v1"
                if ai_claims_applied
                else "deterministic-lexical-v1"
            ),
            "ai_synthesis_id": (request.get("ai_synthesis") or {}).get("synthesis_id"),
            "explanation_readiness": {
                **readiness.as_dict(),
                "enrichment_round": int(request.get("enrichment_round", 0)),
            },
        }
        years = sorted(
            {
                match.group(0)
                for candidate in candidates
                for match in _YEAR.finditer(candidate["exact_text"])
            }
        )
        safe_conclusions = [
            claim["statement"]
            for claim in prepared_claims
            if len(
                {
                    _source_independence_key(link["candidate"]["source"])
                    for link in claim["links"]
                    if link["relationship"] == "supports" and link["independent"]
                }
            )
            >= int(criteria["minimum_independent"])
        ][:5]
        automatic_source_brief = (
            plan.get("channel_workflow", {}).get("research_review")
            == "automatic_source_brief"
        )
        automatic_handoff = bool(completion_document["complete"] and automatic_source_brief)
        if automatic_handoff:
            completion_document["review_mode"] = "automatic_source_brief"
        result = {
            "research_run_id": str(research_run_id),
            "dossier_id": str(dossier_id),
            "claim_count": len(prepared_claims),
            "evidence_count": sum(len(claim["links"]) for claim in prepared_claims),
            "source_count": len(extracted_sources),
            "completion_met": bool(completion.complete and readiness.ready),
            "explanation_readiness": completion_document["explanation_readiness"],
            "review_mode": (
                "automatic_source_brief" if automatic_source_brief else "human_dossier"
            ),
        }
        if automatic_handoff:
            script_workflow_id = f"script-generation-source-brief-{dossier_id.hex}-v1"
            result["automatic_script_request"] = {
                "workflow_id": script_workflow_id,
                "dossier_id": str(dossier_id),
                "expected_dossier_version": 1,
                "sensitivity": "internal",
                "idempotency_key": f"source-brief-{dossier_id.hex}-v1",
                "actor_id": str(actor_id),
                "correlation_id": str(plan["correlation_id"]),
            }
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"research.live_dossier:{idempotency_key}",
            )
            existing = await connection.fetchval(
                """SELECT result FROM idempotency_records
                   WHERE scope='research.live_dossier' AND idempotency_key=$1
                     AND status='succeeded'""",
                idempotency_key,
            )
            if existing:
                return _json(existing)
            run_state = await connection.fetchval(
                "SELECT state FROM research_runs WHERE id=$1 FOR UPDATE", research_run_id
            )
            if run_state != "RESEARCHING":
                raise ApplicationError(
                    f"research run must be RESEARCHING, not {run_state}", non_retryable=True
                )
            dossier_version = await connection.fetchval(
                "SELECT COALESCE(MAX(dossier_version),0)+1 FROM research_dossiers WHERE opportunity_id=$1",
                opportunity_id,
            )
            status = (
                "approved"
                if automatic_handoff
                else "in_review" if completion_document["complete"] else "blocked"
            )
            review_comment = (
                "Automatically accepted as a source brief after deterministic readiness checks; "
                "the human topic selection authorized script preparation."
                if automatic_handoff
                else None
            )
            await connection.execute(
                """INSERT INTO research_dossiers
                (id,version,created_at,updated_at,deleted_at,opportunity_id,dossier_version,status,
                 executive_summary,chronology,unresolved_questions,alternative_explanations,
                 source_quality_notes,safe_conclusions,prohibited_overstatements,proposed_angles,
                 explanation_plan,completion_evaluation,reviewed_by,reviewed_at,review_comment)
                VALUES($1,1,$2,$2,NULL,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,
                       $11::jsonb,$12::jsonb,$13::jsonb,$14::jsonb,$15::jsonb,$16,$17,$18)""",
                dossier_id,
                now,
                opportunity_id,
                dossier_version,
                status,
                (
                    f"Source brief assembled from {len(candidates)} relevant passages in "
                    f"{len(extracted_sources)} immutable source snapshot(s); readiness passed "
                    "and script preparation continues automatically."
                    if automatic_handoff
                    else f"Extracted {len(candidates)} bounded evidence passages from "
                    f"{len(extracted_sources)} immutable source snapshot(s); editorial review is required."
                ),
                json.dumps(
                    [
                        {"date": year, "event": "Date appears in extracted source evidence"}
                        for year in years
                    ]
                ),
                json.dumps(
                    [
                        *plan["research_plan"]["required_factual_questions"],
                        *completion.blockers,
                    ]
                ),
                json.dumps(
                    [
                        "Correlated reporting may repeat a common upstream source.",
                        "The extracted passages may not establish causation or full context.",
                    ]
                ),
                json.dumps(
                    [
                        f"{source['title']} ({source['domain']}): type={source['source_type']}; immutable snapshot {source['content_hash'][:12]}"
                        for source in extracted_sources
                    ]
                ),
                json.dumps(safe_conclusions),
                json.dumps(
                    [
                        "Do not present a single-source statement as independently confirmed.",
                        "Do not infer causation from correlation or chronology alone.",
                        "Do not treat source popularity or search rank as proof.",
                    ]
                ),
                json.dumps([plan["opportunity_title"]]),
                json.dumps(plan.get("explanation_plan", [])),
                json.dumps(completion_document),
                actor_id if automatic_handoff else None,
                now if automatic_handoff else None,
                review_comment,
            )
            for source in extracted_sources:
                for chunk_number, chunk in enumerate(source.get("chunks", []), start=1):
                    await connection.execute(
                        """INSERT INTO semantic_chunks
                        (id,source_snapshot_id,chunk_number,text,location_anchor,start_offset,
                         end_offset,chunk_hash,token_count,embedding_model,embedding,created_at)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'feature-hash-v1',$10::vector,$11)
                        ON CONFLICT (source_snapshot_id,chunk_number) DO NOTHING""",
                        uuid4(),
                        UUID(str(source["source_snapshot_id"])),
                        chunk_number,
                        chunk["text"],
                        chunk["location_anchor"],
                        chunk["start_offset"],
                        chunk["end_offset"],
                        chunk["chunk_hash"],
                        chunk["token_count"],
                        _vector_literal(chunk["embedding"]),
                        now,
                    )
            for relationship in near_duplicate_pairs:
                await connection.execute(
                    """INSERT INTO source_relationships
                    (id,source_document_id,related_source_document_id,relationship,reason,
                     confidence,created_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT (source_document_id,related_source_document_id,relationship)
                    DO NOTHING""",
                    uuid4(),
                    UUID(relationship["source_document_id"]),
                    UUID(relationship["related_source_document_id"]),
                    relationship["relationship"],
                    relationship["reason"],
                    relationship["confidence"],
                    now,
                )
            for claim in prepared_claims:
                await connection.execute(
                    """INSERT INTO claims
                    (id,version,created_at,updated_at,deleted_at,research_dossier_id,
                     normalized_statement,claim_type,scope,relevant_at,entities,confidence,status,
                     risk,central,coverage_unit_ids,review_comment,reviewed_by,reviewed_at)
                    VALUES($1,1,$2,$2,NULL,$3,$4,$5,$6,NULL,'[]'::jsonb,$7,$8,$9,$10,$11::jsonb,$12,$13,$14)""",
                    claim["id"],
                    now,
                    dossier_id,
                    claim["statement"],
                    claim["claim_type"],
                    plan["topic"],
                    claim["confidence"],
                    (
                        "approved"
                        if automatic_handoff and claim["status"] == "supported"
                        else claim["status"]
                    ),
                    plan["risk"],
                    claim["central"],
                    json.dumps(claim.get("coverage_unit_ids", [])),
                    review_comment if automatic_handoff else None,
                    actor_id if automatic_handoff else None,
                    now if automatic_handoff else None,
                )
                for link in claim["links"]:
                    candidate = link["candidate"]
                    source = candidate["source"]
                    excerpt_id = await connection.fetchval(
                        """INSERT INTO evidence_excerpts
                        (id,source_snapshot_id,exact_text,prefix_text,suffix_text,location_anchor,
                         start_offset,end_offset,excerpt_hash,created_at)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        ON CONFLICT (source_snapshot_id,excerpt_hash,location_anchor) DO NOTHING
                        RETURNING id""",
                        uuid4(),
                        UUID(str(source["source_snapshot_id"])),
                        candidate["exact_text"],
                        candidate["prefix_text"],
                        candidate["suffix_text"],
                        candidate["location_anchor"],
                        candidate["start_offset"],
                        candidate["end_offset"],
                        candidate["excerpt_hash"],
                        now,
                    )
                    if excerpt_id is None:
                        excerpt_id = await connection.fetchval(
                            """SELECT id FROM evidence_excerpts
                               WHERE source_snapshot_id=$1 AND excerpt_hash=$2
                                 AND location_anchor=$3""",
                            UUID(str(source["source_snapshot_id"])),
                            candidate["excerpt_hash"],
                            candidate["location_anchor"],
                        )
                    await connection.execute(
                        """INSERT INTO claim_evidence
                        (id,claim_id,evidence_excerpt_id,relationship,source_independent,
                         direct_evidence,primary_source,notes,created_at)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                        uuid4(),
                        claim["id"],
                        excerpt_id,
                        link["relationship"],
                        link["independent"],
                        link["direct"],
                        link["primary"],
                        (
                            "source-bound evidence extraction; automatically accepted with the source brief"
                            if automatic_handoff
                            else "source-bound evidence extraction; reviewer confirmation required"
                        ),
                        now,
                    )
            await connection.execute(
                """UPDATE research_runs
                   SET state=$2, research_plan=$3::jsonb,
                       progress=$4::jsonb, completed_at=$5, updated_at=$5, version=version+1
                   WHERE id=$1""",
                research_run_id,
                (
                    "SCRIPTING"
                    if automatic_handoff
                    else "DOSSIER_REVIEW" if completion_document["complete"]
                    else "INSUFFICIENT_EXPLANATION_EVIDENCE"
                ),
                json.dumps(plan["research_plan"]),
                json.dumps(
                    {
                        "percent": 100,
                        "sources": len(extracted_sources),
                        "claims": len(prepared_claims),
                        "evidence_links": result["evidence_count"],
                        "completion_met": completion_document["complete"],
                        "explanation_readiness": completion_document["explanation_readiness"],
                    }
                ),
                now,
            )
            await connection.execute(
                """INSERT INTO workflow_transitions
                (id,aggregate_type,aggregate_id,from_stage,to_stage,reason,actor_id,
                 correlation_id,occurred_at)
                VALUES($1,'opportunity',$2,'RESEARCHING',$3,$4,$5,$6,$7)""",
                uuid4(),
                opportunity_id,
                (
                    "SCRIPTING"
                    if automatic_handoff
                    else "DOSSIER_REVIEW" if completion_document["complete"] else "PAUSED"
                ),
                (
                    "trusted source brief passed deterministic readiness and continued automatically"
                    if automatic_handoff
                    else "immutable evidence extracted into a versioned review dossier"
                ),
                actor_id,
                plan["correlation_id"],
                now,
            )
            if automatic_handoff:
                script_request = result["automatic_script_request"]
                await connection.execute(
                    """INSERT INTO workflow_control_records
                    (workflow_id,workflow_type,request_payload,parent_workflow_id,
                     correlation_id,started_by,created_at)
                    VALUES($1,'script-generation',$2::jsonb,$3,$4,$5,$6)
                    ON CONFLICT (workflow_id) DO NOTHING""",
                    script_request["workflow_id"],
                    json.dumps(script_request),
                    workflow_id,
                    plan["correlation_id"],
                    actor_id,
                    now,
                )
                await _append_audit(
                    connection,
                    action="dossier.source_brief_accepted",
                    actor_id=actor_id,
                    target_type="research_dossier",
                    target_id=str(dossier_id),
                    correlation_id=str(plan["correlation_id"]),
                    context={
                        "workflow_key": plan.get("channel_workflow", {}).get("key"),
                        "dossier_version": 1,
                        "completion_met": True,
                        "claim_ids": [
                            str(claim["id"])
                            for claim in prepared_claims
                            if claim["status"] == "supported"
                        ],
                        "automatic_continuation_workflow_id": script_request["workflow_id"],
                    },
                )
            await connection.execute(
                """INSERT INTO idempotency_records
                (id,scope,idempotency_key,request_hash,status,external_id,result,created_at,updated_at)
                VALUES($1,'research.live_dossier',$2,$3,'succeeded',$4,$5::jsonb,$6,$6)""",
                uuid4(),
                idempotency_key,
                request_hash,
                workflow_id,
                json.dumps(result),
                now,
            )
        return result
    finally:
        await connection.close()
