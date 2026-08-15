from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import inspect

from youtuber_api.db import Base
from youtuber_api.models import SourceSnapshotModel
import pytest
from pydantic import ValidationError

from youtuber_api.schemas import (
    ChannelProfileWrite,
    FixtureResearchStart,
    ManualDossierClaimWrite,
    ManualDossierWrite,
    ManualOpportunityWrite,
    OpportunityListItem,
    SourceRelationshipWrite,
    SubjectProfileWrite,
)


def test_opportunity_list_normalizes_legacy_grouping_reason_objects() -> None:
    item = OpportunityListItem(
        id=uuid4(),
        version=1,
        subject_profile_id=uuid4(),
        title="Legacy acceptance opportunity",
        summary="An existing row created by an earlier acceptance fixture.",
        editorial_rationale="Retain the historical evidence while presenting a stable API shape.",
        policy_snapshot={},
        decision="pending",
        grouping_reason={"acceptance_gate": "automatic_near_duplicate"},
        estimated_cost={},
        score=None,
        score_components={},
        score_penalties={},
        score_reasoning=[],
        source_count=0,
        snapshot_count=0,
        created_at=datetime.now(timezone.utc),
    )
    assert item.grouping_reason == ["acceptance_gate: automatic_near_duplicate"]


def test_discovery_models_are_registered() -> None:
    required = {
        "channel_profiles",
        "subject_profiles",
        "opportunities",
        "opportunity_scores",
        "research_runs",
        "source_documents",
        "source_snapshots",
        "evidence_excerpts",
        "source_relationships",
        "research_dossiers",
        "claims",
        "claim_evidence",
        "workflow_transitions",
        "opportunity_sources",
        "semantic_chunks",
        "workflow_control_records",
        "providers",
        "models",
        "task_model_assignments",
        "prompt_templates",
        "ai_usage_records",
        "production_briefs",
        "scripts",
        "script_versions",
        "script_segments",
        "segment_claims",
        "storyboards",
        "storyboard_versions",
        "scenes",
        "scene_versions",
        "approvals",
        "comfy_workflow_versions",
        "voice_profile_versions",
        "media_productions",
        "media_assets",
        "narration_segments",
        "production_manifests",
        "production_renders",
        "qa_reports",
        "qa_findings",
        "qa_overrides",
        "youtube_connections",
        "youtube_oauth_states",
        "publishing_configuration_versions",
        "publish_metadata_versions",
        "publication_approvals",
        "publications",
        "publication_schedules",
    }
    assert required <= set(Base.metadata.tables)
    assert inspect(SourceSnapshotModel).primary_key[0].name == "id"


def test_profile_schemas_default_disabled_and_high_risk_rules() -> None:
    channel = ChannelProfileWrite(slug="test-channel", name="Test Channel", languages=["en"])
    assert channel.enabled is False
    assert channel.brand_kit["logo_text"] == "TC"
    assert channel.brand_kit["schema_version"] == 1
    subject = SubjectProfileWrite(
        channel_profile_id="21cb77e7-4de6-44f6-8987-a5fe83b84eb9",
        name="Test subject",
        topic="A testable claim",
        research_goal="Find primary evidence",
        seed_queries=["testable claim evidence"],
        risk="high",
    )
    assert subject.enabled is False
    assert subject.source_requirements["minimum_independent"] == 2
    assert subject.source_requirements["minimum_primary"] == 1
    assert subject.approval_profile["mode"] == "assisted"
    assert subject.approval_profile["repeated_scene_limit"] == 1


def test_operating_profile_and_manual_opportunity_are_strict() -> None:
    subject = SubjectProfileWrite(
        channel_profile_id="21cb77e7-4de6-44f6-8987-a5fe83b84eb9",
        name="Sensitive subject", topic="Health policy", research_goal="Compare primary records",
        seed_queries=["health policy primary record"], risk="high",
        approval_profile={"mode": "trusted", "sensitive_topics": ["health"]},
    )
    assert subject.approval_profile["mode"] == "trusted"
    with pytest.raises(ValidationError, match="unknown sensitive"):
        SubjectProfileWrite(
            channel_profile_id="21cb77e7-4de6-44f6-8987-a5fe83b84eb9",
            name="Bad policy", topic="A topic", research_goal="A research goal",
            seed_queries=["query"], approval_profile={"sensitive_topics": ["invented"]},
        )
    item = ManualOpportunityWrite(
        subject_profile_id="21cb77e7-4de6-44f6-8987-a5fe83b84eb9",
        title="A traceable idea", summary="A sufficiently detailed evidence-led editorial summary.",
        editorial_rationale="It closes a documented information gap with primary evidence.",
    )
    assert item.estimated_cost["tokens"] == 0


def test_write_schemas_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FixtureResearchStart.model_validate(
            {
                "subject_profile_id": "21cb77e7-4de6-44f6-8987-a5fe83b84eb9",
                "idempotency_key": "valid-key",
                "unexpected": "ignored input must be rejected",
            }
        )


def test_subject_schedule_rejects_unknown_timezones() -> None:
    with pytest.raises(ValidationError, match="IANA timezone"):
        SubjectProfileWrite(
            channel_profile_id="21cb77e7-4de6-44f6-8987-a5fe83b84eb9",
            name="Test subject",
            topic="A testable claim",
            research_goal="Find primary evidence",
            seed_queries=["testable claim evidence"],
            schedule={"cron": "0 6 * * *", "timezone": "Mars/Olympus_Mons"},
        )


def test_source_relationship_endpoints_must_be_distinct() -> None:
    with pytest.raises(ValidationError, match="must be distinct"):
        SourceRelationshipWrite(
            source_document_id="21cb77e7-4de6-44f6-8987-a5fe83b84eb9",
            related_source_document_id="21cb77e7-4de6-44f6-8987-a5fe83b84eb9",
            relationship="derived_from",
            reason="The article directly identifies the same original record.",
            confidence=90,
        )


def test_manual_dossier_schema_requires_reviewable_content_and_complete_evidence_pairs() -> None:
    with pytest.raises(ValidationError, match="source_snapshot_id and exact_text"):
        ManualDossierClaimWrite(
            statement="A source-bound claim needs its exact excerpt.",
            source_snapshot_id=uuid4(),
        )
    with pytest.raises(ValidationError, match="at least one safe conclusion"):
        ManualDossierWrite(
            expected_opportunity_version=1,
            idempotency_key="manual-test",
            executive_summary="A human reviewed recovery summary with enough detail.",
            safe_conclusions=["   "],
            review_note="Human reviewer checked failed research before dossier recovery.",
        )
    payload = ManualDossierWrite(
        expected_opportunity_version=1,
        idempotency_key="manual-test",
        executive_summary="A human reviewed recovery summary with enough detail.",
        safe_conclusions=["A source-supported conclusion is safe to review."],
        review_note="Human reviewer checked failed research before dossier recovery.",
        claims=[
            ManualDossierClaimWrite(
                statement="The source directly supports this manual recovery claim.",
                source_snapshot_id=uuid4(),
                exact_text="This is the exact source excerpt used for manual recovery.",
            )
        ],
    )
    assert payload.safe_conclusions == ["A source-supported conclusion is safe to review."]
