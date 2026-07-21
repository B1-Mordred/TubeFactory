from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class OperatingMode(StrEnum):
    ASSISTED = "assisted"
    SUPERVISED = "supervised"
    TRUSTED = "trusted"


SENSITIVE_TOPICS = frozenset(
    {
        "identifiable_accusation",
        "health",
        "legal",
        "finance",
        "politics",
        "breaking_news",
        "misinformation_fact_checking",
    }
)

_BASE_GATES: dict[OperatingMode, tuple[str, ...]] = {
    OperatingMode.ASSISTED: (
        "opportunity", "dossier", "script", "storyboard", "final_render", "publication"
    ),
    OperatingMode.SUPERVISED: ("opportunity", "dossier", "final_render", "publication"),
    OperatingMode.TRUSTED: ("publication",),
}


@dataclass(frozen=True)
class OperatingPolicyDecision:
    mode: OperatingMode
    risk: str
    sensitive_topics: tuple[str, ...]
    required_human_gates: tuple[str, ...]
    trusted_private_upload_eligible: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "risk": self.risk,
            "sensitive_topics": list(self.sensitive_topics),
            "required_human_gates": list(self.required_human_gates),
            "trusted_private_upload_eligible": self.trusted_private_upload_eligible,
            "reasons": list(self.reasons),
        }


def evaluate_operating_policy(
    *, mode: str, risk: str, sensitive_topics: Iterable[str] = ()
) -> OperatingPolicyDecision:
    selected_mode = OperatingMode(mode)
    if risk not in {"low", "medium", "high"}:
        raise ValueError("risk must be low, medium or high")
    topics = tuple(sorted(set(sensitive_topics)))
    unknown = set(topics) - SENSITIVE_TOPICS
    if unknown:
        raise ValueError(f"unknown sensitive topic categories: {', '.join(sorted(unknown))}")

    gates = list(_BASE_GATES[selected_mode])
    reasons = [f"{selected_mode.value} operating profile"]
    if risk == "high" or topics:
        for mandatory in ("dossier", "final_render"):
            if mandatory not in gates:
                gates.insert(-1, mandatory)
        reasons.append("high-risk or sensitive work requires dossier and final-video review")
    # Public release is never eligible for unattended progression. Trusted mode
    # may reach a private upload only when work is low risk and non-sensitive.
    eligible = selected_mode is OperatingMode.TRUSTED and risk == "low" and not topics
    if not eligible and selected_mode is OperatingMode.TRUSTED:
        reasons.append("trusted private progression disabled by risk policy")
    return OperatingPolicyDecision(
        mode=selected_mode,
        risk=risk,
        sensitive_topics=topics,
        required_human_gates=tuple(gates),
        trusted_private_upload_eligible=eligible,
        reasons=tuple(reasons),
    )


def validate_editorial_limits(
    *, factual_claim_count: int, narration_word_count: int,
    evidence_density_minimum: float, repeated_scene_count: int, repeated_scene_limit: int,
) -> tuple[str, ...]:
    if min(factual_claim_count, narration_word_count, repeated_scene_count, repeated_scene_limit) < 0:
        raise ValueError("editorial counters cannot be negative")
    density = factual_claim_count / max(1, narration_word_count) * 100
    failures: list[str] = []
    if density < evidence_density_minimum:
        failures.append("evidence_density_below_policy")
    if repeated_scene_count > repeated_scene_limit:
        failures.append("repeated_scene_limit_exceeded")
    return tuple(failures)
