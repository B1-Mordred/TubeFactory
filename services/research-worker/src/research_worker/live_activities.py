from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4
from urllib.parse import urlsplit

import asyncpg
from temporalio import activity

from editorial_core.discovery import (
    FindingCluster,
    OpportunityScore,
    SearchFinding,
    SubjectBrief,
    cluster_findings,
    deduplicate_findings,
    plan_search,
    score_opportunity,
)
from research_worker.acquisition import DomainPolicy, search_searxng
from research_worker.config import Settings


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _publication_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _finding_signature(title: str, summary: str) -> tuple[str, frozenset[str]]:
    """Return an exact fingerprint and tokens for cross-run claim-cluster checks.

    Discovery results do not include the complete article body. The exact fingerprint
    therefore covers the normalized title and search excerpt; immutable full-content
    hashes and near-duplicate checks are applied later during source acquisition.
    """

    normalized = " ".join(f"{title} {summary}".casefold().split())
    fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
    return fingerprint, frozenset(re.findall(r"[\w-]{3,}", normalized))


def _matches_processed_claim(
    finding: SearchFinding,
    processed_signatures: tuple[tuple[str, frozenset[str]], ...],
    *,
    similarity_threshold: float = 0.86,
) -> bool:
    fingerprint, tokens = _finding_signature(finding.title, finding.summary)
    for processed_fingerprint, processed_tokens in processed_signatures:
        if fingerprint == processed_fingerprint:
            return True
        union = tokens | processed_tokens
        similarity = len(tokens & processed_tokens) / len(union) if union else 0.0
        if similarity >= similarity_threshold:
            return True
    return False


def _clamped_score(value: float) -> int:
    return round(max(0.0, min(100.0, value)))


def _similarity(tokens: frozenset[str], other: frozenset[str]) -> float:
    union = tokens | other
    return len(tokens & other) / len(union) if union else 0.0


