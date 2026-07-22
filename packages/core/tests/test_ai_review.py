import pytest

from editorial_core.ai_review import hybrid_qualification_score


BASE_POSITIVE = {
    "audience_fit": 50,
    "evidence_potential": 60,
    "novelty": 40,
    "timeliness": 55,
    "educational_value": 50,
    "visual_explainability": 50,
    "channel_differentiation": 50,
}
BASE_PENALTIES = {"risk": 10, "estimated_cost": 20, "duplication": 30}
AI = {
    "explainer_need": 90,
    "audience_relevance": 80,
    "video_suitability": 70,
    "channel_fit": 60,
    "angle_originality": 50,
}


def test_hybrid_score_keeps_hard_gate_dimensions_deterministic() -> None:
    result = hybrid_qualification_score(
        BASE_POSITIVE, BASE_PENALTIES, AI, confidence=0.9, abstained=False
    )
    assert result.applied is True
    assert result.score is not None
    assert result.score.positive["evidence_potential"] == 60
    assert result.score.positive["timeliness"] == 55
    assert result.score.penalties["duplication"] == 30
    assert result.score.positive["educational_value"] == 62


@pytest.mark.parametrize(
    ("confidence", "abstained", "reason"),
    [(0.64, False, "below threshold"), (0.99, True, "abstained")],
)
def test_hybrid_score_preserves_baseline_when_ai_is_uncertain(
    confidence: float, abstained: bool, reason: str
) -> None:
    result = hybrid_qualification_score(
        BASE_POSITIVE, BASE_PENALTIES, AI, confidence=confidence, abstained=abstained
    )
    assert result.score is None
    assert result.applied is False
    assert reason in result.reason
