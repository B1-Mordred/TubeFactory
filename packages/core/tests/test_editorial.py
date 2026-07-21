from uuid import uuid4

import pytest

from editorial_core.editorial import (
    ScriptSegmentDraft,
    StatementAnnotation,
    StatementKind,
    regenerate_scene_versions,
    validate_scene_spec,
    verify_script_draft,
)


def segment(
    segment_type: str,
    narration: str,
    *,
    claim_id: str | None,
) -> ScriptSegmentDraft:
    return ScriptSegmentDraft(
        segment_key=segment_type,
        segment_type=segment_type,
        narration=narration,
        presentation_purpose=segment_type,
        duration_seconds=4,
        citation_display={},
        annotations=(
            StatementAnnotation(
                text=narration,
                start_offset=0,
                end_offset=len(narration),
                kind=StatementKind.FACT if segment_type != "call_to_action" else StatementKind.EDITORIAL,
                claim_ids=(claim_id,) if claim_id else (),
            ),
        ),
    )


def complete_script(claim_id: str) -> list[ScriptSegmentDraft]:
    return [
        segment(part, f"Supported {part.replace('_', ' ')} statement.", claim_id=claim_id)
        for part in (
            "hook",
            "thesis",
            "context",
            "evidence",
            "counterevidence",
            "conclusion",
            "uncertainty",
        )
    ] + [segment("call_to_action", "Review the public evidence.", claim_id=None)]


def test_unsupported_factual_sentence_blocks_script() -> None:
    claim_id = str(uuid4())
    script = complete_script(claim_id)
    script[3] = segment(
        "evidence",
        "This added factual sentence has no dossier support.",
        claim_id=None,
    )
    report = verify_script_draft(
        script,
        approved_claim_ids=[claim_id],
        central_claim_ids=[claim_id],
    )
    assert report.valid is False
    assert "unsupported_factual_statement" in {issue.code for issue in report.issues}


def test_complete_claim_linked_script_passes_with_full_coverage() -> None:
    claim_id = str(uuid4())
    report = verify_script_draft(
        complete_script(claim_id),
        approved_claim_ids=[claim_id],
        central_claim_ids=[claim_id],
    )
    assert report.valid is True
    assert report.coverage_percent == 100


def test_factual_evidence_must_belong_to_the_linked_claim() -> None:
    claim_id = str(uuid4())
    other_claim_id = str(uuid4())
    excerpt_id = str(uuid4())
    script = complete_script(claim_id)
    statement = script[1].annotations[0]
    script[1] = ScriptSegmentDraft(
        **{
            **script[1].__dict__,
            "annotations": (
                StatementAnnotation(
                    **{**statement.__dict__, "evidence_excerpt_id": excerpt_id}
                ),
            ),
        }
    )
    report = verify_script_draft(
        script,
        approved_claim_ids=[claim_id, other_claim_id],
        central_claim_ids=[claim_id],
        evidence_text_by_id={excerpt_id: statement.text},
        evidence_claim_ids_by_id={excerpt_id: [other_claim_id]},
    )
    codes = {issue.code for issue in report.issues}
    assert report.valid is False
    assert "evidence_claim_mismatch" in codes
    assert "missing_evidence_excerpt" in codes


def scene(scene_id: str, order: int) -> dict:
    return {
        "scene_id": scene_id,
        "order": order,
        "purpose": "Explain one supported point",
        "narration_segment_ids": [f"segment-{order}"],
        "claim_ids": [],
        "duration": 4,
        "visual_type": "text",
        "visual_brief": "Neutral evidence-first typography",
        "on_screen_text": ["Evidence"],
        "citation_style": "lower-third",
        "source_ids": [],
        "asset_requests": [],
        "transition": "cut",
        "music_sfx_policy": "none",
        "synthetic_media_flag": False,
        "accessibility_notes": "Read text aloud",
    }


def test_scene_spec_rejects_unknown_fields_and_unsafe_generated_evidence() -> None:
    value = scene("scene-1", 1)
    value.update(
        {
            "visual_type": "source_screenshot",
            "synthetic_media_flag": True,
            "execute_workflow": "arbitrary code",
        }
    )
    errors = validate_scene_spec(value)
    assert any("unknown SceneSpec" in error for error in errors)
    assert any("requires explicit source_ids" in error for error in errors)
    assert any("evidence visuals cannot" in error for error in errors)


def test_regenerating_one_scene_preserves_locked_scene_exactly() -> None:
    locked = scene("scene-1", 1)
    editable = scene("scene-2", 2)
    replacement = {**editable, "visual_brief": "Alternative typography"}
    result = regenerate_scene_versions(
        [locked, editable],
        {"scene-2": replacement},
        locked_scene_ids=["scene-1"],
    )
    assert result[0] == locked
    assert result[1]["visual_brief"] == "Alternative typography"
    with pytest.raises(ValueError, match="locked"):
        regenerate_scene_versions(
            [locked, editable],
            {"scene-1": replacement},
            locked_scene_ids=["scene-1"],
        )
