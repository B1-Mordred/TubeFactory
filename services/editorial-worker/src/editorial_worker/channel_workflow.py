from __future__ import annotations

import json
from typing import Any

from editorial_core.channel_workflow import (
    CHANNEL_WORKFLOW_TASKS,
    channel_automation_workflow,
)


def channel_workflow_context(editorial_rules: Any) -> dict[str, Any]:
    rules = json.loads(editorial_rules) if isinstance(editorial_rules, str) else editorial_rules
    workflow = channel_automation_workflow(rules or {})
    if workflow is None:
        return {
            "active": False,
            "key": None,
            "name": None,
            "version": None,
            "summary": None,
            "stages": [],
            "human_gates": [],
            "instructions": {},
        }
    return {
        "active": workflow.enabled,
        "key": workflow.key,
        "name": workflow.name,
        "version": workflow.version,
        "summary": workflow.summary,
        "stages": [
            {"key": stage.key, "label": stage.label, "mode": stage.mode}
            for stage in workflow.stages
        ],
        "human_gates": list(workflow.human_gates),
        "instructions": {
            task: workflow.instructions_for(task) for task in CHANNEL_WORKFLOW_TASKS
        },
    }
