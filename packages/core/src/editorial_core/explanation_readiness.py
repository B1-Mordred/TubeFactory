from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_META_CLAIM_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\blerninhalt\b",
        r"\blearning content\b",
        r"\bkönnte[n]?\b.{0,80}\bumfassen\b",
        r"\bcould\b.{0,80}\binclude\b",
        r"\bkönnen\b.{0,80}\bverglichen(?: und bewertet)? werden\b",
        r"\bcan be compared(?: and evaluated)?\b",
        r"\b(?:diese|die) (?:studie|quelle|arbeit) (?:beschreibt|diskutiert|behandelt)\b",
        r"\b(?:this|the) (?:study|source|paper) (?:describes|discusses|covers)\b",
    )
)


@dataclass(frozen=True)
class ExplanationPolicy:
    target_duration_seconds: int
    minimum_duration_seconds: int
    maximum_duration_seconds: int
    speaking_rate_wpm: int
    maximum_enrichment_rounds: int
    evidence_density_minimum: float
    minimum_coverage_units: int

    @property
    def target_word_range(self) -> tuple[int, int]:
        return (
            math.ceil(self.minimum_duration_seconds / 60 * self.speaking_rate_wpm),
            math.floor(self.maximum_duration_seconds / 60 * self.speaking_rate_wpm),
        )

    @property
    def minimum_factual_claims(self) -> int:
        return max(
            1,
            math.ceil(self.target_word_range[0] * self.evidence_density_minimum / 100),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_duration_seconds": self.target_duration_seconds,
            "minimum_duration_seconds": self.minimum_duration_seconds,
            "maximum_duration_seconds": self.maximum_duration_seconds,
            "speaking_rate_wpm": self.speaking_rate_wpm,
            "maximum_enrichment_rounds": self.maximum_enrichment_rounds,
            "evidence_density_minimum": self.evidence_density_minimum,
            "minimum_coverage_units": self.minimum_coverage_units,
            "target_word_range": list(self.target_word_range),
            "minimum_factual_claims": self.minimum_factual_claims,
        }


@dataclass(frozen=True)
class ExplanationReadiness:
    ready: bool
    policy: ExplanationPolicy
    usable_claim_ids: tuple[str, ...]
    rejected_claims: tuple[dict[str, str], ...]
    coverage_units: tuple[dict[str, Any], ...]
    gaps: tuple[str, ...]
    independent_source_count: int
    primary_source_count: int
    counterevidence_search_completed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            **self.policy.as_dict(),
            "usable_claim_count": len(self.usable_claim_ids),
            "usable_claim_ids": list(self.usable_claim_ids),
            "rejected_claims": list(self.rejected_claims),
            "coverage_units": list(self.coverage_units),
            "gaps": list(self.gaps),
            "independent_source_count": self.independent_source_count,
            "primary_source_count": self.primary_source_count,
            "counterevidence_search_completed": self.counterevidence_search_completed,
        }


def explanation_policy(
    format_policy: Mapping[str, Any], approval_profile: Mapping[str, Any]
) -> ExplanationPolicy:
    target = int(format_policy.get("duration_seconds", 600))
    minimum = int(format_policy.get("minimum_duration_seconds", math.ceil(target * 0.75)))
    maximum = int(format_policy.get("maximum_duration_seconds", math.floor(target * 1.25)))
    speaking_rate = int(format_policy.get("speaking_rate_wpm", 135))
    rounds = int(format_policy.get("max_enrichment_rounds", 3))
    minimum_units = int(format_policy.get("minimum_coverage_units", 6))
    density = float(approval_profile.get("evidence_density_minimum", 0.0))
    if not 60 <= target <= 3600:
        raise ValueError("duration_seconds must be between 60 and 3600")
    if not 30 <= minimum <= target <= maximum <= 3600:
        raise ValueError("explanation duration bounds must contain duration_seconds")
    if not 80 <= speaking_rate <= 220:
        raise ValueError("speaking_rate_wpm must be between 80 and 220")
    if not 0 <= rounds <= 10:
        raise ValueError("max_enrichment_rounds must be between 0 and 10")
    if not 3 <= minimum_units <= 20:
        raise ValueError("minimum_coverage_units must be between 3 and 20")
    if not 0 <= density <= 100:
        raise ValueError("evidence_density_minimum must be between 0 and 100")
    return ExplanationPolicy(
        target_duration_seconds=target,
        minimum_duration_seconds=minimum,
        maximum_duration_seconds=maximum,
        speaking_rate_wpm=speaking_rate,
        maximum_enrichment_rounds=rounds,
        evidence_density_minimum=density,
        minimum_coverage_units=minimum_units,
    )


