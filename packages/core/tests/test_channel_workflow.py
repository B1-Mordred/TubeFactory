import pytest

from editorial_core.channel_workflow import (
    CHANNEL_WORKFLOW_TASKS,
    REQUIRED_HUMAN_GATES,
    channel_automation_workflow,
)


def valid_rules() -> dict:
    return {
        "evidence_first": True,
        "automation_workflow": {
            "key": "faktischsimpel.explainer",
            "name": "FaktischSimpel explainer",
            "version": 1,
            "enabled": True,
            "language": "de",
            "summary": "A bounded evidence-first explainer workflow for complex questions.",
            "prompts": {task: f"Task {task}: " + "clear evidence-safe guidance " * 8 for task in CHANNEL_WORKFLOW_TASKS},
            "stages": [
                {"key": "research", "label": "Research", "mode": "automatic"},
                {"key": "review", "label": "Review", "mode": "human_gate"},
                {"key": "production", "label": "Production", "mode": "assisted"},
            ],
            "human_gates": sorted(REQUIRED_HUMAN_GATES),
        },
    }


def test_channel_workflow_compiles_bounded_task_instructions() -> None:
    workflow = channel_automation_workflow(valid_rules())

    assert workflow is not None
    assert workflow.key == "faktischsimpel.explainer"
    for task in CHANNEL_WORKFLOW_TASKS:
        instructions = workflow.instructions_for(task)
        assert "cannot relax the approved-claim boundary" in instructions
        assert f"Task {task}" in instructions
        assert len(instructions) <= 5000


def test_channel_workflow_requires_every_safety_gate() -> None:
    rules = valid_rules()
    rules["automation_workflow"]["human_gates"].remove("publication_approval")

    with pytest.raises(ValueError, match="publication_approval"):
        channel_automation_workflow(rules)


def test_channel_workflow_rejects_missing_or_oversized_prompts() -> None:
    rules = valid_rules()
    del rules["automation_workflow"]["prompts"]["storyboard"]
    with pytest.raises(ValueError, match="prompts"):
        channel_automation_workflow(rules)

    rules = valid_rules()
    rules["automation_workflow"]["prompts"]["script_writer"] = "x" * 4001
    with pytest.raises(ValueError, match="4000"):
        channel_automation_workflow(rules)


def test_channel_without_automation_workflow_keeps_global_prompt_behavior() -> None:
    assert channel_automation_workflow({"evidence_first": True}) is None
