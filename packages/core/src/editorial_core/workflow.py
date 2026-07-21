from __future__ import annotations

from enum import StrEnum


class ProductionStage(StrEnum):
    DISCOVERY = "DISCOVERY"
    SHORTLISTED = "SHORTLISTED"
    RESEARCHING = "RESEARCHING"
    DOSSIER_REVIEW = "DOSSIER_REVIEW"
    SCRIPTING = "SCRIPTING"
    VERIFYING = "VERIFYING"
    STORYBOARDING = "STORYBOARDING"
    GENERATING_ASSETS = "GENERATING_ASSETS"
    ASSEMBLING = "ASSEMBLING"
    QA_REVIEW = "QA_REVIEW"
    FINAL_APPROVAL = "FINAL_APPROVAL"
    UPLOADED_PRIVATE = "UPLOADED_PRIVATE"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    MONITORING = "MONITORING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


PRIMARY_STAGES: tuple[ProductionStage, ...] = (
    ProductionStage.DISCOVERY,
    ProductionStage.SHORTLISTED,
    ProductionStage.RESEARCHING,
    ProductionStage.DOSSIER_REVIEW,
    ProductionStage.SCRIPTING,
    ProductionStage.VERIFYING,
    ProductionStage.STORYBOARDING,
    ProductionStage.GENERATING_ASSETS,
    ProductionStage.ASSEMBLING,
    ProductionStage.QA_REVIEW,
    ProductionStage.FINAL_APPROVAL,
    ProductionStage.UPLOADED_PRIVATE,
    ProductionStage.SCHEDULED,
    ProductionStage.PUBLISHED,
    ProductionStage.MONITORING,
)

INTERRUPTION_STAGES = frozenset(
    {
        ProductionStage.PAUSED,
        ProductionStage.BLOCKED,
        ProductionStage.FAILED,
        ProductionStage.CANCELLED,
    }
)


class TransitionError(ValueError):
    pass


def validate_transition(
    current: ProductionStage,
    target: ProductionStage,
    *,
    last_primary_stage: ProductionStage | None = None,
    explicit_return: bool = False,
) -> None:
    """Validate legal progression, interruption, resumption, or explicit return.

    External side effects and approval prerequisites are validated by application
    policies before this pure state policy is called.
    """

    if current == target:
        raise TransitionError("a transition must change the workflow state")

    if target in INTERRUPTION_STAGES:
        if current == ProductionStage.CANCELLED:
            raise TransitionError("cancelled workflows require an explicit earlier-stage return")
        return

    if target not in PRIMARY_STAGES:
        raise TransitionError(f"unknown target stage: {target}")

    if current in INTERRUPTION_STAGES:
        if last_primary_stage is None:
            raise TransitionError("resuming an interrupted workflow requires its last primary stage")
        if explicit_return:
            if PRIMARY_STAGES.index(target) > PRIMARY_STAGES.index(last_primary_stage):
                raise TransitionError("an explicit return may not skip ahead")
            return
        if target != last_primary_stage:
            raise TransitionError("resume must restore the last primary stage")
        return

    current_index = PRIMARY_STAGES.index(current)
    target_index = PRIMARY_STAGES.index(target)
    if target_index == current_index + 1:
        return
    if explicit_return and target_index < current_index:
        return
    raise TransitionError(
        f"illegal transition {current} -> {target}; advance one stage or explicitly return earlier"
    )
