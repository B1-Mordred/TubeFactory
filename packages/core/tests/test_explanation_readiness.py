import pytest

from editorial_core.explanation_readiness import (
    evaluate_explanation_readiness,
    explanation_policy,
    meta_claim_reason,
)


def _claim(index: int, *, central: bool = False) -> dict:
    return {
        "id": f"claim-{index}",
        "statement": f"Fachlich belegte Aussage Nummer {index} beschreibt einen konkreten Zusammenhang.",
        "status": "supported",
        "central": central,
        "coverage_unit_ids": [f"unit-{index % 6}"],
        "evidence": [
            {
                "relationship": "supports",
                "direct": True,
                "independent": True,
                "primary": index == 0,
                "source_id": f"source-{index}",
            },
            {
                "relationship": "supports",
                "direct": True,
                "independent": True,
                "primary": False,
                "source_id": f"other-{index}",
            },
        ],
    }


def test_faktischsimpel_policy_derives_expected_depth() -> None:
    policy = explanation_policy(
        {
            "duration_seconds": 480,
            "minimum_duration_seconds": 360,
            "maximum_duration_seconds": 600,
            "speaking_rate_wpm": 135,
        },
        {"evidence_density_minimum": 1.5},
    )
    assert policy.target_word_range == (810, 1350)
    assert policy.minimum_factual_claims == 13


def test_meta_claims_are_not_explanation_ready() -> None:
    assert meta_claim_reason("Ein potenzieller Lerninhalt könnte einen Vergleich umfassen.") == "meta_claim"
    assert meta_claim_reason("Technologien können verglichen und bewertet werden.") == "meta_claim"


def test_readiness_requires_depth_and_all_essential_units() -> None:
    policy = explanation_policy(
        {"duration_seconds": 480, "minimum_duration_seconds": 360, "maximum_duration_seconds": 600},
        {"evidence_density_minimum": 1.5},
    )
    units = [
        {"id": f"unit-{index}", "question": f"Frage {index}", "role": "mechanism", "essential": True}
        for index in range(6)
    ]
    result = evaluate_explanation_readiness(
        policy=policy,
        claims=[_claim(index, central=index < 2) for index in range(13)],
        coverage_units=units,
        minimum_independent_sources=2,
        minimum_primary_sources=1,
        counterevidence_search_completed=True,
    )
    assert result.ready
    assert len(result.usable_claim_ids) == 13


def test_invalid_policy_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="duration bounds"):
        explanation_policy(
            {"duration_seconds": 480, "minimum_duration_seconds": 500},
            {"evidence_density_minimum": 1.5},
        )
