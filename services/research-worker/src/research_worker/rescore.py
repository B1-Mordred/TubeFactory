from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import asyncpg

from editorial_core.discovery import FindingCluster, OpportunityScore, SearchFinding
from research_worker.config import Settings
from research_worker.live_activities import _finding_signature, _score_live_cluster


_PLACEHOLDER_POSITIVE = {
    "audience_fit": 60,
    "novelty": 55,
    "timeliness": 60,
    "educational_value": 65,
    "visual_explainability": 50,
    "channel_differentiation": 50,
}


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _is_placeholder_score(positive: dict[str, Any], penalties: dict[str, Any]) -> bool:
    return all(float(positive.get(name, -1)) == expected for name, expected in _PLACEHOLDER_POSITIVE.items()) and float(
        penalties.get("risk", -1)
    ) == 25 and float(penalties.get("duplication", -1)) == 0


async def rescore_placeholder_opportunities() -> int:
    """Append a signal-derived score version for legacy live-discovery placeholders."""

    connection = await asyncpg.connect(Settings().database_dsn)
    try:
        # Serialize candidate selection as well as insertion so concurrent operators
        # cannot both report the same immutable upgrade as newly applied.
        await connection.execute("SELECT pg_advisory_lock(hashtext('research.rescore.placeholder-v2'))")
        rows = await connection.fetch(
            """SELECT o.id, o.subject_profile_id, o.title, o.summary, o.grouping_reason,
                      sp.topic, sp.risk, sp.freshness_policy, sp.opportunity_weights,
                      score.score_version, score.positive_components, score.penalties
               FROM opportunities o
               JOIN subject_profiles sp ON sp.id=o.subject_profile_id
               JOIN LATERAL (
                   SELECT score_version, positive_components, penalties
                   FROM opportunity_scores
                   WHERE opportunity_id=o.id
                   ORDER BY score_version DESC
                   LIMIT 1
               ) score ON true
               WHERE o.deleted_at IS NULL AND o.manual=false"""
        )
        candidates = [
            row
            for row in rows
            if _is_placeholder_score(_json(row["positive_components"]), _json(row["penalties"]))
        ]
        if not candidates:
            return 0
        all_opportunities = await connection.fetch(
            """SELECT id, subject_profile_id, title, summary
               FROM opportunities
               WHERE deleted_at IS NULL"""
        )
        signatures_by_subject: dict[Any, list[tuple[Any, tuple[str, frozenset[str]]]]] = {}
        for item in all_opportunities:
            signatures_by_subject.setdefault(item["subject_profile_id"], []).append(
                (item["id"], _finding_signature(str(item["title"]), str(item["summary"] or "")))
            )
        source_rows = await connection.fetch(
            """SELECT os.opportunity_id, os.search_purpose, os.search_query, os.result_rank,
                      os.snippet, sd.canonical_url, sd.title, sd.publication_at, sd.source_type, sd.domain
               FROM opportunity_sources os
               JOIN source_documents sd ON sd.id=os.source_document_id
               WHERE os.opportunity_id=ANY($1::uuid[])
               ORDER BY os.opportunity_id, os.result_rank, sd.canonical_url""",
            [row["id"] for row in candidates],
        )
        sources_by_opportunity: dict[Any, list[Any]] = {}
        domains_by_subject: dict[Any, set[str]] = {}
        subject_by_opportunity = {row["id"]: row["subject_profile_id"] for row in candidates}
        for source in source_rows:
            sources_by_opportunity.setdefault(source["opportunity_id"], []).append(source)
            subject_id = subject_by_opportunity[source["opportunity_id"]]
            domains_by_subject.setdefault(subject_id, set()).add(str(source["domain"]))

        rescored = 0
        now = datetime.now(timezone.utc)
        async with connection.transaction():
            for row in candidates:
                sources = sources_by_opportunity.get(row["id"], [])
                source_provenance_available = bool(sources)
                if source_provenance_available:
                    findings = tuple(
                        SearchFinding(
                            url=str(source["canonical_url"]),
                            title=str(source["title"]),
                            summary=str(source["snippet"] or row["summary"] or ""),
                            published_at=source["publication_at"].isoformat() if source["publication_at"] else None,
                            source_type=str(source["source_type"]),
                        )
                        for source in sources
                    )
                    provenance = {
                        finding.canonical_url: (
                            {},
                            {"purpose": str(source["search_purpose"]), "query": str(source["search_query"])},
                            int(source["result_rank"]),
                        )
                        for finding, source in zip(findings, sources, strict=True)
                    }
                else:
                    findings = (
                        SearchFinding(
                            url=f"https://unavailable.invalid/opportunity/{row['id']}",
                            title=str(row["title"]),
                            summary=str(row["summary"] or ""),
                            source_type="unknown",
                        ),
                    )
                    provenance = {}
                prior_signatures = tuple(
                    signature
                    for opportunity_id, signature in signatures_by_subject.get(row["subject_profile_id"], [])
                    if opportunity_id != row["id"]
                )
                freshness = _json(row["freshness_policy"]) or {}
                score = _score_live_cluster(
                    FindingCluster(findings, tuple(_json(row["grouping_reason"]) or ())),
                    provenance,
                    prior_signatures,
                    topic=str(row["topic"]),
                    subject_risk=str(row["risk"]),
                    lookback_days=max(1, int(freshness.get("lookback_days", 30))),
                    total_unique_domains=len(domains_by_subject.get(row["subject_profile_id"], set())),
                    weights=_json(row["opportunity_weights"]) or None,
                    now=now,
                    source_provenance_available=source_provenance_available,
                )
                provenance_reason = (
                    "Score version appended to replace the legacy constant live-discovery trace; the original score remains immutable."
                    if source_provenance_available
                    else "No stored source provenance was available; this replacement uses only the stored title, summary and subject signals with explicit zero-source inputs."
                )
                score = OpportunityScore(
                    score.total,
                    score.positive,
                    score.penalties,
                    (
                        provenance_reason,
                        *score.reasoning,
                    ),
                )
                result = await connection.execute(
                    """INSERT INTO opportunity_scores
                       (id,opportunity_id,score_version,total,positive_components,penalties,weights,reasoning,created_at)
                       VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8::jsonb,$9)
                       ON CONFLICT (opportunity_id,score_version) DO NOTHING""",
                    uuid4(),
                    row["id"],
                    int(row["score_version"]) + 1,
                    score.total,
                    json.dumps(dict(score.positive)),
                    json.dumps(dict(score.penalties)),
                    json.dumps(_json(row["opportunity_weights"]) or {}),
                    json.dumps(score.reasoning),
                    now,
                )
                if result == "INSERT 0 1":
                    rescored += 1
        return rescored
    finally:
        await connection.close()


async def _main() -> None:
    count = await rescore_placeholder_opportunities()
    print(f"rescored_placeholder_opportunities={count}")


if __name__ == "__main__":
    asyncio.run(_main())
