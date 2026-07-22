from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from editorial_core.editorial import narration_sentences


SCRIPT_PARTS = (
    "hook",
    "thesis",
    "context",
    "evidence",
    "counterevidence",
    "conclusion",
    "uncertainty",
    "call_to_action",
)


def _annotation(text: str, *, kind: str, claim: dict[str, Any] | None) -> dict[str, Any]:
    sentence, start, end = narration_sentences(text)[0]
    evidence = (claim or {}).get("evidence", [])
    return {
        "text": sentence,
        "start_offset": start,
        "end_offset": end,
        "kind": kind,
        "claim_ids": [claim["id"]] if claim else [],
        "evidence_excerpt_id": evidence[0]["evidence_excerpt_id"] if evidence else None,
    }


def fake_script_output(inputs: dict[str, Any], model_name: str) -> dict[str, Any]:
    claims = inputs.get("claims", [])
    claim = next((item for item in claims if item.get("central")), claims[0] if claims else None)
    claim_kind = (claim or {}).get("claim_type", "fact")
    if claim_kind not in {"fact", "inference", "opinion"}:
        claim_kind = "fact"
    segments: list[dict[str, Any]] = []
    for index, part in enumerate(SCRIPT_PARTS, start=1):
        if claim is not None and part in {"thesis", "context", "evidence", "conclusion"}:
            narration = claim["normalized_statement"].strip()
            if narration[-1:] not in ".!?":
                narration += "."
            kind = claim_kind
        else:
            narration = {
                "hook": "This story begins with a question grounded in the approved dossier.",
                "counterevidence": "We also examine what could change this account.",
                "uncertainty": "The evidence may change, and corrections will be made visible.",
                "call_to_action": "Review the cited sources and draw your own conclusion.",
            }.get(part, "This section is an editorial bridge.")
            kind = "editorial"
        annotation = _annotation(narration, kind=kind, claim=claim)
        if "unsupported" in model_name.casefold() and part == "evidence":
            narration = "An unsupported factual sentence was added."
            annotation = _annotation(narration, kind="fact", claim=None)
        segments.append(
            {
                "segment_key": part,
                "segment_type": part,
                "narration": narration,
                "presentation_purpose": f"Present the {part.replace('_', ' ')} clearly.",
                "duration_seconds": 8.0,
                "citation_display": {"mode": "lower_third"},
                "annotations": [annotation],
                "locked": False,
            }
        )
    return {"title": inputs.get("title", "Evidence-based editorial"), "segments": segments}


def fake_verifier_output(inputs: dict[str, Any], _: str) -> dict[str, Any]:
    report = inputs["deterministic_report"]
    return {
        "valid": bool(report["valid"]),
        "coverage_percent": int(report["coverage_percent"]),
        "issues": report["issues"],
    }


def fake_storyboard_output(
    inputs: dict[str, Any], _: str, fixture_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    scenes = []
    for order, segment in enumerate(inputs["segments"], start=1):
        scene_id = str(uuid5(NAMESPACE_URL, f"{inputs['script_version_id']}:{segment['id']}"))
        source_ids = sorted(
            {
                source_id
                for annotation in segment.get("annotations", [])
                for source_id in annotation.get("source_ids", [])
            }
        )
        visual_brief = "A deterministic branded text layout supporting the narration."
        if fixture_context and order == fixture_context.get("scene_alternative_order"):
            visual_brief = (
                "An alternative deterministic branded text composition: "
                + str(fixture_context.get("instruction", "editorial variation"))[:240]
            )
        scenes.append(
            {
                "scene_id": scene_id,
                "order": order,
                "purpose": segment["presentation_purpose"],
                "narration_segment_ids": [segment["id"]],
                "claim_ids": sorted(
                    {
                        claim_id
                        for annotation in segment.get("annotations", [])
                        for claim_id in annotation.get("claim_ids", [])
                    }
                ),
                "duration": segment["duration_seconds"],
                "visual_type": "text",
                "visual_brief": visual_brief,
                "on_screen_text": [segment["narration"][:240]],
                "citation_style": "Show source title when a claim is present.",
                "source_ids": source_ids,
                "asset_requests": [],
                "transition": "short dissolve",
                "music_sfx_policy": "No sound effect; keep music below narration.",
                "synthetic_media_flag": False,
                "accessibility_notes": "Keep text high contrast and repeat essential meaning in narration.",
            }
        )
    return {"scenes": scenes}


def fake_output(
    task_type: str,
    inputs: dict[str, Any],
    model_name: str,
    fixture_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if task_type == "script_writer":
        return fake_script_output(inputs, model_name)
    if task_type == "script_verifier":
        return fake_verifier_output(inputs, model_name)
    if task_type == "storyboard":
        return fake_storyboard_output(inputs, model_name, fixture_context)
    if task_type == "research_query_planner":
        title = str(inputs.get("candidate", {}).get("title", "evidence topic"))[:180]
        units = [
            {"id": f"unit-{index}", "question": f"Which exact evidence answers explanation step {index} for {title}?", "role": role, "essential": True}
            for index, role in enumerate(("foundation", "mechanism", "evidence", "limits", "implications", "open_questions"), 1)
        ]
        return {
            "coverage_units": units,
            "queries": [
                {"query": f"{title} original study data", "role": "primary", "language": "en", "coverage_unit_ids": ["unit-1", "unit-2", "unit-3"]},
                {"query": f"{title} limitations critique", "role": "counterevidence", "language": "en", "coverage_unit_ids": ["unit-4", "unit-6"]},
            ],
            "confidence": 0.8,
            "abstained": False,
            "uncertainty": ["Deterministic fixture query plan"],
        }
    if task_type == "evidence_synthesizer":
        sources = [item for item in inputs.get("sources", []) if item.get("evidence")]
        evidence = [
            {"evidence_id": item["evidence"][0]["evidence_id"], "relationship": "supports"}
            for item in sources[:2]
        ]
        return {
            "claims": ([{
                "statement": "Two supplied independent fixture excerpts support this bounded review candidate.",
                "claim_type": "fact",
                "central": True,
                "coverage_unit_ids": ["unit-1"],
                "evidence": evidence,
            }] if len(evidence) == 2 else []),
            "confidence": 0.8 if len(evidence) == 2 else 0.2,
            "abstained": len(evidence) != 2,
            "uncertainty": ["Fixture synthesis requires human review"],
        }
    raise ValueError("fake provider does not support this task")
