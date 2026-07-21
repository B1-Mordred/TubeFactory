import pytest

from editorial_core.operating_policy import (
    OperatingMode,
    evaluate_operating_policy,
    validate_editorial_limits,
)


def test_assisted_and_supervised_profiles_have_deterministic_gates() -> None:
    assisted = evaluate_operating_policy(mode="assisted", risk="low")
    supervised = evaluate_operating_policy(mode="supervised", risk="low")
    assert assisted.required_human_gates == (
        "opportunity", "dossier", "script", "storyboard", "final_render", "publication"
    )
    assert supervised.required_human_gates == (
        "opportunity", "dossier", "final_render", "publication"
    )


def test_trusted_profile_never_removes_publication_or_sensitive_review() -> None:
    ordinary = evaluate_operating_policy(mode="trusted", risk="low")
    assert ordinary.mode is OperatingMode.TRUSTED
    assert ordinary.trusted_private_upload_eligible
    assert ordinary.required_human_gates == ("publication",)
    sensitive = evaluate_operating_policy(mode="trusted", risk="low", sensitive_topics=["health"])
    assert not sensitive.trusted_private_upload_eligible
    assert sensitive.required_human_gates == ("dossier", "final_render", "publication")


def test_unknown_sensitive_categories_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown sensitive"):
        evaluate_operating_policy(mode="trusted", risk="low", sensitive_topics=["anything-goes"])


def test_evidence_density_and_repetition_limits_are_deterministic() -> None:
    assert validate_editorial_limits(
        factual_claim_count=4, narration_word_count=100, evidence_density_minimum=3,
        repeated_scene_count=1, repeated_scene_limit=1,
    ) == ()
    assert validate_editorial_limits(
        factual_claim_count=1, narration_word_count=100, evidence_density_minimum=3,
        repeated_scene_count=2, repeated_scene_limit=1,
    ) == ("evidence_density_below_policy", "repeated_scene_limit_exceeded")
