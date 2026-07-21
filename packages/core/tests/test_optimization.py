from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from editorial_core.optimization import (
    BenchmarkCandidate,
    GateVerdict,
    OptimizationPolicyError,
    correction_action,
    enforce_budget,
    evaluate_freshness,
    evaluate_originality,
    recommend_model,
)


def test_benchmark_recommendation_is_deterministic_and_excludes_ineligible_model() -> None:
    candidates = [
        BenchmarkCandidate("slow-good", 96, 1.0, 2_000, Decimal("0.04")),
        BenchmarkCandidate("fast-good", 94, 1.0, 500, Decimal("0.01")),
        BenchmarkCandidate("forbidden", 100, 1.0, 100, Decimal("0"), policy_eligible=False),
    ]
    result = recommend_model(candidates)
    assert result.model_id == "fast-good"
    assert result.fallback_model_ids == ("slow-good",)
    assert result.reasoning[-1] == "recommendation_only_no_automatic_routing_change"


def test_benchmark_rejects_invalid_weights_and_no_eligible_candidate() -> None:
    candidate = BenchmarkCandidate("m", 90, 1, 100, Decimal("0"), policy_eligible=False)
    with pytest.raises(OptimizationPolicyError, match="weights must sum"):
        recommend_model([candidate], quality_weight=1)
    with pytest.raises(OptimizationPolicyError, match="no policy-eligible"):
        recommend_model([candidate])


def test_freshness_blocks_stale_source() -> None:
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    result = evaluate_freshness(
        retrieved_at=now - timedelta(days=31), maximum_age=timedelta(days=30), now=now
    )
    assert result.verdict == GateVerdict.BLOCK
    assert result.reason == "source_snapshot_stale_reacquisition_required"


def test_originality_has_review_and_block_gates() -> None:
    assert evaluate_originality({"a": 0.1}).verdict == GateVerdict.PASS
    assert evaluate_originality({"a": 0.25}).verdict == GateVerdict.REVIEW
    assert evaluate_originality({"a": 0.5}).verdict == GateVerdict.BLOCK


def test_budget_is_fail_closed_and_correction_action_depends_on_release_state() -> None:
    allowed = enforce_budget(limit=Decimal("10"), spent=Decimal("8"), proposed=Decimal("2"))
    blocked = enforce_budget(limit=Decimal("10"), spent=Decimal("8"), proposed=Decimal("2.01"))
    assert allowed.allowed is True and allowed.remaining == Decimal("2")
    assert blocked.allowed is False and blocked.reason == "budget_exceeded"
    assert correction_action("critical", published=True) == "unpublish_and_escalate"
    assert correction_action("high", published=False) == "block_release"
