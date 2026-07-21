import hashlib
import json
from copy import deepcopy
from uuid import uuid4

import pytest

from editorial_core.editorial import (
    ScriptSegmentDraft,
    StatementAnnotation,
    StatementKind,
    verify_script_draft,
)
from editorial_worker.contracts import ScriptDraft, StoryboardDraft, VerifierOutput
from editorial_worker.fakes import fake_output
from editorial_worker.model_activities import (
    _RATE_WINDOWS,
    _reserve_rate_slot,
    validate_structured_input,
)
from editorial_worker.storyboard_activities import (
    validate_scene_alternative,
    validate_storyboard_activity,
)
from editorial_worker.script_activities import merge_script_regeneration
from temporalio.exceptions import ApplicationError


def claim() -> dict:
    claim_id = str(uuid4())
    excerpt_id = str(uuid4())
    return {
        "id": claim_id,
        "normalized_statement": "The approved record supports this statement.",
        "claim_type": "fact",
        "central": True,
        "evidence": [
            {
                "evidence_excerpt_id": excerpt_id,
                "exact_text": "The approved record supports this statement.",
                "source_id": str(uuid4()),
            }
        ],
    }


def verify(output: dict, approved: dict):
    draft = ScriptDraft.model_validate(output)
    segments = tuple(
        ScriptSegmentDraft(
            segment_key=item.segment_key,
            segment_type=item.segment_type,
            narration=item.narration,
            presentation_purpose=item.presentation_purpose,
            duration_seconds=item.duration_seconds,
            citation_display=item.citation_display,
            annotations=tuple(
                StatementAnnotation(
                    text=annotation.text,
                    start_offset=annotation.start_offset,
                    end_offset=annotation.end_offset,
                    kind=StatementKind(annotation.kind),
                    claim_ids=tuple(str(value) for value in annotation.claim_ids),
                    evidence_excerpt_id=str(annotation.evidence_excerpt_id)
                    if annotation.evidence_excerpt_id
                    else None,
                )
                for annotation in item.annotations
            ),
        )
        for item in draft.segments
    )
    excerpt_id = approved["evidence"][0]["evidence_excerpt_id"]
    return verify_script_draft(
        segments,
        approved_claim_ids=[approved["id"]],
        central_claim_ids=[approved["id"]],
        evidence_text_by_id={excerpt_id: approved["evidence"][0]["exact_text"]},
        evidence_claim_ids_by_id={excerpt_id: [approved["id"]]},
    )


def test_supported_fake_script_satisfies_strict_contract_and_policy() -> None:
    approved = claim()
    output = fake_output("script_writer", {"title": "Fixture", "claims": [approved]}, "fixture")
    report = verify(output, approved)
    assert report.valid is True
    assert report.coverage_percent == 100


def test_unsupported_fake_script_is_persistable_but_blocks_progress() -> None:
    approved = claim()
    output = fake_output(
        "script_writer", {"title": "Fixture", "claims": [approved]}, "fixture-unsupported"
    )
    report = verify(output, approved)
    assert report.valid is False
    assert "unsupported_factual_statement" in {item.code for item in report.issues}


def test_fake_verifier_echoes_deterministic_gate() -> None:
    output = fake_output(
        "script_verifier",
        {
            "deterministic_report": {
                "valid": False,
                "coverage_percent": 75,
                "issues": [
                    {
                        "code": "unsupported_factual_statement",
                        "severity": "error",
                        "segment_key": "evidence",
                        "statement": "Unsupported.",
                        "message": "Blocked.",
                    }
                ],
            }
        },
        "verifier",
    )
    value = VerifierOutput.model_validate(output)
    assert value.valid is False
    assert value.coverage_percent == 75


def test_fake_storyboard_is_strictly_wrapped() -> None:
    script_version_id = str(uuid4())
    segment_id = str(uuid4())
    output = fake_output(
        "storyboard",
        {
            "script_version_id": script_version_id,
            "segments": [
                {
                    "id": segment_id,
                    "presentation_purpose": "Explain",
                    "narration": "Evidence.",
                    "duration_seconds": 5,
                    "annotations": [],
                }
            ],
        },
        "storyboard",
    )
    assert StoryboardDraft.model_validate(output).scenes[0]["narration_segment_ids"] == [
        segment_id
    ]


