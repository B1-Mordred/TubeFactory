import hashlib
import inspect
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
from editorial_worker.channel_workflow import channel_workflow_context
from editorial_worker.fakes import fake_output
from editorial_worker.model_activities import (
    _RATE_WINDOWS,
    _reserve_rate_slot,
    validate_structured_input,
)
from editorial_worker.review_activities import _topic_review_eligible
from editorial_worker.storyboard_activities import (
    _assemble_storyboard_draft,
    _scene_concreteness_errors,
    validate_scene_alternative,
    validate_storyboard_activity,
)
from editorial_worker.script_activities import (
    _attach_imported_script,
    _assemble_script_draft,
    merge_script_regeneration,
    persist_script_regeneration,
)
from editorial_worker.workflows import (
    ExistingResearchScriptImportWorkflow,
    ScriptGenerationWorkflow,
    ScriptRegenerationWorkflow,
    _additional_instructions,
    _correction_script_beats,
    _correction_segment_keys,
    _compact_generated_segment,
    _extractive_fallback_content,
    _factual_audit_statements,
    _local_script_quality_issues,
    _near_duplicate_claim,
    _normalize_editorial_verifier_issues,
    _safe_counterevidence_segment,
    _safe_hook_segment,
    _safe_thesis_segment,
    _safe_uncertainty_segment,
    _script_segment_inputs,
    _script_beats,
    _script_target_words,
    _single_segment_routes,
    _verifier_correction_instruction,
)
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


def test_topic_review_quality_policy_filters_abstentions_and_weak_dimensions() -> None:
    policy = {
        "minimum_confidence": 0.65,
        "dimension_minimums": {
            "explainer_need": 70,
            "audience_relevance": 65,
            "video_suitability": 65,
            "channel_fit": 70,
        },
    }
    strong = {
        "confidence": 0.9,
        "abstained": False,
        "dimensions": {
            "explainer_need": 90,
            "audience_relevance": 80,
            "video_suitability": 85,
            "channel_fit": 85,
        },
    }

    assert _topic_review_eligible(strong, policy)
    assert not _topic_review_eligible({**strong, "abstained": True}, policy)
    assert not _topic_review_eligible(
        {**strong, "dimensions": {**strong["dimensions"], "channel_fit": 45}}, policy
    )
    assert _topic_review_eligible({"abstained": True}, {})


def test_factual_audit_pairs_each_statement_only_with_its_linked_evidence() -> None:
    first = claim()
    second = claim()
    checked = {
        "draft": {
            "segments": [
                {
                    "segment_key": "evidence-a",
                    "annotations": [
                        {
                            "text": "Die belegte Aussage.",
                            "kind": "fact",
                            "claim_ids": [first["id"], first["id"]],
                        },
                        {
                            "text": "Nur eine Überleitung.",
                            "kind": "editorial",
                            "claim_ids": [],
                        },
                    ],
                },
                {
                    "segment_key": "evidence-b",
                    "annotations": [
                        {
                            "text": "Eine zweite belegte Aussage.",
                            "kind": "inference",
                            "claim_ids": [second["id"]],
                        }
                    ],
                },
            ]
        }
    }

    statements = _factual_audit_statements(
        {"structured_inputs": {"claims": [first, second]}},
        checked,
        issue_scope_segment_keys={"evidence-a"},
    )

    assert statements == [
        {
            "segment_key": "evidence-a",
            "statement_kind": "fact",
            "statement": "Die belegte Aussage.",
            "linked_claims": [
                {
                    "id": first["id"],
                    "normalized_statement": first["normalized_statement"],
                    "exact_evidence_excerpts": [
                        first["evidence"][0]["exact_text"]
                    ],
                }
            ],
        }
    ]


def test_channel_instructions_are_bounded_and_merge_with_regeneration_request() -> None:
    prompt = "Explain step by step, separate evidence from uncertainty, and avoid jargon. " * 3
    workflow = channel_workflow_context(
        {
            "automation_workflow": {
                "key": "faktischsimpel.explainer",
                "name": "FaktischSimpel explainer",
                "version": 1,
                "enabled": True,
                "language": "de",
                "summary": "An evidence-first explanatory workflow with mandatory review gates.",
                "prompts": {
                    "script_writer": prompt,
                    "script_verifier": prompt,
                    "storyboard": prompt,
                },
                "stages": [
                    {"key": "research", "label": "Research", "mode": "automatic"},
                    {"key": "review", "label": "Review", "mode": "human_gate"},
                    {"key": "production", "label": "Production", "mode": "assisted"},
                ],
                "human_gates": [
                    "opportunity_shortlist",
                    "dossier_approval",
                    "script_approval",
                    "storyboard_approval",
                    "media_approval",
                    "publication_approval",
                ],
            }
        }
    )

    generated = _additional_instructions(
        {"channel_workflow": workflow}, "script_writer", "Rewrite only context."
    )

    assert "cannot relax the approved-claim boundary" in generated
    assert "Explain step by step" in generated
    assert generated.endswith("Rewrite only context.")
    assert len(generated) <= 5000


