import pytest

from editorial_core.workflow import ProductionStage, TransitionError, validate_transition


def test_can_advance_exactly_one_primary_stage() -> None:
    validate_transition(ProductionStage.DISCOVERY, ProductionStage.SHORTLISTED)
    with pytest.raises(TransitionError, match="advance one stage"):
        validate_transition(ProductionStage.DISCOVERY, ProductionStage.RESEARCHING)


def test_can_pause_and_resume_without_skipping() -> None:
    validate_transition(ProductionStage.RESEARCHING, ProductionStage.PAUSED)
    validate_transition(
        ProductionStage.PAUSED,
        ProductionStage.RESEARCHING,
        last_primary_stage=ProductionStage.RESEARCHING,
    )
    with pytest.raises(TransitionError, match="restore"):
        validate_transition(
            ProductionStage.PAUSED,
            ProductionStage.SCRIPTING,
            last_primary_stage=ProductionStage.RESEARCHING,
        )


def test_return_to_earlier_stage_must_be_explicit() -> None:
    with pytest.raises(TransitionError):
        validate_transition(ProductionStage.VERIFYING, ProductionStage.RESEARCHING)
    validate_transition(
        ProductionStage.VERIFYING,
        ProductionStage.RESEARCHING,
        explicit_return=True,
    )