def test_input_schema_rejects_external_refs_and_unknown_fields() -> None:
    try:
        validate_structured_input({}, {"$ref": "https://example.org/unsafe.json"})
    except ValueError as exc:
        assert "only local references" in str(exc)
    else:
        raise AssertionError("external schema reference was accepted")
    try:
        validate_structured_input(
            {"unexpected": True},
            {"type": "object", "additionalProperties": False},
        )
    except ValueError as exc:
        assert "structured input failed" in str(exc)
    else:
        raise AssertionError("unknown structured input field was accepted")


def test_provider_rate_limit_uses_a_sliding_minute_window() -> None:
    provider_id = str(uuid4())
    _RATE_WINDOWS.pop(provider_id, None)
    assert _reserve_rate_slot(provider_id, 2, now=100.0) == 0
    assert _reserve_rate_slot(provider_id, 2, now=101.0) == 0
    assert _reserve_rate_slot(provider_id, 2, now=102.0) == pytest.approx(58.0)
    assert _reserve_rate_slot(provider_id, 2, now=160.0) == 0
    assert list(_RATE_WINDOWS[provider_id]) == [101.0, 160.0]
    _RATE_WINDOWS.pop(provider_id, None)


async def test_selected_regeneration_preserves_unselected_and_locked_segments() -> None:
    approved = claim()
    generated = fake_output("script_writer", {"title": "Fixture", "claims": [approved]}, "fixture")
    current = deepcopy(generated)
    changed_hook = "A deliberately different current hook."
    current["segments"][0]["narration"] = changed_hook
    current["segments"][0]["annotations"][0].update(
        text=changed_hook, start_offset=0, end_offset=len(changed_hook)
    )
    current["segments"][1]["locked"] = True
    merged = await merge_script_regeneration(
        {
            "current_draft": current,
            "generated_draft": generated,
            "selected_segment_keys": ["hook"],
        }
    )
    by_key = {item["segment_key"]: item for item in merged["draft"]["segments"]}
    assert by_key["hook"]["narration"] == generated["segments"][0]["narration"]
    assert by_key["thesis"] == current["segments"][1]
    assert by_key["thesis"]["locked"] is True


async def test_storyboard_rejects_claim_or_source_outside_referenced_segment() -> None:
    segment_id = str(uuid4())
    claim_id = str(uuid4())
    source_id = str(uuid4())
    output = fake_output(
        "storyboard",
        {
            "script_version_id": str(uuid4()),
            "segments": [
                {
                    "id": segment_id,
                    "presentation_purpose": "Explain",
                    "narration": "Evidence.",
                    "duration_seconds": 5,
                    "annotations": [
                        {"claim_ids": [claim_id], "source_ids": [source_id]}
                    ],
                }
            ],
        },
        "storyboard",
    )
    output["scenes"][0]["claim_ids"] = [str(uuid4())]
    with pytest.raises(ApplicationError, match="claim is not linked"):
        await validate_storyboard_activity(
            {
                "draft": output,
                "expected_segment_ids": [segment_id],
                "allowed_claim_ids": {segment_id: [claim_id]},
                "allowed_source_ids": {segment_id: [source_id]},
            }
        )


async def test_scene_alternative_is_distinct_and_preserves_combined_coverage() -> None:
    segment_id = str(uuid4())
    claim_id = str(uuid4())
    source_id = str(uuid4())
    inputs = {
        "script_version_id": str(uuid4()),
        "segments": [
            {
                "id": segment_id,
                "presentation_purpose": "Explain",
                "narration": "Evidence.",
                "duration_seconds": 5,
                "annotations": [{"claim_ids": [claim_id], "source_ids": [source_id]}],
            }
        ],
    }
    current = fake_output("storyboard", inputs, "storyboard")
    generated = fake_output(
        "storyboard",
        inputs,
        "storyboard",
        {"scene_alternative_order": 1, "instruction": "Use a clearer evidence card."},
    )
    base = current["scenes"][0]
    base_hash = hashlib.sha256(
        json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = await validate_scene_alternative(
        {
            "generated_draft": generated,
            "scene_id": base["scene_id"],
            "scene_order": 1,
            "base_scene_hash": base_hash,
            "current_scenes": current["scenes"],
            "expected_segment_ids": [segment_id],
            "allowed_claim_ids": {segment_id: [claim_id]},
            "allowed_source_ids": {segment_id: [source_id]},
        }
    )
    assert result["content_hash"] != base_hash
    assert result["scene_spec"]["scene_id"] == base["scene_id"]
    assert result["scene_spec"]["visual_brief"].startswith("An alternative")