def test_operational_correction_keeps_priority_with_a_long_channel_instruction() -> None:
    correction = "Verifier correction must remain complete."
    generated = _additional_instructions(
        {
            "channel_workflow": {
                "instructions": {"script_writer": "Channel style. " * 800}
            }
        },
        "script_writer",
        correction,
    )

    assert len(generated) <= 5000
    assert generated.endswith(correction)
    assert generated.startswith("Channel style.")


def test_channel_without_workflow_adds_no_generation_instruction() -> None:
    context = channel_workflow_context({"evidence_first": True})

    assert context["active"] is False
    assert _additional_instructions({"channel_workflow": context}, "script_writer") == ""


def test_initial_generation_never_reads_regeneration_only_fields() -> None:
    initial_source = inspect.getsource(ScriptGenerationWorkflow.run)
    regeneration_source = inspect.getsource(ScriptRegenerationWorkflow.run)

    assert "selected_segment_keys" not in initial_source
    assert "context[\"instruction\"]" not in initial_source
    assert "selected_segment_keys" in regeneration_source
    assert "context[\"instruction\"]" in regeneration_source
    assert "base_draft=context[\"current_draft\"]" in regeneration_source
    assert "issue_scope_segment_keys" in regeneration_source


def test_existing_research_import_uses_normal_verification_and_persistence() -> None:
    source = inspect.getsource(ExistingResearchScriptImportWorkflow.run)

    assert '"load-script-generation-context"' in source
    assert '"load-script-regeneration-context"' in source
    assert '"verify-script-draft"' in source
    assert '"persist-script-result"' in source
    assert '"persist-script-regeneration"' in source


def test_imported_script_is_marked_untrusted_and_hashed() -> None:
    context = {"structured_inputs": {"title": "Research title", "claims": []}}
    request = {"script_title": "Author title", "script_text": "Author narration."}

    attached = _attach_imported_script(context, request)

    assert attached["structured_inputs"]["title"] == "Author title"
    assert attached["structured_inputs"]["imported_script"]["trust"] == "untrusted_author_input"
    assert attached["import_metadata"]["script_character_count"] == len("Author narration.")
    assert len(attached["import_metadata"]["script_text_hash"]) == 64


def test_regeneration_persists_the_verifier_decision_without_a_second_manual_run() -> None:
    workflow_source = inspect.getsource(ScriptRegenerationWorkflow.run)
    persistence_source = inspect.getsource(persist_script_regeneration)

    assert '"verifier_output": verifier["output"]' in workflow_source
    assert '"verifier_model_id": verifier["model_id"]' in workflow_source
    assert 'status = "verified" if final_valid else "blocked"' in persistence_source
    assert '"independent_verifier": verifier.model_dump' in persistence_source


def test_writer_chunk_schema_is_derived_without_mutating_active_route() -> None:
    route = {
        "response_schema": {
            "type": "object",
            "properties": {
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"segment_type": {"enum": ["hook", "thesis"]}},
                    },
                }
            },
        }
    }

    chunk = _single_segment_routes([route], "hook")[0]

    assert chunk["response_schema"]["required"] == ["segment"]
    assert chunk["response_schema"]["properties"]["segment"]["properties"]["segment_type"] == {"const": "hook"}
    assert route["response_schema"]["properties"]["segments"]["items"]["properties"]["segment_type"] == {"enum": ["hook", "thesis"]}


def test_chunk_schema_accepts_duplicate_transport_ids_for_canonical_assembly() -> None:
    route = {
        "response_schema": {
            "type": "object",
            "properties": {
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "segment_type": {"enum": ["evidence"]},
                            "sentences": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "claim_ids": {"type": "array", "uniqueItems": True},
                                        "evidence_excerpt_ids": {"type": "array", "uniqueItems": True},
                                    },
                                },
                            },
                        },
                    },
                }
            },
        }
    }

    chunk = _single_segment_routes([route], "evidence")[0]
    properties = chunk["response_schema"]["properties"]["segment"]["properties"]["sentences"]["items"]["properties"]

    assert "uniqueItems" not in properties["claim_ids"]
    assert "uniqueItems" not in properties["evidence_excerpt_ids"]
    assert route["response_schema"]["properties"]["segments"]["items"]["properties"]["sentences"]["items"]["properties"]["claim_ids"]["uniqueItems"] is True


