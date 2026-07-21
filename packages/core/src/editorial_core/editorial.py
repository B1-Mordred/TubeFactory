from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping
from uuid import UUID


_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_REQUIRED_SCRIPT_PARTS = frozenset(
    {
        "hook",
        "thesis",
        "context",
        "evidence",
        "counterevidence",
        "conclusion",
        "uncertainty",
        "call_to_action",
    }
)


class StatementKind(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    OPINION = "opinion"
    QUOTE = "quote"
    EDITORIAL = "editorial"


@dataclass(frozen=True)
class StatementAnnotation:
    text: str
    start_offset: int
    end_offset: int
    kind: StatementKind
    claim_ids: tuple[str, ...] = ()
    evidence_excerpt_id: str | None = None


@dataclass(frozen=True)
class ScriptSegmentDraft:
    segment_key: str
    segment_type: str
    narration: str
    presentation_purpose: str
    duration_seconds: float
    citation_display: Mapping[str, Any]
    annotations: tuple[StatementAnnotation, ...]
    locked: bool = False


@dataclass(frozen=True)
class ScriptVerificationIssue:
    code: str
    severity: str
    segment_key: str | None
    statement: str | None
    message: str


@dataclass(frozen=True)
class ScriptVerificationReport:
    valid: bool
    coverage_percent: int
    issues: tuple[ScriptVerificationIssue, ...]
    linked_claim_ids: tuple[str, ...]


def narration_sentences(text: str) -> tuple[tuple[str, int, int], ...]:
    sentences: list[tuple[str, int, int]] = []
    for match in _SENTENCE.finditer(text):
        raw = match.group(0)
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw) - len(raw.rstrip())
        start = match.start() + leading
        end = match.end() - trailing
        if start < end:
            sentences.append((text[start:end], start, end))
    return tuple(sentences)


