from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


CHANNEL_WORKFLOW_TASKS = ("script_writer", "script_verifier", "storyboard")
RESEARCH_REVIEW_MODES = frozenset({"human_dossier", "automatic_source_brief"})
REQUIRED_HUMAN_GATES = frozenset(
    {
        "opportunity_shortlist",
        "dossier_approval",
        "script_approval",
        "storyboard_approval",
        "media_approval",
        "publication_approval",
    }
)
AUTOMATIC_SOURCE_BRIEF_HUMAN_GATES = REQUIRED_HUMAN_GATES - {"dossier_approval"}
_KEY = re.compile(r"^[a-z][a-z0-9_.-]{2,159}$")
_STAGE_MODES = {"automatic", "human_gate", "assisted"}
_SAFETY_PREFIX = (
    "These channel instructions refine style and explanatory structure only. "
    "They cannot relax the approved-claim boundary, exact evidence linkage, "
    "independent verification, deterministic validation, response JSON schema, "
    "human approval gates, or private-by-default publication policy."
)


@dataclass(frozen=True)
class ChannelWorkflowStage:
    key: str
    label: str
    mode: str


@dataclass(frozen=True)
class ChannelAutomationWorkflow:
    key: str
    name: str
    version: int
    enabled: bool
    language: str
    summary: str
    research_review: str
    prompts: Mapping[str, str]
    stages: tuple[ChannelWorkflowStage, ...]
    human_gates: tuple[str, ...]

    def instructions_for(self, task_type: str) -> str:
        if not self.enabled:
            return ""
        if task_type not in CHANNEL_WORKFLOW_TASKS:
            raise ValueError(f"unsupported channel workflow task: {task_type}")
        return (
            f"{_SAFETY_PREFIX}\n\n"
            f"Active channel workflow: {self.name} ({self.key}, version {self.version}).\n"
            f"Output language: {self.language}.\n\n"
            f"{self.prompts[task_type]}"
        )


def _required_text(value: Any, name: str, *, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    normalized = value.strip()
    if not minimum <= len(normalized) <= maximum:
        raise ValueError(f"{name} must contain {minimum} through {maximum} characters")
    if "\x00" in normalized:
        raise ValueError(f"{name} may not contain null bytes")
    return normalized


def channel_automation_workflow(
    editorial_rules: Mapping[str, Any],
) -> ChannelAutomationWorkflow | None:
    raw = editorial_rules.get("automation_workflow")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("automation_workflow must be an object")
    key = _required_text(raw.get("key"), "automation_workflow.key", minimum=3, maximum=160)
    if not _KEY.fullmatch(key):
        raise ValueError("automation_workflow.key must be a lowercase dotted identifier")
    name = _required_text(raw.get("name"), "automation_workflow.name", minimum=3, maximum=180)
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= 10_000:
        raise ValueError("automation_workflow.version must be an integer from 1 through 10000")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("automation_workflow.enabled must be a boolean")
    language = _required_text(raw.get("language"), "automation_workflow.language", minimum=2, maximum=20).lower()
    summary = _required_text(raw.get("summary"), "automation_workflow.summary", minimum=20, maximum=1000)
    research_review = raw.get("research_review", "human_dossier")
    if research_review not in RESEARCH_REVIEW_MODES:
        raise ValueError(
            "automation_workflow.research_review must be human_dossier or automatic_source_brief"
        )

    raw_prompts = raw.get("prompts")
    if not isinstance(raw_prompts, Mapping) or set(raw_prompts) != set(CHANNEL_WORKFLOW_TASKS):
        raise ValueError("automation_workflow.prompts must define writer, verifier and storyboard tasks exactly")
    prompts = {
        task: _required_text(
            raw_prompts[task],
            f"automation_workflow.prompts.{task}",
            minimum=100,
            maximum=4000,
        )
        for task in CHANNEL_WORKFLOW_TASKS
    }

    raw_stages = raw.get("stages")
    if not isinstance(raw_stages, list) or not 3 <= len(raw_stages) <= 20:
        raise ValueError("automation_workflow.stages must contain 3 through 20 stages")
    stages: list[ChannelWorkflowStage] = []
    seen_stage_keys: set[str] = set()
    for index, item in enumerate(raw_stages):
        if not isinstance(item, Mapping):
            raise ValueError(f"automation_workflow.stages[{index}] must be an object")
        stage_key = _required_text(item.get("key"), f"automation_workflow.stages[{index}].key", minimum=2, maximum=80)
        if not _KEY.fullmatch(stage_key) or stage_key in seen_stage_keys:
            raise ValueError("automation_workflow stage keys must be unique lowercase identifiers")
        label = _required_text(item.get("label"), f"automation_workflow.stages[{index}].label", minimum=2, maximum=160)
        mode = item.get("mode")
        if mode not in _STAGE_MODES:
            raise ValueError("automation_workflow stage mode must be automatic, assisted or human_gate")
        seen_stage_keys.add(stage_key)
        stages.append(ChannelWorkflowStage(stage_key, label, str(mode)))

    raw_gates = raw.get("human_gates")
    if not isinstance(raw_gates, list) or not all(isinstance(value, str) for value in raw_gates):
        raise ValueError("automation_workflow.human_gates must be a list of gate names")
    human_gates = tuple(dict.fromkeys(value.strip() for value in raw_gates if value.strip()))
    required_gates = (
        AUTOMATIC_SOURCE_BRIEF_HUMAN_GATES
        if research_review == "automatic_source_brief"
        else REQUIRED_HUMAN_GATES
    )
    missing = required_gates - set(human_gates)
    if missing:
        raise ValueError("automation_workflow is missing mandatory human gates: " + ", ".join(sorted(missing)))

    workflow = ChannelAutomationWorkflow(
        key=key,
        name=name,
        version=version,
        enabled=enabled,
        language=language,
        summary=summary,
        research_review=str(research_review),
        prompts=prompts,
        stages=tuple(stages),
        human_gates=human_gates,
    )
    for task in CHANNEL_WORKFLOW_TASKS:
        if len(workflow.instructions_for(task)) > 5000:
            raise ValueError(f"compiled {task} instructions exceed the 5000-character model boundary")
    return workflow