def test_correction_compaction_keeps_first_atomic_claim_uses_and_drops_padding() -> None:
    first, second = str(uuid4()), str(uuid4())
    segment = {
        "segment_type": "counterevidence",
        "sentences": [
            {"text": "First fact.", "kind": "fact", "claim_ids": [first], "evidence_excerpt_ids": []},
            {"text": "Second fact.", "kind": "fact", "claim_ids": [second], "evidence_excerpt_ids": []},
            {"text": "The first fact again.", "kind": "fact", "claim_ids": [first], "evidence_excerpt_ids": []},
            {"text": "An unsupported summary.", "kind": "editorial", "claim_ids": [], "evidence_excerpt_ids": []},
        ],
    }

    compacted = _compact_generated_segment(segment, maximum_sentences=3)

    assert [item["text"] for item in compacted["sentences"]] == [
        "First fact.",
        "Second fact.",
    ]
    assert compacted["claim_ids"] == [first, second]


def test_call_to_action_uses_minimal_editorial_input_without_claim_payload() -> None:
    full = {
        "title": "A sourced explainer",
        "channel": {"name": "FaktischSimpel"},
        "dossier": {"unresolved_questions": ["What remains open?"]},
        "claims": [{"id": "allowed", "large": "evidence payload"}, {"id": "other", "large": "other evidence"}],
        "disputed_claims": [{"large": "counterevidence payload"}],
    }

    content = _script_segment_inputs(full, "call_to_action", 8)
    evidence = _script_segment_inputs(
        full,
        {
            "segment_key": "evidence",
            "segment_type": "evidence",
            "purpose": "Explain",
            "target_words": 90,
            "allowed_claim_ids": ["allowed"],
            "required_claim_ids": ["allowed"],
        },
        4,
    )

    assert "claims" not in content
    assert "disputed_claims" not in content
    assert content["editorial_context"]["unresolved_questions"] == [
        "What remains open?"
    ]
    assert evidence["claims"] == [full["claims"][0]]
    assert evidence["requested_segment"]["required_claim_ids"] == ["allowed"]


def test_partial_chunk_generation_requires_an_existing_base_draft() -> None:
    source = inspect.getsource(__import__(
        "editorial_worker.workflows", fromlist=["_write_script_segments"]
    )._write_script_segments)

    assert "partial script chunk generation requires a base draft" in source
    assert "generated_by_key.get" in source
    assert 'segment["annotations"]' in source


def test_verifier_correction_boundary_is_ordered_bounded_and_lock_aware() -> None:
    checked = {
        "draft": {
            "segments": [
                {"segment_key": "hook", "locked": False},
                {"segment_key": "evidence", "locked": True},
                {"segment_key": "conclusion", "locked": False},
            ]
        }
    }
    verifier = {
        "valid": False,
        "issues": [
            {"segment_key": "conclusion", "severity": "error"},
            {"segment_key": "evidence", "severity": "warning"},
            {"segment_key": "hook", "severity": "error"},
        ],
    }

    assert _correction_segment_keys(checked, verifier) == ("hook", "conclusion")
    assert _correction_segment_keys(
        checked, verifier, allowed_segment_keys={"conclusion"}
    ) == ("conclusion",)
    assert _correction_segment_keys(
        checked, verifier, allowed_segment_keys={"hook"}
    ) == ("hook",)


def test_global_blocking_issue_rewrites_all_unlocked_segments_even_with_warnings() -> None:
    checked = {
        "draft": {
            "segments": [
                {"segment_key": "context", "locked": False},
                {"segment_key": "evidence", "locked": False},
                {"segment_key": "conclusion", "locked": True},
            ]
        }
    }
    verifier = {
        "valid": False,
        "issues": [
            {"code": "narration_below_format_minimum", "severity": "error", "segment_key": None},
            {"code": "editorial_language", "severity": "warning", "segment_key": "context"},
        ],
    }

    assert _correction_segment_keys(checked, verifier) == ("context", "evidence")


def test_verifier_correction_does_not_rewrite_unrelated_allowed_segments() -> None:
    checked = {
        "draft": {
            "segments": [
                {"segment_key": "hook", "locked": False},
                {"segment_key": "conclusion", "locked": False},
            ]
        }
    }
    verifier = {
        "valid": False,
        "issues": [{"segment_key": "conclusion", "severity": "error"}],
    }

    assert _correction_segment_keys(
        checked, verifier, allowed_segment_keys={"hook"}
    ) == ()


def test_extractive_fallback_source_filters_to_blocking_errors() -> None:
    source = inspect.getsource(__import__(
        "editorial_worker.workflows", fromlist=["_refine_script_with_verifier"]
    )._refine_script_with_verifier)

    assert 'issue.get("severity") == "error"' in source
    assert "if blocking_issues" in source


def test_verifier_combination_never_accepts_a_report_containing_errors() -> None:
    source = inspect.getsource(__import__(
        "editorial_worker.workflows", fromlist=["_verify_script_with_ai"]
    )._verify_script_with_ai)

    assert 'issue.get("severity") == "error"' in source
    assert "primary_valid and audit_valid" in source
    assert "rhetorical question" in source