def meta_claim_reason(statement: str) -> str | None:
    normalized = " ".join(statement.split())
    if any(pattern.search(normalized) for pattern in _META_CLAIM_PATTERNS):
        return "meta_claim"
    if normalized.endswith(("?", ":")):
        return "non_assertive_claim"
    return None


def evaluate_explanation_readiness(
    *,
    policy: ExplanationPolicy,
    claims: Iterable[Mapping[str, Any]],
    coverage_units: Iterable[Mapping[str, Any]],
    minimum_independent_sources: int,
    minimum_primary_sources: int,
    counterevidence_search_completed: bool,
) -> ExplanationReadiness:
    units = [dict(item) for item in coverage_units]
    unit_ids = {str(item.get("id", "")) for item in units if str(item.get("id", ""))}
    usable: list[str] = []
    rejected: list[dict[str, str]] = []
    covered: dict[str, set[str]] = {unit_id: set() for unit_id in unit_ids}
    independent_sources: set[str] = set()
    primary_sources: set[str] = set()
    has_counterevidence = False
    central_independence_failure = False

    for raw in claims:
        claim_id = str(raw.get("id", ""))
        statement = str(raw.get("statement") or raw.get("normalized_statement") or "").strip()
        reason = meta_claim_reason(statement)
        if not claim_id or raw.get("status") not in {"supported", "approved"}:
            reason = reason or "claim_not_supported"
        evidence = [dict(item) for item in raw.get("evidence", raw.get("links", []))]
        supports = [
            item
            for item in evidence
            if item.get("relationship") == "supports" and bool(item.get("direct", item.get("direct_evidence", False)))
        ]
        if not supports:
            reason = reason or "no_direct_support"
        claim_units = {
            str(value) for value in raw.get("coverage_unit_ids", []) if str(value) in unit_ids
        }
        if not claim_units:
            reason = reason or "missing_coverage_unit"
        if reason:
            rejected.append({"claim_id": claim_id, "reason": reason})
            continue
        support_keys = {
            str(item.get("independence_key") or item.get("source_id") or item.get("source_document_id") or "")
            for item in supports
            if bool(item.get("independent", item.get("source_independent", False)))
        } - {""}
        if bool(raw.get("central")) and len(support_keys) < minimum_independent_sources:
            rejected.append({"claim_id": claim_id, "reason": "central_claim_independence"})
            central_independence_failure = True
            continue
        usable.append(claim_id)
        for unit_id in claim_units:
            covered[unit_id].add(claim_id)
        independent_sources.update(support_keys)
        primary_sources.update(
            str(item.get("source_id") or item.get("source_document_id") or "")
            for item in supports
            if bool(item.get("primary", item.get("primary_source", False)))
        )
        has_counterevidence = has_counterevidence or any(
            item.get("relationship") == "contradicts" for item in evidence
        )

    rendered_units: list[dict[str, Any]] = []
    for unit in units:
        unit_id = str(unit.get("id", ""))
        claim_ids = sorted(covered.get(unit_id, set()))
        essential = bool(unit.get("essential", True))
        rendered_units.append(
            {**unit, "id": unit_id, "claim_ids": claim_ids, "status": "covered" if claim_ids else "missing"}
        )

    gaps: list[str] = []
    if len(units) < policy.minimum_coverage_units:
        gaps.append("coverage_plan_too_small")
    if len(usable) < policy.minimum_factual_claims:
        gaps.append("insufficient_usable_claims")
    if any(item["essential"] and item["status"] != "covered" for item in rendered_units):
        gaps.append("essential_coverage_units_missing")
    if len(independent_sources) < minimum_independent_sources:
        gaps.append("insufficient_independent_sources")
    if len(primary_sources) < minimum_primary_sources:
        gaps.append("insufficient_primary_sources")
    if central_independence_failure:
        gaps.append("central_claim_independence_unmet")
    if not (has_counterevidence or counterevidence_search_completed):
        gaps.append("counterevidence_search_missing")
    return ExplanationReadiness(
        ready=not gaps,
        policy=policy,
        usable_claim_ids=tuple(usable),
        rejected_claims=tuple(rejected),
        coverage_units=tuple(rendered_units),
        gaps=tuple(dict.fromkeys(gaps)),
        independent_source_count=len(independent_sources),
        primary_source_count=len(primary_sources - {""}),
        counterevidence_search_completed=counterevidence_search_completed,
    )