def verify_script_draft(
    segments: Iterable[ScriptSegmentDraft],
    *,
    approved_claim_ids: Iterable[str],
    central_claim_ids: Iterable[str],
    evidence_text_by_id: Mapping[str, str] | None = None,
    evidence_claim_ids_by_id: Mapping[str, Iterable[str]] | None = None,
    maximum_quote_words: int = 25,
) -> ScriptVerificationReport:
    values = tuple(segments)
    approved = frozenset(str(UUID(value)) for value in approved_claim_ids)
    central = frozenset(str(UUID(value)) for value in central_claim_ids)
    evidence = evidence_text_by_id or {}
    evidence_claims = {
        str(excerpt_id): frozenset(str(UUID(claim_id)) for claim_id in claim_ids)
        for excerpt_id, claim_ids in (evidence_claim_ids_by_id or {}).items()
    }
    issues: list[ScriptVerificationIssue] = []
    linked: set[str] = set()
    present_parts = {segment.segment_type for segment in values}
    for missing in sorted(_REQUIRED_SCRIPT_PARTS - present_parts):
        issues.append(
            ScriptVerificationIssue(
                code="missing_structure_part",
                severity="error",
                segment_key=None,
                statement=None,
                message=f"Required script part is missing: {missing}.",
            )
        )

    seen_keys: set[str] = set()
    for segment in values:
        if segment.segment_key in seen_keys:
            issues.append(
                ScriptVerificationIssue(
                    "duplicate_segment_key",
                    "error",
                    segment.segment_key,
                    None,
                    "Segment keys must be unique within a version.",
                )
            )
        seen_keys.add(segment.segment_key)
        annotations_by_location = {
            (annotation.start_offset, annotation.end_offset): annotation
            for annotation in segment.annotations
        }
        for sentence, start, end in narration_sentences(segment.narration):
            annotation = annotations_by_location.get((start, end))
            if annotation is None or annotation.text != sentence:
                issues.append(
                    ScriptVerificationIssue(
                        "unannotated_sentence",
                        "error",
                        segment.segment_key,
                        sentence,
                        "Every narration sentence must have an exact, offset-bound annotation.",
                    )
                )
                continue
            normalized_claims: list[str] = []
            for claim_id in annotation.claim_ids:
                try:
                    normalized_claims.append(str(UUID(claim_id)))
                except ValueError:
                    issues.append(
                        ScriptVerificationIssue(
                            "invalid_claim_id",
                            "error",
                            segment.segment_key,
                            sentence,
                            "Statement references a malformed claim ID.",
                        )
                    )
            if annotation.kind in {
                StatementKind.FACT,
                StatementKind.INFERENCE,
                StatementKind.QUOTE,
            } and not normalized_claims:
                issues.append(
                    ScriptVerificationIssue(
                        "unsupported_factual_statement",
                        "error",
                        segment.segment_key,
                        sentence,
                        "Facts, inferences and quotations require an approved dossier claim.",
                    )
                )
            unknown = set(normalized_claims) - approved
            if unknown:
                issues.append(
                    ScriptVerificationIssue(
                        "unapproved_claim_reference",
                        "error",
                        segment.segment_key,
                        sentence,
                        "Statement references a claim outside the approved dossier version.",
                    )
                )
            linked.update(set(normalized_claims) & approved)
            excerpt_id = annotation.evidence_excerpt_id
            if evidence_claim_ids_by_id is not None and annotation.kind in {
                StatementKind.FACT,
                StatementKind.INFERENCE,
                StatementKind.QUOTE,
            }:
                if not excerpt_id:
                    issues.append(
                        ScriptVerificationIssue(
                            "missing_evidence_excerpt",
                            "error",
                            segment.segment_key,
                            sentence,
                            "Facts, inferences and quotations require an exact evidence excerpt.",
                        )
                    )
                elif not (
                    set(normalized_claims) & evidence_claims.get(excerpt_id, frozenset())
                ):
                    issues.append(
                        ScriptVerificationIssue(
                            "evidence_claim_mismatch",
                            "error",
                            segment.segment_key,
                            sentence,
                            "The evidence excerpt is not linked to the referenced dossier claim.",
                        )
                    )
            if annotation.kind is StatementKind.QUOTE:
                excerpt = evidence.get(excerpt_id or "")
                if not excerpt or sentence.strip(' "“”') not in excerpt:
                    issues.append(
                        ScriptVerificationIssue(
                            "quotation_mismatch",
                            "error",
                            segment.segment_key,
                            sentence,
                            "Quoted narration must match an approved exact evidence excerpt.",
                        )
                    )
                if len(_WORD.findall(sentence)) > maximum_quote_words:
                    issues.append(
                        ScriptVerificationIssue(
                            "quotation_limit_exceeded",
                            "error",
                            segment.segment_key,
                            sentence,
                            "Quotation exceeds the configured word limit.",
                        )
                    )

    missing_central = central - linked
    for claim_id in sorted(missing_central):
        issues.append(
            ScriptVerificationIssue(
                "central_claim_omitted",
                "error",
                None,
                None,
                f"Central approved claim is not represented: {claim_id}.",
            )
        )
    coverage = 100 if not central else round(len(central & linked) / len(central) * 100)
    return ScriptVerificationReport(
        valid=not any(issue.severity == "error" for issue in issues),
        coverage_percent=coverage,
        issues=tuple(issues),
        linked_claim_ids=tuple(sorted(linked)),
    )


VISUAL_TYPES = frozenset(
    {
        "title_card",
        "citation_card",
        "text",
        "diagram",
        "timeline",
        "chart",
        "source_screenshot",
        "licensed_media",
        "comfyui_image",
        "comfyui_video",
        "waveform",
        "branded_transition",
    }
)
_SCENE_KEYS = frozenset(
    {
        "scene_id",
        "order",
        "purpose",
        "narration_segment_ids",
        "claim_ids",
        "duration",
        "visual_type",
        "visual_brief",
        "on_screen_text",
        "citation_style",
        "source_ids",
        "asset_requests",
        "transition",
        "music_sfx_policy",
        "synthetic_media_flag",
        "accessibility_notes",
    }
)


