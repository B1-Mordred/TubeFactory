from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4
from urllib.parse import urlsplit

import asyncpg
from editorial_core.operating_policy import evaluate_operating_policy
from temporalio import activity
from temporalio.exceptions import ApplicationError

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
from research_worker.acquisition import DomainPolicy, SearxngSearchResponse, search_searxng
from research_worker.config import Settings


_EXPLAINER_SIGNAL = re.compile(
    r"\b(?:warum|wieso|wie|erklär\w*|zusammenhang|ursach\w*|funktionier\w*|"
    r"studie|forsch\w*|wissenschaft\w*|technolog\w*|entwickl\w*|verfahren|"
    r"experiment\w*|analyse\w*|auswirkung\w*|wirkung\w*|mess\w*|entdeck\w*|beleg\w*|"
    r"konsens|why|how|explain\w*|study|research|evidence|impact)\b",
    re.IGNORECASE,
)
_MISINFORMATION_SIGNAL = re.compile(
    r"\b(?:desinformation|falsch(?:e|er|es|en)?|falschmeldung|fake(?:\s|-)?news|"
    r"irreführend\w*|widerleg\w*|faktencheck\w*|fact(?:\s|-)?check\w*|"
    r"behaupt\w*|gerücht\w*|hoax|correct(?:ed|ion)?|retract(?:ed|ion)?|"
    r"misleading|debunk\w*|false(?:hood)?)\b",
    re.IGNORECASE,
)
_GENERIC_PLATFORM_DOMAINS = {
    "facebook.com",
    "www.facebook.com",
    "instagram.com",
    "www.instagram.com",
    "tiktok.com",
    "www.tiktok.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
}
_NOISE_DOMAIN_SUFFIXES = (
    "adultforum.co",
    "duden.de",
    "dwds.de",
    "languagetool.org",
    "leo.org",
    "satzbeispiele.de",
    "scribbr.de",
    "wiktionary.org",
    "wikipedia.org",
    "wortbedeutung.info",
)
_NOISE_TITLE = re.compile(
    r"\b(?:rechtschreibung|schreibung|definition|bedeutung|etymologie|synonyme|"
    r"komma|wiktionary|wikipedia|mediathek|tatort|ip-adresse|kostenlose?\s+rechtschreib|"
    r"forum|adult|xnxx|testbericht|im\s+test|test\s+zeigt|preisvergleich|"
    r"kaufberatung|wartungskosten|service\s+kostet|wie\s+viel\s+geld)\b",
    re.IGNORECASE,
)
_MISINFORMATION_META_NOISE = re.compile(
    r"\b(?:was\s+ist\s+desinformation|desinformation\s+erkennen|zu\s+erkennen|"
    r"leitfaden|ratgeber|medienkompetenz|lexikon|glossar)\b",
    re.IGNORECASE,
)
_INSTITUTIONAL_ANNOUNCEMENT = re.compile(
    r"\b(?:wissenschafts?preis|forschungspreis|award|auszeichnung|preisverleihung|"
    r"workshop(?:-reihe)?|veranstaltungsreihe|unterrichtsmaterial(?:ien)?|"
    r"pressegespräch|kooperationsvereinbarung|förderbescheid)\b",
    re.IGNORECASE,
)
_DISCOVERY_STOPWORDS = frozenset(
    {
        "aber",
        "aktuell",
        "aktuelle",
        "aktueller",
        "aktuelles",
        "alle",
        "auch",
        "aus",
        "bei",
        "beim",
        "eine",
        "einem",
        "einen",
        "einer",
        "eines",
        "einfach",
        "erklärt",
        "erklaert",
        "erklären",
        "erklaeren",
        "für",
        "fuer",
        "gibt",
        "haben",
        "kann",
        "mit",
        "oder",
        "ohne",
        "sich",
        "sind",
        "thema",
        "themen",
        "tun",
        "und",
        "vom",
        "von",
        "warum",
        "was",
        "welche",
        "wie",
        "wieso",
        "wird",
        "werden",
        "zeit",
        "zum",
        "zur",
        "the",
        "and",
        "for",
        "how",
        "why",
        "what",
        "explain",
        "explained",
        "simple",
    }
)


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


def _source_domains(findings: tuple[SearchFinding, ...]) -> set[str]:
    return {
        hostname
        for finding in findings
        if (hostname := (urlsplit(finding.canonical_url).hostname or ""))
    }


def _hostname(value: str) -> str:
    try:
        return urlsplit(value).hostname or ""
    except ValueError:
        return ""


def _tokenize_relevance(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[\w-]{3,}", value.casefold())
        if token not in _DISCOVERY_STOPWORDS
    )