def test_verifier_feedback_is_deduplicated_and_keeps_review_as_quoted_data() -> None:
    issue = {
        "segment_key": "conclusion",
        "severity": "error",
        "statement": "An unsupported conclusion.",
        "message": "The evidence does not entail this conclusion.",
    }

    instruction = _verifier_correction_instruction(
        {"valid": False, "issues": [issue, issue]}, ("conclusion",), 2
    )

    assert "correction pass 2" in instruction
    assert "quoted review data" in instruction
    assert instruction.count("An unsupported conclusion.") == 1
    assert "Delete or narrow an unsupported assertion" in instruction
    encoded = instruction.split("quoted review data: ", 1)[1].split(
        ". Correct every listed defect.", 1
    )[0]
    assert json.loads(encoded) == [issue]


def test_extractive_fallback_uses_exact_approved_claim_text_and_evidence() -> None:
    approved = claim()
    checked = {
        "draft": {
            "title": "Explainer",
            "segments": [
                {
                    "segment_key": "07-conclusion",
                    "segment_type": "conclusion",
                    "presentation_purpose": "Conclude",
                    "annotations": [],
                }
            ],
        }
    }

    content = _extractive_fallback_content(
        {"structured_inputs": {"claims": [approved]}},
        checked,
        ("07-conclusion",),
    )

    sentences = content["segments"][0]["sentences"]
    assert sentences[0]["text"] == approved["normalized_statement"]
    assert sentences[0]["claim_ids"] == [approved["id"]]
    assert sentences[0]["evidence_excerpt_ids"] == [
        approved["evidence"][0]["evidence_excerpt_id"]
    ]
    assert sentences[-1]["kind"] == "editorial"


def test_safe_counterevidence_makes_no_factual_assertion() -> None:
    segment = _safe_counterevidence_segment()

    assert segment["segment_type"] == "counterevidence"
    assert len(segment["sentences"]) >= 3
    assert {sentence["kind"] for sentence in segment["sentences"]} == {"editorial"}
    assert all(not sentence["claim_ids"] for sentence in segment["sentences"])


def test_safe_hook_uses_title_only_as_a_question() -> None:
    segment = _safe_hook_segment("Warum Dinge funktionieren")

    assert segment["segment_type"] == "hook"
    assert segment["sentences"][0]["text"].endswith("?")
    assert "Warum Dinge funktionieren" in segment["sentences"][0]["text"]
    assert all(sentence["kind"] == "editorial" for sentence in segment["sentences"])


def test_safe_role_scaffolds_contain_no_subject_claims() -> None:
    for segment in (_safe_thesis_segment(), _safe_uncertainty_segment()):
        assert segment["claim_ids"] == []
        assert all(sentence["kind"] == "editorial" for sentence in segment["sentences"])
        assert all(not sentence["claim_ids"] for sentence in segment["sentences"])


def test_verifier_self_contradiction_becomes_warning_only_for_editorial_text() -> None:
    checked = {
        "draft": {
            "segments": [
                {
                    "annotations": [
                        {"text": "We now change perspective.", "kind": "editorial"},
                        {"text": "A factual sentence.", "kind": "fact"},
                    ]
                }
            ]
        }
    }
    issues = [
        {
            "code": "error",
            "severity": "error",
            "statement": "We now change perspective.",
            "message": "This is purely editorial and not checkable.",
        },
        {
            "code": "error",
            "severity": "error",
            "statement": "A factual sentence.",
            "message": "This factual statement is not checkable from the evidence.",
        },
    ]

    normalized = _normalize_editorial_verifier_issues(issues, checked)

    assert normalized[0]["severity"] == "warning"
    assert normalized[0]["code"] == "editorial_language"
    assert normalized[1]["severity"] == "error"


def test_local_quality_gate_blocks_duplicate_factual_blocks() -> None:
    duplicated = {
        "text": "An approved fact.",
        "kind": "fact",
        "claim_ids": [str(uuid4())],
        "evidence_excerpt_id": str(uuid4()),
    }
    checked = {
        "draft": {
            "segments": [
                {
                    "segment_key": "02-thesis",
                    "segment_type": "context",
                    "annotations": [duplicated],
                },
                {
                    "segment_key": "06-uncertainty",
                    "segment_type": "context",
                    "annotations": [duplicated],
                },
            ]
        }
    }

    assert _local_script_quality_issues(checked) == [
        {
            "code": "material_repetition",
            "message": (
                "This segment repeats the complete factual block from 02-thesis; "
                "use a distinct explanatory role or remove it."
            ),
            "severity": "error",
            "statement": "An approved fact.",
            "segment_key": "06-uncertainty",
        }
    ]


