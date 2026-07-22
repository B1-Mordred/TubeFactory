import json
from pathlib import Path
from uuid import uuid4

from editorial_core.channel_workflow import channel_automation_workflow
from youtuber_api.schemas import ChannelProfileWrite, SubjectProfileWrite


ROOT = Path(__file__).resolve().parents[3]


def test_faktischsimpel_blueprint_matches_channel_and_subject_contracts() -> None:
    blueprint = json.loads(
        (ROOT / "config" / "channel-workflows" / "faktischsimpel.json").read_text()
    )
    channel = ChannelProfileWrite(
        slug="faktischsimpel",
        name="FaktischSimpel",
        enabled=True,
        identity={"description": "Komplexe Themen verständlich erklärt."},
        languages=["de"],
        **blueprint["channel"],
    )
    subject = SubjectProfileWrite(
        channel_profile_id=uuid4(),
        **blueprint["subject"],
    )
    workflow = channel_automation_workflow(channel.editorial_rules)

    assert workflow is not None and workflow.enabled
    assert workflow.key == "faktischsimpel.simple_explainer"
    assert workflow.research_review == "automatic_source_brief"
    assert "dossier_approval" not in workflow.human_gates
    assert workflow.language == "de"
    assert subject.enabled is True
    assert subject.schedule.timezone == "Europe/Berlin"
    assert subject.source_requirements["minimum_independent"] == 2
    assert subject.approval_profile["mode"] == "supervised"
    assert subject.format_policy["minimum_duration_seconds"] == 300
    assert subject.format_policy["maximum_duration_seconds"] == 600
    assert subject.format_policy["minimum_coverage_units"] == 5
    assert subject.approval_profile["evidence_density_minimum"] == 0.8