def _positive_query_text(strategy: dict[str, Any]) -> str:
    query = str(strategy.get("query", ""))
    query = re.sub(r'-"[^"]+"', " ", query)
    query = re.sub(r"\b(?:OR|AND|NOT)\b", " ", query, flags=re.IGNORECASE)
    return query


def _query_overlap(item: dict[str, Any], strategy: dict[str, Any] | None) -> int:
    if not strategy:
        return 1
    query_tokens = _tokenize_relevance(_positive_query_text(strategy))
    if not query_tokens:
        return 1
    content_tokens = _tokenize_relevance(
        f"{item.get('title', '')} {item.get('summary', '')}"
    )
    return len(query_tokens & content_tokens)


def _is_noisy_search_result(item: dict[str, Any]) -> bool:
    hostname = _hostname(str(item.get("url", ""))).casefold()
    if not hostname:
        return True
    if hostname in _GENERIC_PLATFORM_DOMAINS:
        return True
    if any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in _NOISE_DOMAIN_SUFFIXES):
        return True
    title = str(item.get("title", "")).strip()
    summary = str(item.get("summary", "")).strip()
    if not title or title.casefold() in {"youtube", "facebook", "instagram", "tiktok"}:
        return True
    return bool(_NOISE_TITLE.search(f"{title} {summary}"))


def _is_explainer_candidate(item: dict[str, Any], strategy: dict[str, Any] | None = None) -> bool:
    """Reject navigational/search noise for evidence-first explainer subjects."""

    if _is_noisy_search_result(item):
        return False
    if _query_overlap(item, strategy) < 1:
        return False
    title = str(item.get("title", "")).strip()
    summary = str(item.get("summary", "")).strip()
    if _INSTITUTIONAL_ANNOUNCEMENT.search(f"{title} {summary}"):
        return False
    return bool(_EXPLAINER_SIGNAL.search(f"{title} {summary}"))


def _is_misinformation_candidate(
    item: dict[str, Any], strategy: dict[str, Any] | None = None
) -> bool:
    """Reject generic web pages before they become misinformation opportunities."""

    if _is_noisy_search_result(item):
        return False
    if _query_overlap(item, strategy) < 1:
        return False
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    if _MISINFORMATION_META_NOISE.search(text):
        return False
    return bool(_MISINFORMATION_SIGNAL.search(text))


def _discovery_mode(format_policy: dict[str, Any]) -> str:
    return str(format_policy.get("discovery_mode", "auto")).strip().casefold()


def _profile_text(profile: dict[str, Any]) -> str:
    return json.dumps(profile, ensure_ascii=False, sort_keys=True).casefold()


def _uses_explainer_candidate_filter(profile: dict[str, Any]) -> bool:
    """Apply discovery hygiene to every explainer profile, not one legacy target key."""

    format_policy = profile.get("format_policy") if "format_policy" in profile else profile
    format_policy = format_policy if isinstance(format_policy, dict) else {}
    combined = _profile_text(profile)
    target = str(format_policy.get("target", "")).strip().casefold()
    editorial_format = str(
        (profile.get("editorial_profile") or {}).get("format", "")
        if isinstance(profile.get("editorial_profile"), dict)
        else ""
    ).casefold()
    return (
        target.endswith("explainer")
        or "explainer" in target
        or "explainer" in editorial_format
        or "erklaervideo" in editorial_format
        or "erklärvideo" in editorial_format
        or "erklaervideo" in combined
        or "erklärvideo" in combined
    )


def _uses_misinformation_candidate_filter(profile: dict[str, Any]) -> bool:
    approval_profile = profile.get("approval_profile") or {}
    sensitive_topics = approval_profile.get("sensitive_topics", [])
    if "misinformation_fact_checking" in sensitive_topics:
        return True
    combined = _profile_text(profile)
    return any(
        signal in combined
        for signal in ("misinformation", "falschinformation", "desinformation", "faktencheck")
    )


def _candidate_filter_kind(profile: dict[str, Any]) -> str:
    format_policy = profile.get("format_policy") or {}
    if _discovery_mode(format_policy) == "manual_only":
        return "disabled"
    if _uses_misinformation_candidate_filter(profile):
        return "misinformation"
    if _uses_explainer_candidate_filter(profile):
        return "explainer"
    return "general"