def test_local_quality_gate_enforces_configured_explanation_length_and_density() -> None:
    checked = {
        "draft": {
            "segments": [
                {
                    "segment_key": "01-hook",
                    "segment_type": "hook",
                    "narration": "too short",
                    "duration_seconds": 4,
                    "annotations": [
                        {"text": "too short", "kind": "editorial", "claim_ids": []}
                    ],
                }
            ]
        }
    }
    issues = _local_script_quality_issues(
        checked,
        {
            "script_policy": {
                "target_word_range": [810, 1350],
                "minimum_duration_seconds": 360,
                "maximum_duration_seconds": 600,
                "minimum_factual_claims": 13,
            }
        },
    )
    assert {issue["code"] for issue in issues} == {
        "narration_below_format_minimum",
        "duration_below_format_minimum",
        "evidence_density_below_format_minimum",
    }


def test_script_plan_targets_a_margin_and_assigns_each_claim_once() -> None:
    claims = [
        {
            "id": str(uuid4()),
            "central": index == 0,
            "coverage_unit_ids": ["mechanism" if index < 3 else "limits"],
        }
        for index in range(6)
    ]
    context = {
        "script_policy": {"target_word_range": [810, 1350]},
        "structured_inputs": {
            "claims": claims,
            "explanation_plan": [
                {"id": "mechanism", "role": "mechanism", "question": "How?"},
                {"id": "limits", "role": "limits", "question": "Limits?"},
            ],
        },
    }

    beats = _script_beats(context)
    required = [value for beat in beats for value in beat["required_claim_ids"]]

    assert _script_target_words(context["script_policy"]) == 932
    assert sorted(required) == sorted(item["id"] for item in claims)
    assert len(required) == len(set(required))
    assert sum(beat["target_words"] for beat in beats) >= 932


def test_correction_plan_closes_length_and_missing_claim_deficits() -> None:
    present, missing = str(uuid4()), str(uuid4())
    base = {
        "segments": [
            {
                "segment_key": "03-context",
                "segment_type": "context",
                "presentation_purpose": "Foundation",
                "narration": "word " * 100,
                "annotations": [{"claim_ids": [present]}],
            },
            {
                "segment_key": "04-evidence",
                "segment_type": "evidence",
                "presentation_purpose": "Mechanism",
                "narration": "word " * 650,
                "annotations": [{"claim_ids": []}],
            },
        ]
    }
    context = {
        "script_policy": {"target_word_range": [810, 1350]},
        "structured_inputs": {
            "claims": [
                {"id": present, "coverage_unit_ids": ["foundation"]},
                {"id": missing, "coverage_unit_ids": ["mechanism"]},
            ]
        },
    }

    beats = _correction_script_beats(
        context, base, {"03-context", "04-evidence"}
    )

    assert sum(beat["target_words"] for beat in beats) >= 932
    assert missing in next(
        beat for beat in beats if beat["segment_type"] == "evidence"
    )["required_claim_ids"]


def test_correction_plan_does_not_reward_growth_without_a_global_deficit() -> None:
    claim_id = str(uuid4())
    base = {
        "segments": [
            {
                "segment_key": "04-evidence",
                "segment_type": "evidence",
                "presentation_purpose": "Mechanism",
                "narration": "word " * 100,
                "annotations": [{"claim_ids": [claim_id]}],
            }
        ]
    }
    context = {
        "script_policy": {"target_word_range": [0, 1350]},
        "structured_inputs": {
            "claims": [{"id": claim_id, "coverage_unit_ids": ["mechanism"]}]
        },
    }

    beat = _correction_script_beats(context, base, {"04-evidence"})[0]

    assert beat["target_words"] == 35


def test_correction_plan_sizes_rewrites_by_distinct_evidence_concepts() -> None:
    first, duplicate, other = str(uuid4()), str(uuid4()), str(uuid4())
    claims = [
        {
            "id": first,
            "normalized_statement": "Geothermal brines have complex chemistry, high salinity, and high temperatures.",
            "coverage_unit_ids": ["limits"],
        },
        {
            "id": duplicate,
            "normalized_statement": "Geothermal brines present complex chemistry, high salinity, and high temperatures.",
            "coverage_unit_ids": ["limits"],
        },
        {
            "id": other,
            "normalized_statement": "Battery recycling provides another sustainable supply path.",
            "coverage_unit_ids": ["alternatives"],
        },
    ]
    base = {
        "segments": [
            {
                "segment_key": "06-counterevidence",
                "segment_type": "counterevidence",
                "presentation_purpose": "Limits and alternatives",
                "narration": "word " * 140,
                "annotations": [{"claim_ids": [first, duplicate, other]}],
            }
        ]
    }
    context = {
        "script_policy": {"target_word_range": [0, 1350]},
        "structured_inputs": {"claims": claims},
    }

    beat = _correction_script_beats(
        context, base, {"06-counterevidence"}
    )[0]

    assert beat["target_words"] == 64
    assert beat["maximum_sentences"] == 3


