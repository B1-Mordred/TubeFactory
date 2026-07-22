from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OpportunityDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class SubjectBrief:
    topic: str
    research_goal: str
    seed_queries: tuple[str, ...]
    related_concepts: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()
    languages: tuple[str, ...] = ("en",)
    regions: tuple[str, ...] = ()
    expected_primary_source_types: tuple[str, ...] = ("official record", "original study")

    def __post_init__(self) -> None:
        if not self.topic.strip() or not self.research_goal.strip():
            raise ValueError("topic and research_goal are required")
        if not self.seed_queries:
            raise ValueError("at least one seed query is required")


@dataclass(frozen=True)
class SearchStrategy:
    purpose: str
    query: str
    language: str
    region: str | None = None


@dataclass(frozen=True)
class SearchPlan:
    subject_topic: str
    strategies: tuple[SearchStrategy, ...]
    expected_primary_source_types: tuple[str, ...]
    falsification_queries: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.strategies) < 3:
            raise ValueError("a search plan requires at least three strategies")
        if not self.falsification_queries:
            raise ValueError("a search plan requires a falsification query")


def plan_search(subject: SubjectBrief) -> SearchPlan:
    language = subject.languages[0]
    region = subject.regions[0] if subject.regions else None
    negative = " ".join(f'-"{term.strip()}"' for term in subject.negative_keywords if term.strip())
    seeds = tuple(dict.fromkeys(query.strip() for query in subject.seed_queries if query.strip()))
    strategies = [
        SearchStrategy("broad discovery", f"{query} {negative}".strip(), language, region)
        for query in seeds
    ]
    focus_query = seeds[0]
    if language.casefold().startswith("de"):
        primary_terms = "(Studie OR Bericht OR Datensatz OR Forschungsbericht)"
        falsification_terms = "(Kritik OR Korrektur OR widerlegt OR zurückgezogen OR Gegenbeleg)"
    else:
        primary_terms = "(report OR dataset OR filing OR study)"
        falsification_terms = "(false OR correction OR retracted OR criticism OR counterevidence)"
    strategies.append(
        SearchStrategy(
            "primary evidence",
            f"{focus_query} {primary_terms} {negative}".strip(),
            language,
            region,
        )
    )
    strategies.append(
        SearchStrategy(
            "related concepts",
            " ".join((focus_query, *subject.related_concepts, negative)).strip(),
            language,
            region,
        )
    )
    falsification = (
        f"{focus_query} {falsification_terms}",
    )
    return SearchPlan(
        subject_topic=subject.topic,
        strategies=tuple(strategies),
        expected_primary_source_types=subject.expected_primary_source_types,
        falsification_queries=falsification,
    )


_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}