def _score_live_cluster(
    cluster: FindingCluster,
    provenance: dict[str, tuple[dict[str, Any], dict[str, Any], int]],
    processed_signatures: tuple[tuple[str, frozenset[str]], ...],
    *,
    topic: str,
    subject_risk: str,
    lookback_days: int,
    total_unique_domains: int,
    weights: dict[str, float] | None = None,
    now: datetime | None = None,
    source_provenance_available: bool = True,
) -> OpportunityScore:
    """Derive an auditable score from the actual search result and cluster signals."""

    observed_at = now or datetime.now(timezone.utc)
    representative = cluster.findings[0]
    text = f"{representative.title} {representative.summary}"
    _, content_tokens = _finding_signature(representative.title, representative.summary)
    _, topic_tokens = _finding_signature(topic, "")
    scored_findings = cluster.findings if source_provenance_available else ()
    cluster_domains = {
        hostname
        for finding in scored_findings
        if (hostname := urlsplit(finding.canonical_url).hostname)
    }
    entries = [provenance.get(finding.canonical_url, ({}, {}, 10)) for finding in scored_findings]
    best_rank = min((rank for _, _, rank in entries), default=None)
    rank_quality = max(0.0, min(1.0, 1.0 - (best_rank - 1) / 12)) if best_rank is not None else 0.0
    query_tokens = frozenset(
        token
        for _, strategy, _ in entries
        for token in _finding_signature(str(strategy.get("query", "")), "")[1]
    )
    topic_coverage = min(1.0, len(content_tokens & topic_tokens) / max(min(len(topic_tokens), 12), 1))
    query_coverage = min(1.0, len(content_tokens & query_tokens) / max(min(len(query_tokens), 16), 1))
    purposes = {str(strategy.get("purpose", "discovery")) for _, strategy, _ in entries}
    source_types = {finding.source_type.casefold() for finding in scored_findings}
    primary_count = sum(
        source_type in {"primary", "authoritative", "official record", "original study"}
        for source_type in source_types
    )
    has_number = bool(re.search(r"\d", text))
    has_date_language = bool(re.search(r"\b(?:19|20)\d{2}\b|\b(?:today|yesterday|heute|gestern)\b", text, re.I))
    specificity = min(1.0, len(content_tokens) / 45)
    published_dates = [parsed for finding in scored_findings if (parsed := _publication_at(finding.published_at))]
    if published_dates:
        newest = max(published_dates)
        age_days = max(0.0, (observed_at - newest).total_seconds() / 86_400)
        timeliness = _clamped_score(100 - min(age_days / max(lookback_days, 1), 1.5) * 60)
        freshness_reason = f"newest result is {age_days:.1f} days old within a {lookback_days}-day window"
    elif best_rank is not None:
        timeliness = _clamped_score(45 + rank_quality * 20)
        freshness_reason = f"no reliable publication date; search rank {best_rank} supplies limited recency evidence"
    else:
        timeliness = 35
        freshness_reason = "no publication date or search rank is available"
    maximum_prior_similarity = max(
        (_similarity(content_tokens, previous_tokens) for _, previous_tokens in processed_signatures),
        default=0.0,
    )
    result_count = len(scored_findings)
    repeated_domain_ratio = max(0.0, (result_count - len(cluster_domains)) / max(result_count, 1))
    domain_rarity = (
        1.0 - min(1.0, len(cluster_domains) / max(total_unique_domains, 1))
        if cluster_domains
        else 0.0
    )

    positive = {
        "audience_fit": _clamped_score(30 + topic_coverage * 35 + query_coverage * 20 + rank_quality * 15),
        "evidence_potential": _clamped_score(
            24 + result_count * 9 + len(cluster_domains) * 12 + primary_count * 12 + specificity * 18
        ),
        "novelty": _clamped_score(92 - maximum_prior_similarity * 65),
        "timeliness": timeliness,
        "educational_value": _clamped_score(
            32 + specificity * 42 + (10 if has_number else 0) + min(16, len(cluster_domains) * 6)
        ),
        "visual_explainability": _clamped_score(
            28 + specificity * 24 + (22 if has_number else 0) + (10 if has_date_language else 0) + min(16, len(cluster_domains) * 6)
        ),
        "channel_differentiation": _clamped_score(
            30 + topic_coverage * 30 + (14 if purposes & {"primary evidence", "falsification"} else 4 if purposes else 0) + (10 if primary_count else 0) + domain_rarity * 12
        ),
    }
    risk_base = {"low": 12, "medium": 25, "high": 38}.get(subject_risk.casefold(), 25)
    penalties = {
        "risk": _clamped_score(risk_base + (6 if published_dates and timeliness >= 85 else 0)),
        "estimated_cost": _clamped_score(7 + result_count * 5 + specificity * 8 + primary_count * 3),
        "duplication": _clamped_score(maximum_prior_similarity * 70 + repeated_domain_ratio * 30),
    }
    scored = score_opportunity(positive, penalties, weights=weights)
    reasoning = (
        f"Audience fit {positive['audience_fit']}: topic coverage {topic_coverage:.0%}, query coverage {query_coverage:.0%}, best search rank {best_rank if best_rank is not None else 'unavailable'}.",
        f"Evidence potential {positive['evidence_potential']}: {result_count} result(s), {len(cluster_domains)} domain(s), {primary_count} primary/authoritative source type(s).",
        f"Novelty {positive['novelty']}: closest prior finding token similarity {maximum_prior_similarity:.0%}.",
        f"Timeliness {positive['timeliness']}: {freshness_reason}.",
        f"Educational value {positive['educational_value']}: {len(content_tokens)} distinct content terms; numerical specificity {'present' if has_number else 'absent'}.",
        f"Visual explainability {positive['visual_explainability']}: numerical signal {'present' if has_number else 'absent'}, date signal {'present' if has_date_language else 'absent'}.",
        f"Channel differentiation {positive['channel_differentiation']}: search purposes {', '.join(sorted(purposes)) or 'unavailable'}; topic coverage {topic_coverage:.0%}; domain rarity {domain_rarity:.0%}.",
        f"Risk penalty {penalties['risk']}: subject risk is {subject_risk}; very recent material adds review pressure when applicable.",
        f"Cost penalty {penalties['estimated_cost']}: estimated from {result_count} source(s), content specificity and source type.",
        f"Duplication penalty {penalties['duplication']}: prior similarity {maximum_prior_similarity:.0%}; repeated-domain ratio {repeated_domain_ratio:.0%}.",
        f"Final weighted score {scored.total}/100. The complete component values, penalties and configured weights are stored immutably.",
    )
    return OpportunityScore(scored.total, scored.positive, scored.penalties, reasoning)