def test_near_duplicate_claims_are_consolidated_on_one_selected_owner_beat() -> None:
    first, duplicate, alternative = str(uuid4()), str(uuid4()), str(uuid4())
    limit_one = {
        "id": first,
        "normalized_statement": "Geothermal brines face challenges from complex chemistry, high salinity, and high temperatures.",
        "coverage_unit_ids": ["limits"],
    }
    limit_two = {
        "id": duplicate,
        "normalized_statement": "Geothermal brines present challenges due to complex chemistry, high salinity, and high temperatures.",
        "coverage_unit_ids": ["limits"],
    }
    recycling = {
        "id": alternative,
        "normalized_statement": "Battery recycling provides an alternative lithium supply path.",
        "coverage_unit_ids": ["alternatives"],
    }
    assert _near_duplicate_claim(limit_one, limit_two) is True
    assert _near_duplicate_claim(limit_one, recycling) is False
    base = {
        "segments": [
            {
                "segment_key": "06-counterevidence",
                "segment_type": "counterevidence",
                "presentation_purpose": "Limits",
                "narration": "Technical limits are explained here with sufficient detail for viewers.",
                "annotations": [{"claim_ids": [first]}],
            },
            {
                "segment_key": "07-counterevidence",
                "segment_type": "counterevidence",
                "presentation_purpose": "Alternatives",
                "narration": "Alternative supply paths are explained here with sufficient detail.",
                "annotations": [{"claim_ids": [duplicate, alternative]}],
            },
        ]
    }
    context = {
        "script_policy": {"target_word_range": [0, 1350]},
        "structured_inputs": {"claims": [limit_one, limit_two, recycling]},
    }

    beats = _correction_script_beats(
        context, base, {"06-counterevidence", "07-counterevidence"}
    )

    limits = next(beat for beat in beats if beat["segment_key"] == "06-counterevidence")
    alternatives = next(beat for beat in beats if beat["segment_key"] == "07-counterevidence")
    assert set(limits["required_claim_ids"]) == {first, duplicate}
    assert alternatives["required_claim_ids"] == [alternative]


def test_local_quality_gate_rejects_meta_filler_weak_counterevidence_and_repetition() -> None:
    repeated = "Welche Aussagen tragen die Quellen direkt und welche Fragen bleiben offen?"
    checked = {
        "draft": {
            "segments": [
                {
                    "segment_key": "05-counterevidence",
                    "segment_type": "counterevidence",
                    "narration": "Wir prüfen die freigegebenen Claims und wechseln die Perspektive.",
                    "duration_seconds": 10,
                    "annotations": [{"text": "Wir prüfen die freigegebenen Claims und wechseln die Perspektive.", "kind": "editorial", "claim_ids": []}],
                },
                {
                    "segment_key": "09-uncertainty",
                    "segment_type": "uncertainty",
                    "narration": repeated,
                    "duration_seconds": 10,
                    "annotations": [{"text": repeated, "kind": "editorial", "claim_ids": []}],
                },
                {
                    "segment_key": "10-uncertainty",
                    "segment_type": "uncertainty",
                    "narration": repeated,
                    "duration_seconds": 10,
                    "annotations": [{"text": repeated, "kind": "editorial", "claim_ids": []}],
                },
            ]
        }
    }

    codes = {issue["code"] for issue in _local_script_quality_issues(checked)}

    assert "counterevidence_without_evidence" in codes
    assert "workflow_meta_language" in codes
    assert "repeated_sentence" in codes


def test_local_quality_gate_rejects_claim_set_padding_within_one_segment() -> None:
    claim_id = str(uuid4())
    checked = {
        "draft": {
            "segments": [
                {
                    "segment_key": "04-evidence",
                    "segment_type": "evidence",
                    "narration": "Complex chemistry, salinity, and temperature hinder extraction. Complex chemistry, high salinity, and high temperature make extraction difficult.",
                    "duration_seconds": 12,
                    "annotations": [
                        {"text": "Complex chemistry, salinity, and temperature hinder extraction.", "kind": "fact", "claim_ids": [claim_id]},
                        {"text": "Complex chemistry, high salinity, and high temperature make extraction difficult.", "kind": "fact", "claim_ids": [claim_id]},
                    ],
                }
            ]
        }
    }

    issues = _local_script_quality_issues(checked)

    assert "repeated_claim_set_within_segment" in {
        issue["code"] for issue in issues
    }


def test_local_quality_gate_allows_distinct_details_from_one_linked_claim() -> None:
    claim_id = str(uuid4())
    checked = {
        "draft": {
            "segments": [
                {
                    "segment_key": "04-evidence",
                    "segment_type": "evidence",
                    "narration": "Solvent extraction is evaluated. Adsorbents are also evaluated. Electrochemical systems form a third group.",
                    "duration_seconds": 12,
                    "annotations": [
                        {"text": "Solvent extraction is evaluated.", "kind": "fact", "claim_ids": [claim_id]},
                        {"text": "Adsorbents are also evaluated.", "kind": "fact", "claim_ids": [claim_id]},
                        {"text": "Electrochemical systems form a third group.", "kind": "fact", "claim_ids": [claim_id]},
                    ],
                }
            ]
        }
    }

    issues = _local_script_quality_issues(checked)

    assert "repeated_claim_set_within_segment" not in {
        issue["code"] for issue in issues
    }


