import pytest

from editorial_core.media import (
    MediaContractError,
    QAFinding,
    QAOverridePolicy,
    QAVerdict,
    TypedWorkflowInput,
    effective_qa_verdict,
    media_cache_key,
    render_is_approvable,
    stable_voice_chunks,
    substitute_workflow_inputs,
    validate_comfy_api_workflow,
)


WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "model": ["4", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "safe.safetensors"}},
}
SPECS = (TypedWorkflowInput("seed", "3", "seed", "seed"),)


def test_workflow_registry_rejects_ui_format_and_inventory_escape():
    assert "not UI graph" in validate_comfy_api_workflow(
        {"nodes": []}, typed_inputs=(), allowed_node_types=(), allowed_models=()
    )[0]
    assert validate_comfy_api_workflow(
        WORKFLOW,
        typed_inputs=SPECS,
        allowed_node_types={"KSampler", "CheckpointLoaderSimple"},
        allowed_models={"safe.safetensors"},
    ) == ()
    errors = validate_comfy_api_workflow(
        WORKFLOW, typed_inputs=SPECS, allowed_node_types={"KSampler"}, allowed_models=set()
    )
    assert any("unapproved type" in error for error in errors)
    assert any("unapproved model" in error for error in errors)


def test_only_declared_typed_inputs_can_change():
    changed = substitute_workflow_inputs(WORKFLOW, SPECS, {"seed": 99})
    assert changed["3"]["inputs"]["seed"] == 99
    assert WORKFLOW["3"]["inputs"]["seed"] == 1
    with pytest.raises(MediaContractError, match="undeclared"):
        substitute_workflow_inputs(WORKFLOW, SPECS, {"prompt": "escape"})
    with pytest.raises(MediaContractError, match="must be seed"):
        substitute_workflow_inputs(WORKFLOW, SPECS, {"seed": "99"})


def test_media_cache_key_binds_capabilities_and_workflow():
    a = "a" * 64
    b = "b" * 64
    assert media_cache_key(workflow_hash=a, inputs={"seed": 1}, capability_hash=b) == media_cache_key(
        workflow_hash=a, inputs={"seed": 1}, capability_hash=b
    )
    assert media_cache_key(workflow_hash=a, inputs={"seed": 2}, capability_hash=b) != media_cache_key(
        workflow_hash=a, inputs={"seed": 1}, capability_hash=b
    )


def test_voice_chunking_is_stable_and_bounded():
    assert stable_voice_chunks("one two three four", maximum_characters=8) == ("one two", "three", "four")


def test_non_overridable_qa_failure_blocks_and_reasoned_warning_can_clear():
    findings = (
        QAFinding("corrupt_media", QAVerdict.FAIL, "decode failed", QAOverridePolicy.NEVER),
        QAFinding("loudness", QAVerdict.WARN, "outside target"),
    )
    assert effective_qa_verdict(findings) is QAVerdict.FAIL
    with pytest.raises(MediaContractError, match="cannot be overridden"):
        effective_qa_verdict(findings, overridden_codes={"corrupt_media"})
    assert effective_qa_verdict(findings[1:], overridden_codes={"loudness"}) is QAVerdict.PASS


def test_approval_binds_exact_storyboard_manifest_and_nonfailing_qa():
    h = "a" * 64
    m = "b" * 64
    assert render_is_approvable(
        storyboard_hash=h,
        bound_storyboard_hash=h,
        manifest_hash=m,
        bound_manifest_hash=m,
        qa_verdict=QAVerdict.WARN,
    )
    assert not render_is_approvable(
        storyboard_hash=h,
        bound_storyboard_hash="c" * 64,
        manifest_hash=m,
        bound_manifest_hash=m,
        qa_verdict=QAVerdict.PASS,
    )