def _discovery_strategies(plan: Any, format_policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep source verification after topic selection when the workflow requests it."""

    defer_evidence = (
        str(format_policy.get("evidence_research_timing", "")).strip().casefold()
        == "after_topic_selection"
    )
    strategies = [
        {
            "purpose": item.purpose,
            "query": item.query,
            "language": item.language,
            "region": item.region,
        }
        for item in plan.strategies
        if not defer_evidence or item.purpose == "broad discovery"
    ]
    if not defer_evidence:
        strategies.extend(
            {
                "purpose": "falsification",
                "query": query,
                "language": plan.strategies[0].language,
                "region": plan.strategies[0].region,
            }
            for query in plan.falsification_queries
        )
    return strategies


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
    alignment_penalty = (
        28
        if topic_coverage < 0.08 and query_coverage < 0.08
        else 14 if query_coverage < 0.08 else 0
    )

    positive = {
        "audience_fit": _clamped_score(18 + topic_coverage * 38 + query_coverage * 24 + rank_quality * 15),
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
        "risk": _clamped_score(
            risk_base + alignment_penalty + (6 if published_dates and timeliness >= 85 else 0)
        ),
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
        f"Risk penalty {penalties['risk']}: subject risk is {subject_risk}; alignment penalty {alignment_penalty}; very recent material adds review pressure when applicable.",
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
                      domain_policy, opportunity_weights, freshness_policy, format_policy,
                      editorial_profile, approval_profile, risk
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
        format_policy = _json(subject["format_policy"]) or {}
        editorial_profile = _json(subject["editorial_profile"]) or {}
        approval_profile = _json(subject["approval_profile"]) or {}
        policy_snapshot = evaluate_operating_policy(
            mode=str(approval_profile.get("mode", "assisted")),
            risk=str(subject["risk"]),
            sensitive_topics=approval_profile.get("sensitive_topics", []),
        ).as_dict()
        profile_context = {
            "topic": subject["topic"],
            "research_goal": subject["research_goal"],
            "format_policy": format_policy,
            "editorial_profile": editorial_profile,
            "approval_profile": approval_profile,
        }
        discovery_disabled = _candidate_filter_kind(profile_context) == "disabled"
        strategies = [] if discovery_disabled else _discovery_strategies(plan, format_policy)
        return {
            "subject_profile_id": str(subject["id"]),
            "topic": subject["topic"],
            "strategies": strategies[:12],
            "domain_policy": _json(subject["domain_policy"]) or {"allow": [], "block": []},
            "opportunity_weights": _json(subject["opportunity_weights"]) or {},
            "freshness_policy": _json(subject["freshness_policy"]) or {"lookback_days": 30},
            "format_policy": format_policy,
            "editorial_profile": editorial_profile,
            "approval_profile": approval_profile,
            "policy_snapshot": policy_snapshot,
            "discovery_disabled": discovery_disabled,
            "risk": subject["risk"],
        }
    finally:
        await connection.close()


@activity.defn(name="search-live-strategy")
async def search_live_strategy(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    strategy = request["strategy"]
    purpose = str(strategy.get("purpose", "broad discovery"))
    engine_setting = (
        settings.searxng_science_engines
        if purpose in {"primary evidence", "falsification"}
        else settings.searxng_general_engines
    )
    engines = tuple(item.strip() for item in engine_setting.split(",") if item.strip())
    configured_lookback = max(
        1, int((request.get("freshness_policy") or {}).get("lookback_days", 30))
    )
    recent_lookback = configured_lookback if purpose == "broad discovery" else None
    attempts: list[SearxngSearchResponse] = []
    first = await search_searxng(
        settings.searxng_endpoint,
        query=str(strategy["query"])[:2000],
        language=str(strategy["language"])[:20],
        policy=DomainPolicy.from_mapping(request.get("domain_policy")),
        lookback_days=recent_lookback,
        engines=engines,
    )
    attempts.append(first)
    selected = first
    fallback_used = False
    if not first.results and recent_lookback is not None:
        # A time_range is useful for the news radar but inconsistently supported by
        # metasearch engines. Retry once without it before declaring the strategy empty.
        await asyncio.sleep(1.5)
        selected = await search_searxng(
            settings.searxng_endpoint,
            query=str(strategy["query"])[:2000],
            language=str(strategy["language"])[:20],
            policy=DomainPolicy.from_mapping(request.get("domain_policy")),
            lookback_days=None,
            engines=engines,
        )
        attempts.append(selected)
        fallback_used = True

    responding_engines = sorted(
        {engine for attempt in attempts for engine in attempt.responding_engines}
    )
    failures = {
        (failure["engine"], failure["reason"])
        for attempt in attempts
        for failure in attempt.unresponsive_engines
    }
    health = {
        "status": "degraded" if failures or fallback_used else "healthy",
        "requested_engines": list(engines),
        "responding_engines": responding_engines,
        "unresponsive_engines": [
            {"engine": engine, "reason": reason} for engine, reason in sorted(failures)
        ],
        "freshness_mode": "recent_then_evergreen" if recent_lookback is not None else "evergreen",
        "time_range": first.time_range,
        "fallback_used": fallback_used,
    }
    if not selected.results and selected.unresponsive_engines:
        raise ApplicationError(
            "Metasearch sources were unavailable; an empty result would be unreliable",
            health,
            type="SearchBackendUnavailable",
        )
    return {"strategy": strategy, "results": list(selected.results), "health": health}


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
    profile_context = {
        "topic": request.get("topic", ""),
        "research_goal": request.get("research_goal", ""),
        "format_policy": request.get("format_policy") or {},
        "editorial_profile": request.get("editorial_profile") or {},
        "approval_profile": request.get("approval_profile") or {},
    }
    filter_kind = _candidate_filter_kind(profile_context)
    if filter_kind == "explainer":
        entries = [entry for entry in entries if _is_explainer_candidate(entry[0], entry[1])]
    elif filter_kind == "misinformation":
        entries = [entry for entry in entries if _is_misinformation_candidate(entry[0], entry[1])]
    elif filter_kind == "disabled":
        entries = []
    eligible_findings = [item for item, _, _ in entries]
    findings = tuple(
        SearchFinding(
            url=str(item["url"]),
            title=str(item["title"]),
            summary=str(item.get("summary", "")),
            published_at=item.get("published_at"),
            source_type=str(item.get("source_type", "secondary")),
        )
        for item in eligible_findings
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
            policy_snapshot = request.get("policy_snapshot") or {}
            previously_processed_urls = set(
                await connection.fetchval(
                    """SELECT coalesce(array_agg(DISTINCT sd.canonical_url), ARRAY[]::text[])
                       FROM source_documents sd
                       JOIN opportunity_sources os ON os.source_document_id=sd.id
                       JOIN opportunities o ON o.id=os.opportunity_id
                       WHERE sd.deleted_at IS NULL AND o.deleted_at IS NULL
                         AND o.subject_profile_id=$2 AND sd.canonical_url = ANY($1::text[])""",
                    [finding.canonical_url for finding in unique],
                    subject_id,
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
                cluster_domains = _source_domains(cluster.findings)
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
                     editorial_rationale,policy_snapshot,decision,manual,grouping_reason,
                     duplicate_of_id,estimated_cost,decided_by,decided_at,decision_reason)
                    VALUES($1,1,$2,$2,NULL,$3,$4,$5,'',$6::jsonb,'pending',false,$7::jsonb,
                           NULL,$8::jsonb,NULL,NULL,NULL)""",
                    opportunity_id,
                    now,
                    subject_id,
                    representative.title[:300],
                    representative.summary or "Live discovery result awaiting editorial triage.",
                    json.dumps(policy_snapshot),
                    json.dumps(
                        [
                            f"classification: {filter_kind} candidate",
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
            source_health = [
                result_set.get("health", {})
                for result_set in result_sets
                if isinstance(result_set.get("health"), dict)
            ]
            engine_failures = {
                (str(failure.get("engine", "")), str(failure.get("reason", "")))
                for health in source_health
                for failure in health.get("unresponsive_engines", [])
                if isinstance(failure, dict) and str(failure.get("engine", "")).strip()
            }
            result = {
                "opportunity_ids": opportunity_ids,
                "search_strategy_count": len(result_sets),
                "candidate_filter": filter_kind,
                "discovery_disabled": bool(request.get("discovery_disabled")),
                "raw_result_count": len(raw_findings),
                "eligible_result_count": len(findings),
                "relevance_rejected_count": len(raw_findings) - len(findings),
                "deduplicated_result_count": len(unique),
                "duplicate_count": len(findings) - len(unique),
                "previously_processed_count": len(unique) - len(new_unique),
                "new_candidate_count": len(new_unique),
                "source_domain_count": total_unique_domains,
                "search_health": (
                    "degraded"
                    if any(health.get("status") == "degraded" for health in source_health)
                    else "healthy"
                ),
                "responding_engines": sorted(
                    {
                        str(engine)
                        for health in source_health
                        for engine in health.get("responding_engines", [])
                        if str(engine).strip()
                    }
                ),
                "unresponsive_engines": [
                    {"engine": engine, "reason": reason}
                    for engine, reason in sorted(engine_failures)
                ],
                "degraded_strategy_count": sum(
                    health.get("status") == "degraded" for health in source_health
                ),
                "fallback_strategy_count": sum(
                    bool(health.get("fallback_used")) for health in source_health
                ),
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
