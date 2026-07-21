from __future__ import annotations

import asyncio
import hashlib
import json
import re
import math
from collections import Counter
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
from editorial_core.research import (
    chunk_semantic_text,
    classify_claim,
    cluster_evidence_statements,
    evidence_relation,
    extract_evidence_sentences,
)
from research_worker.config import Settings


_PRIMARY_TYPES = {"primary", "official", "official record", "original study", "authoritative"}
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _cosine(left: list[float], right: list[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def detect_dependent_sources(
    extracted_sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    relationships: list[dict[str, Any]] = []
    dependent_documents: set[str] = set()
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
            if not exact_duplicate and similarity < 0.82:
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
                    "confidence": 100 if exact_duplicate else min(99, round(similarity * 100)),
                    "reason": (
                        "identical acquired content or semantic chunk hash"
                        if exact_duplicate
                        else f"local feature-hash chunk cosine similarity {similarity:.3f}"
                    ),
                }
            )
            dependent_documents.update((first, second))
    return relationships, dependent_documents


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
                      sp.topic, sp.research_goal, sp.risk, sp.source_requirements
               FROM opportunities o
               JOIN subject_profiles sp ON sp.id=o.subject_profile_id
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
                      sd.source_type, sd.domain, ss.id AS source_snapshot_id,
                      ss.normalized_object_key, ss.content_hash, ss.retrieved_at
               FROM opportunity_sources os
               JOIN source_documents sd ON sd.id=os.source_document_id
               JOIN source_snapshots ss ON ss.source_document_id=sd.id
               WHERE os.opportunity_id=$1
               ORDER BY sd.id, ss.snapshot_number DESC""",
            opportunity_id,
        )
        if not sources:
            raise ApplicationError(
                "opportunity has no immutable source snapshots", non_retryable=True
            )
        requirements = _json(row["source_requirements"]) or {}
        return {
            "research_run_id": str(row["research_run_id"]),
            "opportunity_id": str(row["opportunity_id"]),
            "opportunity_title": row["title"],
            "opportunity_summary": row["summary"],
            "topic": row["topic"],
            "research_goal": row["research_goal"],
            "risk": row["risk"],
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
        for evidence in source.get("evidence", [])[:4]
    ]
    if not candidates:
        raise ApplicationError(
            "no bounded evidence sentences could be extracted", non_retryable=True
        )
    clusters = cluster_evidence_statements(item["exact_text"] for item in candidates)
    ordered_clusters = sorted(
        clusters,
        key=lambda cluster: (
            -len({candidates[index]["source"]["domain"] for index in cluster}),
            -max(int(candidates[index]["relevance_score"]) for index in cluster),
            cluster[0],
        ),
    )[:12]
    near_duplicate_pairs, dependent_documents = detect_dependent_sources(extracted_sources)
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
        prepared_claims: list[dict[str, Any]] = []
        for position, cluster in enumerate(ordered_clusters):
            representative_index = max(
                cluster, key=lambda index: int(candidates[index]["relevance_score"])
            )
            representative = candidates[representative_index]
            domains = [candidates[index]["source"]["domain"] for index in cluster]
            domain_counts = Counter(domains)
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
                        "independent": domain_counts[source["domain"]] == 1
                        and source["source_document_id"] not in dependent_documents,
                        "primary": str(source["source_type"]).casefold() in _PRIMARY_TYPES,
                        "direct": relation != "context",
                    }
                )
            support_domains = {
                link["candidate"]["source"]["domain"]
                for link in links
                if link["relationship"] == "supports" and link["independent"]
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
        completion_document = {
            "complete": completion.complete,
            "blockers": list(completion.blockers),
            "independent_supporting_sources": len(supporting_sources),
            "primary_sources": len(primary_sources),
            "criteria": criteria,
            "policy": "deterministic-research-completion-v1",
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
                    link["candidate"]["source"]["domain"]
                    for link in claim["links"]
                    if link["relationship"] == "supports" and link["independent"]
                }
            )
            >= int(criteria["minimum_independent"])
        ][:5]
        result = {
            "research_run_id": str(research_run_id),
            "dossier_id": str(dossier_id),
            "claim_count": len(prepared_claims),
            "evidence_count": sum(len(claim["links"]) for claim in prepared_claims),
            "source_count": len(extracted_sources),
            "completion_met": completion.complete,
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
            status = "in_review" if completion.complete else "blocked"
            await connection.execute(
                """INSERT INTO research_dossiers
                (id,version,created_at,updated_at,deleted_at,opportunity_id,dossier_version,status,
                 executive_summary,chronology,unresolved_questions,alternative_explanations,
                 source_quality_notes,safe_conclusions,prohibited_overstatements,proposed_angles,
                 completion_evaluation,reviewed_by,reviewed_at,review_comment)
                VALUES($1,1,$2,$2,NULL,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,
                       $11::jsonb,$12::jsonb,$13::jsonb,$14::jsonb,NULL,NULL,NULL)""",
                dossier_id,
                now,
                opportunity_id,
                dossier_version,
                status,
                f"Extracted {len(candidates)} bounded evidence passages from {len(extracted_sources)} immutable source snapshot(s); editorial review is required.",
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
                json.dumps(completion_document),
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
                     risk,central,review_comment,reviewed_by,reviewed_at)
                    VALUES($1,1,$2,$2,NULL,$3,$4,$5,$6,NULL,'[]'::jsonb,$7,$8,$9,$10,NULL,NULL,NULL)""",
                    claim["id"],
                    now,
                    dossier_id,
                    claim["statement"],
                    claim["claim_type"],
                    plan["topic"],
                    claim["confidence"],
                    claim["status"],
                    plan["risk"],
                    claim["central"],
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
                        "deterministic lexical evidence extraction; reviewer confirmation required",
                        now,
                    )
            await connection.execute(
                """UPDATE research_runs
                   SET state='DOSSIER_REVIEW', research_plan=$2::jsonb,
                       progress=$3::jsonb, completed_at=$4, updated_at=$4, version=version+1
                   WHERE id=$1""",
                research_run_id,
                json.dumps(plan["research_plan"]),
                json.dumps(
                    {
                        "percent": 100,
                        "sources": len(extracted_sources),
                        "claims": len(prepared_claims),
                        "evidence_links": result["evidence_count"],
                        "completion_met": completion.complete,
                    }
                ),
                now,
            )
            await connection.execute(
                """INSERT INTO workflow_transitions
                (id,aggregate_type,aggregate_id,from_stage,to_stage,reason,actor_id,
                 correlation_id,occurred_at)
                VALUES($1,'opportunity',$2,'RESEARCHING','DOSSIER_REVIEW',$3,$4,$5,$6)""",
                uuid4(),
                opportunity_id,
                "immutable evidence extracted into a versioned review dossier",
                actor_id,
                plan["correlation_id"],
                now,
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