@activity.defn(name="load-live-search-plan")
async def load_live_search_plan(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        subject = await connection.fetchrow(
            """SELECT id, topic, research_goal, seed_queries, related_concepts,
                      negative_keywords, languages, regions, source_requirements,
                      domain_policy, opportunity_weights, freshness_policy, risk
               FROM subject_profiles
               WHERE id=$1 AND enabled=true AND deleted_at IS NULL""",
            UUID(str(request["subject_profile_id"])),
        )
        if subject is None:
            raise ValueError("enabled subject profile does not exist")
        workflow_id = str(request.get("workflow_id", ""))
        if workflow_id:
            actor_id = request.get("actor_id")
            await connection.execute(
                """INSERT INTO workflow_control_records
                   (workflow_id,workflow_type,request_payload,parent_workflow_id,correlation_id,
                    started_by,created_at)
                   VALUES($1,$2,$3::jsonb,NULL,$4,$5,$6)
                   ON CONFLICT (workflow_id) DO NOTHING""",
                workflow_id,
                # Scheduled discovery shares the live-discovery control and retry surface.
                "live-discovery",
                json.dumps(request),
                str(request.get("correlation_id", workflow_id))[:160],
                UUID(str(actor_id)) if actor_id else None,
                datetime.now(timezone.utc),
            )
        requirements = _json(subject["source_requirements"]) or {}
        plan = plan_search(
            SubjectBrief(
                topic=subject["topic"],
                research_goal=subject["research_goal"],
                seed_queries=tuple(_json(subject["seed_queries"])),
                related_concepts=tuple(_json(subject["related_concepts"])),
                negative_keywords=tuple(_json(subject["negative_keywords"])),
                languages=tuple(_json(subject["languages"])),
                regions=tuple(_json(subject["regions"])),
                expected_primary_source_types=tuple(
                    requirements.get("expected_primary_types", ("official record", "original study"))
                ),
            )
        )
        strategies = [
            {
                "purpose": item.purpose,
                "query": item.query,
                "language": item.language,
                "region": item.region,
            }
            for item in plan.strategies
        ]
        strategies.extend(
            {
                "purpose": "falsification",
                "query": query,
                "language": plan.strategies[0].language,
                "region": plan.strategies[0].region,
            }
            for query in plan.falsification_queries
        )
        return {
            "subject_profile_id": str(subject["id"]),
            "topic": subject["topic"],
            "strategies": strategies[:12],
            "domain_policy": _json(subject["domain_policy"]) or {"allow": [], "block": []},
            "opportunity_weights": _json(subject["opportunity_weights"]) or {},
            "freshness_policy": _json(subject["freshness_policy"]) or {"lookback_days": 30},
            "risk": subject["risk"],
        }
    finally:
        await connection.close()


@activity.defn(name="search-live-strategy")
async def search_live_strategy(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    strategy = request["strategy"]
    results = await search_searxng(
        settings.searxng_endpoint,
        query=str(strategy["query"])[:2000],
        language=str(strategy["language"])[:20],
        policy=DomainPolicy.from_mapping(request.get("domain_policy")),
        lookback_days=max(1, int((request.get("freshness_policy") or {}).get("lookback_days", 30))),
    )
    return {"strategy": strategy, "results": results}


@activity.defn(name="persist-live-opportunities")
async def persist_live_opportunities(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    workflow_id = str(request["workflow_id"])
    idempotency_key = str(request["idempotency_key"])
    subject_id = UUID(str(request["subject_profile_id"]))
    result_sets = request.get("result_sets", [])
    entries = [
        (item, result_set.get("strategy", {}), rank)
        for result_set in result_sets
        for rank, item in enumerate(result_set.get("results", []), start=1)
    ]
    raw_findings = [item for item, _, _ in entries]
    findings = tuple(
        SearchFinding(
            url=str(item["url"]),
            title=str(item["title"]),
            summary=str(item.get("summary", "")),
            published_at=item.get("published_at"),
            source_type=str(item.get("source_type", "secondary")),
        )
        for item in raw_findings
    )
    unique = deduplicate_findings(findings)
    provenance: dict[str, tuple[dict[str, Any], dict[str, Any], int]] = {}
    for item, strategy, rank in entries:
        try:
            canonical = SearchFinding(str(item["url"]), str(item["title"]), "").canonical_url
        except (KeyError, ValueError):
            continue
        provenance.setdefault(canonical, (item, strategy, rank))
    request_hash = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        existing = await connection.fetchval(
            """SELECT result FROM idempotency_records
               WHERE scope='research.live_discovery' AND idempotency_key=$1 AND status='succeeded'""",
            idempotency_key,
        )
        if existing:
            return _json(existing)
        now = datetime.now(timezone.utc)
        opportunity_ids: list[str] = []
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"research.live_discovery:{idempotency_key}",
            )
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"research.live_discovery.subject:{subject_id}",
            )
            existing = await connection.fetchval(
                """SELECT result FROM idempotency_records
                   WHERE scope='research.live_discovery' AND idempotency_key=$1 AND status='succeeded'""",
                idempotency_key,
            )
            if existing:
                return _json(existing)
            weights = request.get("opportunity_weights") or {}
            previously_processed_urls = set(
                await connection.fetchval(
                    """SELECT coalesce(array_agg(canonical_url), ARRAY[]::text[])
                       FROM source_documents
                       WHERE deleted_at IS NULL AND canonical_url = ANY($1::text[])""",
                    [finding.canonical_url for finding in unique],
                )
            )
            processed_rows = await connection.fetch(
                """SELECT title, summary
                   FROM opportunities
                   WHERE subject_profile_id=$1 AND deleted_at IS NULL""",
                subject_id,
            )
            processed_signatures = tuple(
                _finding_signature(str(row["title"]), str(row["summary"] or ""))
                for row in processed_rows
            )
            new_unique = tuple(
                finding
                for finding in unique
                if finding.canonical_url not in previously_processed_urls
                and not _matches_processed_claim(finding, processed_signatures)
            )
            clusters = cluster_findings(new_unique)
            total_unique_domains = len({finding.canonical_url.split("/", 3)[2] for finding in new_unique})
            for cluster in clusters[:25]:
                representative = cluster.findings[0]
                score = _score_live_cluster(
                    cluster,
                    provenance,
                    processed_signatures,
                    topic=str(request.get("topic", "")),
                    subject_risk=str(request.get("risk", "medium")),
                    lookback_days=max(1, int((request.get("freshness_policy") or {}).get("lookback_days", 30))),
                    total_unique_domains=total_unique_domains,
                    weights=weights,
                    now=now,
                )
                opportunity_id = uuid4()
                await connection.execute(
                    """INSERT INTO opportunities
                    (id,version,created_at,updated_at,deleted_at,subject_profile_id,title,summary,
                     decision,manual,grouping_reason,duplicate_of_id,estimated_cost,decided_by,
                     decided_at,decision_reason)
                    VALUES($1,1,$2,$2,NULL,$3,$4,$5,'pending',false,$6::jsonb,NULL,$7::jsonb,NULL,NULL,NULL)""",
                    opportunity_id,
                    now,
                    subject_id,
                    representative.title[:300],
                    representative.summary or "Live discovery result awaiting editorial triage.",
                    json.dumps(
                        [
                            "classification: potential misinformation",
                            *cluster.grouping_reasons,
                            f"{len(cluster.findings)} deduplicated result(s)",
                            f"{len(cluster_domains)} source domain(s)",
                        ]
                    ),
                    json.dumps(
                        {
                            "currency_minor": 0,
                            "tokens": 0,
                            "gpu_seconds": 0,
                            "sources_to_acquire": len(cluster.findings),
                        }
                    ),
                )
                await connection.execute(
                    """INSERT INTO opportunity_scores
                    (id,opportunity_id,score_version,total,positive_components,penalties,weights,reasoning,created_at)
                    VALUES($1,$2,1,$3,$4::jsonb,$5::jsonb,$6::jsonb,$7::jsonb,$8)""",
                    uuid4(),
                    opportunity_id,
                    score.total,
                    json.dumps(dict(score.positive)),
                    json.dumps(dict(score.penalties)),
                    json.dumps(weights),
                    json.dumps(score.reasoning),
                    now,
                )
                for finding in cluster.findings:
                    item, strategy, rank = provenance[finding.canonical_url]
                    source_document_id = await connection.fetchval(
                        """INSERT INTO source_documents
                        (id,version,created_at,updated_at,deleted_at,canonical_url,title,author,
                         publisher,source_type,publication_at,event_at,reputation,domain)
                        VALUES($1,1,$2,$2,NULL,$3,$4,NULL,$5,$6,$7,NULL,$8::jsonb,$9)
                        ON CONFLICT (canonical_url) DO UPDATE SET
                          title=EXCLUDED.title, updated_at=EXCLUDED.updated_at,
                          version=source_documents.version+1
                        RETURNING id""",
                        uuid4(),
                        now,
                        finding.canonical_url,
                        finding.title,
                        urlsplit(finding.canonical_url).hostname or "",
                        finding.source_type,
                        _publication_at(finding.published_at),
                        json.dumps(
                            {
                                "discovered_via": "searxng",
                                "popularity_is_proof": False,
                                "not_yet_acquired": True,
                            }
                        ),
                        urlsplit(finding.canonical_url).hostname or "",
                    )
                    await connection.execute(
                        """INSERT INTO opportunity_sources
                        (id,opportunity_id,source_document_id,search_purpose,search_query,
                         result_rank,snippet,created_at)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                        ON CONFLICT (opportunity_id,source_document_id) DO NOTHING""",
                        uuid4(),
                        opportunity_id,
                        source_document_id,
                        str(strategy.get("purpose", "discovery"))[:80],
                        str(strategy.get("query", "")),
                        rank,
                        str(item.get("summary", "")),
                        now,
                    )
                opportunity_ids.append(str(opportunity_id))
            result = {
                "opportunity_ids": opportunity_ids,
                "search_strategy_count": len(result_sets),
                "raw_result_count": len(findings),
                "deduplicated_result_count": len(unique),
                "duplicate_count": len(findings) - len(unique),
                "previously_processed_count": len(unique) - len(new_unique),
                "new_candidate_count": len(new_unique),
                "source_domain_count": total_unique_domains,
            }
            await connection.execute(
                """INSERT INTO idempotency_records
                (id,scope,idempotency_key,request_hash,status,external_id,result,created_at,updated_at)
                VALUES($1,'research.live_discovery',$2,$3,'succeeded',$4,$5::jsonb,$6,$6)""",
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