def canonicalize_url(raw_url: str) -> str:
    parts = urlsplit(raw_url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("only absolute HTTP(S) URLs can be canonicalized")
    if parts.username or parts.password:
        raise ValueError("URL credentials are not allowed")
    hostname = parts.hostname.rstrip(".").lower()
    port = parts.port
    netloc = hostname
    if port and not ((parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMETERS
        )
    )
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


@dataclass(frozen=True)
class SearchFinding:
    url: str
    title: str
    summary: str
    published_at: str | None = None
    source_type: str = "secondary"
    content_hash: str | None = None

    @property
    def canonical_url(self) -> str:
        return canonicalize_url(self.url)


def deduplicate_findings(findings: Iterable[SearchFinding]) -> tuple[SearchFinding, ...]:
    unique: list[SearchFinding] = []
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    for finding in findings:
        canonical = finding.canonical_url
        if canonical in seen_urls:
            continue
        if finding.content_hash and finding.content_hash in seen_hashes:
            continue
        seen_urls.add(canonical)
        if finding.content_hash:
            seen_hashes.add(finding.content_hash)
        unique.append(finding)
    return tuple(unique)


SCORE_DIMENSIONS = (
    "audience_fit",
    "evidence_potential",
    "novelty",
    "timeliness",
    "educational_value",
    "visual_explainability",
    "channel_differentiation",
)
PENALTY_DIMENSIONS = ("risk", "estimated_cost", "duplication")


@dataclass(frozen=True)
class OpportunityScore:
    total: int
    positive: Mapping[str, float]
    penalties: Mapping[str, float]
    reasoning: tuple[str, ...]


def score_opportunity(
    positive: Mapping[str, float],
    penalties: Mapping[str, float],
    *,
    weights: Mapping[str, float] | None = None,
) -> OpportunityScore:
    unknown = (set(positive) - set(SCORE_DIMENSIONS)) | (set(penalties) - set(PENALTY_DIMENSIONS))
    if unknown:
        raise ValueError(f"unknown score dimensions: {', '.join(sorted(unknown))}")
    values = {**positive, **penalties}
    if any(not math.isfinite(value) or value < 0 or value > 100 for value in values.values()):
        raise ValueError("score components must be finite values from 0 through 100")
    resolved_weights = {dimension: 1.0 for dimension in (*SCORE_DIMENSIONS, *PENALTY_DIMENSIONS)}
    resolved_weights.update(weights or {})
    positive_weight = sum(resolved_weights[name] for name in SCORE_DIMENSIONS)
    weighted_positive = sum(positive.get(name, 0) * resolved_weights[name] for name in SCORE_DIMENSIONS)
    weighted_penalty = sum(
        penalties.get(name, 0) * resolved_weights[name] for name in PENALTY_DIMENSIONS
    ) / max(sum(resolved_weights[name] for name in PENALTY_DIMENSIONS), 1)
    raw = weighted_positive / max(positive_weight, 1) - weighted_penalty * 0.35
    total = round(max(0, min(100, raw)))
    reasoning = tuple(
        [f"{name} contributed {positive.get(name, 0):g}/100" for name in SCORE_DIMENSIONS]
        + [f"{name} penalty was {penalties.get(name, 0):g}/100" for name in PENALTY_DIMENSIONS]
    )
    return OpportunityScore(total, dict(positive), dict(penalties), reasoning)


def _tokens(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[\w-]{3,}", value.casefold()))


@dataclass(frozen=True)
class FindingCluster:
    findings: tuple[SearchFinding, ...]
    grouping_reasons: tuple[str, ...]


def cluster_findings(
    findings: Sequence[SearchFinding], *, similarity_threshold: float = 0.42
) -> tuple[FindingCluster, ...]:
    unique = deduplicate_findings(findings)
    clusters: list[list[SearchFinding]] = []
    reasons: list[list[str]] = []
    for finding in unique:
        candidate_tokens = _tokens(f"{finding.title} {finding.summary}")
        selected: int | None = None
        selected_similarity = 0.0
        for index, cluster in enumerate(clusters):
            representative = cluster[0]
            existing_tokens = _tokens(f"{representative.title} {representative.summary}")
            union = candidate_tokens | existing_tokens
            similarity = len(candidate_tokens & existing_tokens) / len(union) if union else 0.0
            if similarity >= similarity_threshold and similarity > selected_similarity:
                selected, selected_similarity = index, similarity
        if selected is None:
            clusters.append([finding])
            reasons.append(["first distinct finding in cluster"])
        else:
            clusters[selected].append(finding)
            reasons[selected].append(f"token similarity {selected_similarity:.2f}")
    return tuple(
        FindingCluster(tuple(cluster), tuple(cluster_reasons))
        for cluster, cluster_reasons in zip(clusters, reasons, strict=True)
    )


class ClaimType(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    OPINION = "opinion"


class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"


@dataclass(frozen=True)
class EvidenceLink:
    excerpt_id: str
    source_id: str
    relation: EvidenceRelation
    independent: bool
    primary: bool
    direct: bool


@dataclass(frozen=True)
class AtomicClaim:
    claim_id: str
    statement: str
    claim_type: ClaimType
    central: bool
    risk: RiskLevel
    evidence: tuple[EvidenceLink, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CompletionResult:
    complete: bool
    blockers: tuple[str, ...]


def evaluate_research_completion(
    claims: Iterable[AtomicClaim],
    *,
    minimum_independent_sources: int = 2,
    require_primary_for_high_risk: bool = True,
) -> CompletionResult:
    blockers: list[str] = []
    for claim in claims:
        if not claim.central:
            continue
        supporting = [item for item in claim.evidence if item.relation is EvidenceRelation.SUPPORTS]
        independent_sources = {item.source_id for item in supporting if item.independent}
        if len(independent_sources) < minimum_independent_sources:
            blockers.append(
                f"claim {claim.claim_id} has {len(independent_sources)} independent supporting source(s); "
                f"requires {minimum_independent_sources}"
            )
        if (
            require_primary_for_high_risk
            and claim.risk is RiskLevel.HIGH
            and not any(item.primary and item.direct for item in supporting)
        ):
            blockers.append(f"high-risk claim {claim.claim_id} lacks direct primary evidence")
    return CompletionResult(not blockers, tuple(blockers))


def stable_document_hash(content: str | bytes) -> str:
    body = content.encode() if isinstance(content, str) else content
    return hashlib.sha256(body).hexdigest()