def validate_scene_spec(scene: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    unknown = set(scene) - _SCENE_KEYS
    missing = _SCENE_KEYS - set(scene)
    if unknown:
        errors.append(f"unknown SceneSpec fields: {', '.join(sorted(unknown))}")
    if missing:
        errors.append(f"missing SceneSpec fields: {', '.join(sorted(missing))}")
    visual_type = scene.get("visual_type")
    if visual_type not in VISUAL_TYPES:
        errors.append("visual_type is not in the approved registry")
    if not isinstance(scene.get("order"), int) or scene.get("order", 0) < 1:
        errors.append("order must be a positive integer")
    duration = scene.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not 0.5 <= duration <= 900:
        errors.append("duration must be between 0.5 and 900 seconds")
    for field in ("narration_segment_ids", "claim_ids", "source_ids", "asset_requests"):
        if not isinstance(scene.get(field), list):
            errors.append(f"{field} must be a list")
    if visual_type in {"source_screenshot", "citation_card", "chart"} and not scene.get("source_ids"):
        errors.append(f"{visual_type} requires explicit source_ids")
    if visual_type in {"comfyui_image", "comfyui_video"} and not scene.get("synthetic_media_flag"):
        errors.append("generated visuals must set synthetic_media_flag")
    if visual_type in {"chart", "source_screenshot", "citation_card"} and scene.get(
        "synthetic_media_flag"
    ):
        errors.append("evidence visuals cannot be marked as generated synthetic media")
    for field in ("purpose", "visual_brief", "citation_style", "transition", "music_sfx_policy"):
        if not isinstance(scene.get(field), str) or not scene.get(field, "").strip():
            errors.append(f"{field} must be non-empty text")
    if not isinstance(scene.get("on_screen_text"), list) or not all(
        isinstance(item, str) for item in scene.get("on_screen_text", [])
    ):
        errors.append("on_screen_text must be a list of strings")
    return tuple(errors)


def validate_storyboard(
    scenes: Iterable[Mapping[str, Any]],
    *,
    expected_segment_ids: Iterable[str],
) -> tuple[str, ...]:
    values = tuple(scenes)
    errors: list[str] = []
    orders: set[int] = set()
    covered_segments: set[str] = set()
    for index, scene in enumerate(values):
        errors.extend(f"scene[{index}]: {error}" for error in validate_scene_spec(scene))
        order = scene.get("order")
        if isinstance(order, int) and order in orders:
            errors.append(f"scene[{index}]: order is duplicated")
        if isinstance(order, int):
            orders.add(order)
        covered_segments.update(str(value) for value in scene.get("narration_segment_ids", []))
    expected = {str(value) for value in expected_segment_ids}
    if covered_segments != expected:
        errors.append("storyboard narration coverage must exactly match the script version")
    if orders and orders != set(range(1, len(values) + 1)):
        errors.append("scene order must be contiguous starting at one")
    return tuple(errors)


def regenerate_scene_versions(
    current: Iterable[Mapping[str, Any]],
    replacements: Mapping[str, Mapping[str, Any]],
    *,
    locked_scene_ids: Iterable[str],
) -> tuple[Mapping[str, Any], ...]:
    locked = frozenset(locked_scene_ids)
    output: list[Mapping[str, Any]] = []
    current_ids = {str(scene["scene_id"]) for scene in current}
    if set(replacements) - current_ids:
        raise ValueError("replacement references an unknown scene")
    if set(replacements) & locked:
        raise ValueError("locked scenes cannot be regenerated")
    for scene in current:
        scene_id = str(scene["scene_id"])
        replacement = replacements.get(scene_id)
        output.append(dict(replacement) if replacement is not None else dict(scene))
    return tuple(output)


def content_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
