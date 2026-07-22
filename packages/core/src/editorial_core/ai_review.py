from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from editorial_core.discovery import OpportunityScore, score_opportunity


AI_QUALIFICATION_DIMENSIONS = (
    "explainer_need",
    "audience_relevance",
    "video_suitability",
    "channel_fit",
    "angle_originality",
)


@dataclass(frozen=True)
class HybridQualificationResult:
    score: OpportunityScore | None
    applied: bool
    reason: str


def hybrid_qualification_score(
    deterministic_positive: Mapping[str, float],
    deterministic_penalties: Mapping[str, float],
    ai_dimensions: Mapping[str, Any],
    *,
    confidence: float,
    abstained: bool,
    weights: Mapping[str, float] | None = None,
    minimum_confidence: float = 0.65,
) -> HybridQualificationResult:
    """Blend bounded advisory factors while retaining deterministic hard gates.

    The model never supplies a total. Evidence potential, timeliness and every
    penalty remain untouched; semantic duplication therefore cannot be waived
    by a model response.
    """

    if abstained:
        return HybridQualificationResult(None, False, "AI qualifier abstained")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be between zero and one")
    if confidence < minimum_confidence:
        return HybridQualificationResult(None, False, "AI confidence below threshold")
    missing = set(AI_QUALIFICATION_DIMENSIONS) - set(ai_dimensions)
    if missing:
        raise ValueError(f"missing AI qualification dimensions: {', '.join(sorted(missing))}")
    values = {name: float(ai_dimensions[name]) for name in AI_QUALIFICATION_DIMENSIONS}
    if any(not math.isfinite(value) or value < 0 or value > 100 for value in values.values()):
        raise ValueError("AI qualification dimensions must be between zero and 100")

    positive = dict(deterministic_positive)
    mappings = {
        "audience_fit": "audience_relevance",
        "educational_value": "explainer_need",
        "visual_explainability": "video_suitability",
        "channel_differentiation": "channel_fit",
    }
    for deterministic_name, ai_name in mappings.items():
        baseline = float(positive.get(deterministic_name, 0))
        positive[deterministic_name] = round(baseline * 0.70 + values[ai_name] * 0.30, 2)
    positive["novelty"] = round(
        float(positive.get("novelty", 0)) * 0.80 + values["angle_originality"] * 0.20,
        2,
    )
    result = score_opportunity(positive, deterministic_penalties, weights=weights)
    return HybridQualificationResult(result, True, "AI advisory dimensions blended deterministically")