def test_storyboard_concreteness_rejects_placeholders_and_transition_padding() -> None:
    segment_id = str(uuid4())
    errors = _scene_concreteness_errors(
        [
            {
                "purpose": "Erfüllt die redaktionelle Rolle.",
                "visual_brief": "Generic visual",
                "on_screen_text": [],
                "narration_segment_ids": [segment_id],
                "visual_type": "branded_transition",
                "duration": 20,
            }
        ],
        {segment_id: "Lithiumgewinnung benötigt konkrete Prozessschritte und Messwerte."},
    )
    assert "scene[0]: purpose or visual brief is a generic placeholder" in errors
    assert "scene[0]: at least one concrete on-screen text cue is required" in errors
    assert "branded transitions exceed 15 percent of storyboard duration" in errors


def test_local_quality_gate_enforces_segment_roles_and_uncertainty_text() -> None:
    approved = claim()
    second = claim()
    checked = {
        "draft": {
            "segments": [
                {
                    "segment_key": "02-thesis",
                    "segment_type": "thesis",
                    "annotations": [
                        {
                            "text": approved["normalized_statement"],
                            "kind": "fact",
                            "claim_ids": [approved["id"]],
                        },
                        {
                            "text": second["normalized_statement"],
                            "kind": "fact",
                            "claim_ids": [second["id"]],
                        },
                    ],
                },
                {
                    "segment_key": "06-uncertainty",
                    "segment_type": "uncertainty",
                    "annotations": [
                        {
                            "text": "A loose paraphrase of the approved record.",
                            "kind": "inference",
                            "claim_ids": [approved["id"]],
                        },
                        {
                            "text": "Was uns fehlt, ist eine eindeutige Antwort.",
                            "kind": "editorial",
                            "claim_ids": [],
                        },
                    ],
                },
            ]
        }
    }

    issues = _local_script_quality_issues(
        checked,
        {"structured_inputs": {"claims": [approved, second]}},
    )

    assert {issue["code"] for issue in issues} == {
        "excessive_claim_restatement",
        "uncertainty_requires_exact_claim",
        "unsupported_source_gap",
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


def test_semantic_script_is_assembled_with_exact_offsets_and_bound_evidence() -> None:
    claim_id = "11111111-1111-1111-1111-111111111111"
    evidence_id = "22222222-2222-2222-2222-222222222222"
    segment_types = [
        "hook", "thesis", "context", "evidence", "counterevidence",
        "uncertainty", "conclusion", "call_to_action",
    ]
    draft = _assemble_script_draft(
        {
            "content_draft": {
                "segments": [
                    {
                        "segment_type": segment_type,
                        "narration": f"Satz für {segment_type}.",
                        "presentation_purpose": "Verständlich einordnen.",
                        "claim_ids": [claim_id] if segment_type != "call_to_action" else [],
                        "evidence_excerpt_ids": [evidence_id] if segment_type != "call_to_action" else [],
                    }
                    for segment_type in segment_types
                ]
            },
            "default_title": "Fallback-Titel",
            "approved_claim_ids": [claim_id],
            "evidence_claim_ids_by_id": {evidence_id: [claim_id]},
        }
    )

    assert draft.title == "Fallback-Titel"
    assert [segment.segment_type for segment in draft.segments] == segment_types
    first = draft.segments[0]
    assert first.annotations[0].text == first.narration
    assert first.annotations[0].start_offset == 0
    assert first.annotations[0].end_offset == len(first.narration)
    assert str(first.annotations[0].evidence_excerpt_id) == evidence_id
    assert draft.segments[-1].annotations[0].kind == "editorial"


def test_sentence_level_script_preserves_editorial_and_factual_kinds() -> None:
    claim_id = "11111111-1111-1111-1111-111111111111"
    evidence_id = "22222222-2222-2222-2222-222222222222"
    segment_types = [
        "hook", "thesis", "context", "evidence", "counterevidence",
        "uncertainty", "conclusion", "call_to_action",
    ]
    draft = _assemble_script_draft(
        {
            "content_draft": {
                "segments": [
                    {
                        "segment_type": segment_type,
                        "presentation_purpose": "Schrittweise erklären.",
                        "sentences": [
                            {"text": "Was bedeutet das?", "kind": "editorial", "claim_ids": [], "evidence_excerpt_ids": []},
                            {"text": "Der freigegebene Beleg stützt diesen Satz.", "kind": "fact", "claim_ids": [claim_id], "evidence_excerpt_ids": [evidence_id]},
                        ],
                    }
                    for segment_type in segment_types
                ]
            },
            "default_title": "Satzgenau",
            "approved_claim_ids": [claim_id],
            "evidence_claim_ids_by_id": {evidence_id: [claim_id]},
        }
    )

    assert draft.segments[0].narration == "Was bedeutet das? Der freigegebene Beleg stützt diesen Satz."
    assert [item.kind for item in draft.segments[0].annotations] == ["editorial", "fact"]
    assert draft.segments[0].annotations[1].start_offset == len("Was bedeutet das? ")


def test_sentence_level_script_downgrades_invalid_model_bindings() -> None:
    segment_types = [
        "hook", "thesis", "context", "evidence", "counterevidence",
        "uncertainty", "conclusion", "call_to_action",
    ]
    draft = _assemble_script_draft(
        {
            "content_draft": {
                "segments": [
                    {
                        "segment_type": segment_type,
                        "presentation_purpose": "Sicher normalisieren.",
                        "sentences": [{
                            "text": "Dieser Satz hat keine gültige Belegbindung.",
                            "kind": "fact",
                            "claim_ids": [],
                            "evidence_excerpt_ids": ["not-a-uuid-or-known-evidence"],
                        }],
                    }
                    for segment_type in segment_types
                ]
            },
            "default_title": "Normalisierung",
            "approved_claim_ids": [],
            "evidence_claim_ids_by_id": {},
        }
    )

    annotation = draft.segments[0].annotations[0]
    assert annotation.kind == "editorial"
    assert annotation.claim_ids == []
    assert annotation.evidence_excerpt_id is None


def test_semantic_storyboard_is_assembled_one_scene_per_segment() -> None:
    segment_ids = [str(uuid4()), str(uuid4())]
    claim_id = str(uuid4())
    source_id = str(uuid4())
    draft = _assemble_storyboard_draft(
        {
            "content_draft": {
                "scenes": [
                    {
                        "narration_segment_id": segment_id,
                        "purpose": "Den Schritt sichtbar machen.",
                        "visual_type": "diagram",
                        "visual_brief": "Eine klare beschriftete Prozessgrafik.",
                        "on_screen_text": [f"Schritt {index}"],
                    }
                    for index, segment_id in enumerate(segment_ids, start=1)
                ]
            },
            "expected_segment_ids": segment_ids,
            "segment_durations": {segment_id: 12.0 for segment_id in segment_ids},
            "allowed_claim_ids": {segment_ids[0]: [claim_id], segment_ids[1]: []},
            "allowed_source_ids": {segment_ids[0]: [source_id], segment_ids[1]: []},
        }
    )

    assert [scene["order"] for scene in draft.scenes] == [1, 2]
    assert draft.scenes[0]["claim_ids"] == [claim_id]
    assert draft.scenes[0]["source_ids"] == [source_id]
    assert draft.scenes[1]["narration_segment_ids"] == [segment_ids[1]]
    assert [scene["visual_type"] for scene in draft.scenes] == [
        "title_card",
        "timeline",
    ]
    assert len({scene["visual_type"] for scene in draft.scenes}) == 2


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


async def test_full_import_replacement_can_retitle_the_new_version() -> None:
    approved = claim()
    generated = fake_output("script_writer", {"title": "Fixture", "claims": [approved]}, "fixture")

    merged = await merge_script_regeneration(
        {
            "current_draft": generated,
            "generated_draft": generated,
            "selected_segment_keys": [item["segment_key"] for item in generated["segments"]],
            "title": "Pasted author title",
        }
    )

    assert merged["draft"]["title"] == "Pasted author title"


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


async def test_semantic_scene_alternative_is_locally_assembled() -> None:
    segment_id = str(uuid4())
    inputs = {
        "script_version_id": str(uuid4()),
        "segments": [
            {
                "id": segment_id,
                "presentation_purpose": "Open the question",
                "narration": "What can we establish?",
                "duration_seconds": 5,
                "annotations": [],
            }
        ],
    }
    current = fake_output("storyboard", inputs, "storyboard")
    base = current["scenes"][0]
    base_hash = hashlib.sha256(
        json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    result = await validate_scene_alternative(
        {
            "generated_draft": {
                "scenes": [
                    {
                        "narration_segment_id": segment_id,
                        "purpose": "Open with a focused title card.",
                        "visual_type": "diagram",
                        "visual_brief": "A clear typographic question.",
                        "on_screen_text": ["What can we establish?"],
                    }
                ]
            },
            "scene_id": base["scene_id"],
            "scene_order": 1,
            "base_scene_hash": base_hash,
            "current_scenes": current["scenes"],
            "expected_segment_ids": [segment_id],
            "allowed_claim_ids": {segment_id: []},
            "allowed_source_ids": {segment_id: []},
            "instruction": "Use visual_type exakt title_card.",
        }
    )

    assert result["scene_spec"]["visual_type"] == "title_card"
    assert result["scene_spec"]["duration"] == base["duration"]
    assert result["scene_spec"]["narration_segment_ids"] == [segment_id]
