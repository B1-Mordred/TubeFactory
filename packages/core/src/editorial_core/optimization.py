from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Sequence


class OptimizationPolicyError(ValueError):
    pass


class GateVerdict(StrEnum):
    PASS = "pass"
    REVIEW = "review"
    BLOCK = "block"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class BenchmarkCandidate:
    model_id: str
    quality_score: float
    success_rate: float
    p95_latency_ms: int
    mean_cost_usd: Decimal
    policy_eligible: bool = True


@dataclass(frozen=True)
class BenchmarkRecommendation:
    model_id: str
    fallback_model_ids: tuple[str, ...]
    score: float
    reasoning: tuple[str, ...]


def recommend_model(
    candidates: Sequence[BenchmarkCandidate],
    *,
    quality_weight: float = 0.65,
    reliability_weight: float = 0.20,
    latency_weight: float = 0.10,
    cost_weight: float = 0.05,
) -> BenchmarkRecommendation:
    """Rank eligible candidates deterministically; this function never changes routing."""
    if not candidates:
        raise OptimizationPolicyError("at least one benchmark candidate is required")
    if any(weight < 0 for weight in (quality_weight, reliability_weight, latency_weight, cost_weight)):
        raise OptimizationPolicyError("benchmark weights cannot be negative")
    if abs(quality_weight + reliability_weight + latency_weight + cost_weight - 1.0) > 0.000001:
        raise OptimizationPolicyError("benchmark weights must sum to one")
    eligible = [candidate for candidate in candidates if candidate.policy_eligible]
    if not eligible:
        raise OptimizationPolicyError("no policy-eligible benchmark candidate exists")
    for candidate in candidates:
        if not candidate.model_id:
            raise OptimizationPolicyError("candidate model ID is required")
        if not 0 <= candidate.quality_score <= 100 or not 0 <= candidate.success_rate <= 1:
            raise OptimizationPolicyError("quality must be 0..100 and success_rate must be 0..1")
        if candidate.p95_latency_ms <= 0 or candidate.mean_cost_usd < 0:
            raise OptimizationPolicyError("latency must be positive and cost cannot be negative")
    maximum_latency = max(item.p95_latency_ms for item in eligible)
    maximum_cost = max((item.mean_cost_usd for item in eligible), default=Decimal("0"))

    def score(candidate: BenchmarkCandidate) -> float:
        latency_score = 1 - ((candidate.p95_latency_ms - 1) / maximum_latency)
        cost_score = 1.0 if maximum_cost == 0 else float(1 - candidate.mean_cost_usd / maximum_cost)
        return round(
            quality_weight * (candidate.quality_score / 100)
            + reliability_weight * candidate.success_rate
            + latency_weight * latency_score
            + cost_weight * cost_score,
            8,
        )

    ranked = sorted(eligible, key=lambda item: (-score(item), item.model_id))
    winner = ranked[0]
    return BenchmarkRecommendation(
        model_id=winner.model_id,
        fallback_model_ids=tuple(item.model_id for item in ranked[1:6]),
        score=score(winner),
        reasoning=(
            f"quality={winner.quality_score:.2f}",
            f"success_rate={winner.success_rate:.4f}",
            f"p95_latency_ms={winner.p95_latency_ms}",
            f"mean_cost_usd={winner.mean_cost_usd}",
            "recommendation_only_no_automatic_routing_change",
        ),
    )


@dataclass(frozen=True)
class FreshnessResult:
    verdict: GateVerdict
    age_seconds: int
    maximum_age_seconds: int
    reason: str


def evaluate_freshness(
    *, retrieved_at: datetime, maximum_age: timedelta, now: datetime | None = None
) -> FreshnessResult:
    if retrieved_at.tzinfo is None or maximum_age <= timedelta(0):
        raise OptimizationPolicyError("freshness timestamps need a timezone and maximum age must be positive")
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = max(timedelta(0), reference - retrieved_at.astimezone(timezone.utc))
    verdict = GateVerdict.PASS if age <= maximum_age else GateVerdict.BLOCK
    return FreshnessResult(
        verdict=verdict,
        age_seconds=int(age.total_seconds()),
        maximum_age_seconds=int(maximum_age.total_seconds()),
        reason="source_within_freshness_window" if verdict == GateVerdict.PASS else "source_snapshot_stale_reacquisition_required",
    )


@dataclass(frozen=True)
class OriginalityResult:
    verdict: GateVerdict
    maximum_overlap: float
    reason: str


def evaluate_originality(
    overlaps: Mapping[str, float], *, review_threshold: float = 0.20, block_threshold: float = 0.35
) -> OriginalityResult:
    if not 0 <= review_threshold < block_threshold <= 1:
        raise OptimizationPolicyError("originality thresholds must satisfy 0 <= review < block <= 1")
    if any(not source or not 0 <= value <= 1 for source, value in overlaps.items()):
        raise OptimizationPolicyError("overlap values must map a source to a value from zero to one")
    maximum = max(overlaps.values(), default=0.0)
    if maximum >= block_threshold:
        return OriginalityResult(GateVerdict.BLOCK, maximum, "overlap_exceeds_block_threshold")
    if maximum >= review_threshold:
        return OriginalityResult(GateVerdict.REVIEW, maximum, "overlap_requires_human_review")
    return OriginalityResult(GateVerdict.PASS, maximum, "overlap_below_review_threshold")


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    remaining: Decimal
    reason: str


def enforce_budget(*, limit: Decimal, spent: Decimal, proposed: Decimal) -> BudgetDecision:
    if limit < 0 or spent < 0 or proposed < 0:
        raise OptimizationPolicyError("budget values cannot be negative")
    remaining = max(Decimal("0"), limit - spent)
    return BudgetDecision(
        allowed=proposed <= remaining,
        remaining=remaining,
        reason="within_budget" if proposed <= remaining else "budget_exceeded",
    )


def correction_action(severity: str, *, published: bool) -> str:
    normalized = severity.strip().lower()
    if normalized not in {"low", "medium", "high", "critical"}:
        raise OptimizationPolicyError("correction severity must be low, medium, high, or critical")
    if normalized == "critical":
        return "unpublish_and_escalate" if published else "block_release"
    if normalized == "high":
        return "publish_correction_and_review" if published else "block_release"
    if normalized == "medium":
        return "add_correction_notice" if published else "require_review"
    return "record_erratum" if published else "record_before_release"
