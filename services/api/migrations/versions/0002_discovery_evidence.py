"""discovery profiles sources evidence and dossiers

Revision ID: 0002_discovery_evidence
Revises: 0001_foundation
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_discovery_evidence"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSON = postgresql.JSONB(astext_type=sa.Text())
UTC = sa.DateTime(timezone=True)


def _identity_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("id", UUID, primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("updated_at", UTC, nullable=False),
        sa.Column("deleted_at", UTC, nullable=True),
    )


def upgrade() -> None:
    op.create_table(
        "channel_profiles",
        *_identity_columns(),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("identity", JSON, nullable=False),
        sa.Column("languages", JSON, nullable=False),
        sa.Column("audience", JSON, nullable=False),
        sa.Column("editorial_rules", JSON, nullable=False),
        sa.Column("brand_kit", JSON, nullable=False),
        sa.Column("default_render_settings", JSON, nullable=False),
        sa.Column("default_publish_settings", JSON, nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("slug", name="uq_channel_profiles_slug"),
        sa.CheckConstraint("version >= 1", name="ck_channel_profiles_version"),
    )

    op.create_table(
        "subject_profiles",
        *_identity_columns(),
        sa.Column("channel_profile_id", UUID, nullable=False),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("research_goal", sa.Text(), nullable=False),
        sa.Column("excluded_angles", JSON, nullable=False),
        sa.Column("seed_queries", JSON, nullable=False),
        sa.Column("related_concepts", JSON, nullable=False),
        sa.Column("negative_keywords", JSON, nullable=False),
        sa.Column("languages", JSON, nullable=False),
        sa.Column("regions", JSON, nullable=False),
        sa.Column("domain_policy", JSON, nullable=False),
        sa.Column("source_requirements", JSON, nullable=False),
        sa.Column("schedule", JSON, nullable=False),
        sa.Column("freshness_policy", JSON, nullable=False),
        sa.Column("format_policy", JSON, nullable=False),
        sa.Column("editorial_profile", JSON, nullable=False),
        sa.Column("risk", sa.String(20), nullable=False),
        sa.Column("budget", JSON, nullable=False),
        sa.Column("opportunity_weights", JSON, nullable=False),
        sa.Column("approval_profile", JSON, nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("channel_profile_id", "name", name="uq_subject_channel_name"),
        sa.CheckConstraint("risk IN ('low','medium','high')", name="ck_subject_profiles_risk"),
        sa.CheckConstraint("version >= 1", name="ck_subject_profiles_version"),
    )

    op.create_table(
        "opportunities",
        *_identity_columns(),
        sa.Column("subject_profile_id", UUID, nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("grouping_reason", JSON, nullable=False),
        sa.Column("duplicate_of_id", UUID, nullable=True),
        sa.Column("estimated_cost", JSON, nullable=False),
        sa.Column("decided_by", UUID, nullable=True),
        sa.Column("decided_at", UTC, nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["subject_profile_id"], ["subject_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["duplicate_of_id"], ["opportunities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("decision IN ('pending','approved','rejected','deferred')", name="ck_opportunities_decision"),
        sa.CheckConstraint("version >= 1", name="ck_opportunities_version"),
    )

    op.create_table(
        "opportunity_scores",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("opportunity_id", UUID, nullable=False),
        sa.Column("score_version", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("positive_components", JSON, nullable=False),
        sa.Column("penalties", JSON, nullable=False),
        sa.Column("weights", JSON, nullable=False),
        sa.Column("reasoning", JSON, nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("opportunity_id", "score_version", name="uq_opportunity_score_version"),
        sa.CheckConstraint("total BETWEEN 0 AND 100", name="ck_opportunity_score_total"),
    )

    op.create_table(
        "research_runs",
        *_identity_columns(),
        sa.Column("opportunity_id", UUID, nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("research_plan", JSON, nullable=False),
        sa.Column("progress", JSON, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("started_by", UUID, nullable=False),
        sa.Column("completed_at", UTC, nullable=True),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["started_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("workflow_id", name="uq_research_runs_workflow_id"),
        sa.CheckConstraint("version >= 1", name="ck_research_runs_version"),
    )

    op.create_table(
        "source_documents",
        *_identity_columns(),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author", sa.String(300), nullable=True),
        sa.Column("publisher", sa.String(300), nullable=True),
        sa.Column("source_type", sa.String(60), nullable=False),
        sa.Column("publication_at", UTC, nullable=True),
        sa.Column("event_at", UTC, nullable=True),
        sa.Column("reputation", JSON, nullable=False),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.UniqueConstraint("canonical_url", name="uq_source_documents_canonical_url"),
        sa.CheckConstraint("version >= 1", name="ck_source_documents_version"),
    )

    op.create_table(
        "source_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("source_document_id", UUID, nullable=False),
        sa.Column("snapshot_number", sa.Integer(), nullable=False),
        sa.Column("raw_object_key", sa.String(1024), nullable=False),
        sa.Column("normalized_object_key", sa.String(1024), nullable=False),
        sa.Column("screenshot_object_key", sa.String(1024), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("retrieved_at", UTC, nullable=False),
        sa.Column("redirect_chain", JSON, nullable=False),
        sa.Column("extraction_metadata", JSON, nullable=False),
        sa.Column("injection_markers", JSON, nullable=False),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("source_document_id", "snapshot_number", name="uq_source_snapshot_number"),
        sa.UniqueConstraint("source_document_id", "content_hash", name="uq_source_snapshot_hash"),
        sa.CheckConstraint("snapshot_number >= 1", name="ck_source_snapshot_number"),
        sa.CheckConstraint("byte_size >= 0", name="ck_source_snapshot_byte_size"),
    )

    op.create_table(
        "evidence_excerpts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("source_snapshot_id", UUID, nullable=False),
        sa.Column("exact_text", sa.Text(), nullable=False),
        sa.Column("prefix_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("suffix_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("location_anchor", sa.String(500), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column("excerpt_hash", sa.String(64), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["source_snapshot_id"], ["source_snapshots.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("source_snapshot_id", "excerpt_hash", "location_anchor", name="uq_evidence_excerpt_anchor"),
    )

    op.create_table(
        "source_relationships",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("source_document_id", UUID, nullable=False),
        sa.Column("related_source_document_id", UUID, nullable=False),
        sa.Column("relationship", sa.String(40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_source_document_id"], ["source_documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_document_id", "related_source_document_id", "relationship", name="uq_source_relationship"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 100", name="ck_source_relationship_confidence"),
        sa.CheckConstraint("source_document_id <> related_source_document_id", name="ck_source_relationship_distinct"),
    )

    op.create_table(
        "research_dossiers",
        *_identity_columns(),
        sa.Column("opportunity_id", UUID, nullable=False),
        sa.Column("dossier_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("executive_summary", sa.Text(), nullable=False),
        sa.Column("chronology", JSON, nullable=False),
        sa.Column("unresolved_questions", JSON, nullable=False),
        sa.Column("alternative_explanations", JSON, nullable=False),
        sa.Column("source_quality_notes", JSON, nullable=False),
        sa.Column("safe_conclusions", JSON, nullable=False),
        sa.Column("prohibited_overstatements", JSON, nullable=False),
        sa.Column("proposed_angles", JSON, nullable=False),
        sa.Column("completion_evaluation", JSON, nullable=False),
        sa.Column("reviewed_by", UUID, nullable=True),
        sa.Column("reviewed_at", UTC, nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("opportunity_id", "dossier_version", name="uq_dossier_version"),
        sa.CheckConstraint("status IN ('draft','blocked','in_review','approved','rejected')", name="ck_dossier_status"),
        sa.CheckConstraint("version >= 1 AND dossier_version >= 1", name="ck_dossier_versions"),
    )

    op.create_table(
        "claims",
        *_identity_columns(),
        sa.Column("research_dossier_id", UUID, nullable=False),
        sa.Column("normalized_statement", sa.Text(), nullable=False),
        sa.Column("claim_type", sa.String(20), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("relevant_at", UTC, nullable=True),
        sa.Column("entities", JSON, nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("risk", sa.String(20), nullable=False),
        sa.Column("central", sa.Boolean(), nullable=False),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("reviewed_by", UUID, nullable=True),
        sa.Column("reviewed_at", UTC, nullable=True),
        sa.ForeignKeyConstraint(["research_dossier_id"], ["research_dossiers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("claim_type IN ('fact','inference','opinion')", name="ck_claims_type"),
        sa.CheckConstraint("status IN ('draft','supported','disputed','approved','rejected')", name="ck_claims_status"),
        sa.CheckConstraint("risk IN ('low','medium','high')", name="ck_claims_risk"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 100", name="ck_claims_confidence"),
        sa.CheckConstraint("version >= 1", name="ck_claims_version"),
    )

    op.create_table(
        "claim_evidence",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("claim_id", UUID, nullable=False),
        sa.Column("evidence_excerpt_id", UUID, nullable=False),
        sa.Column("relationship", sa.String(20), nullable=False),
        sa.Column("source_independent", sa.Boolean(), nullable=False),
        sa.Column("direct_evidence", sa.Boolean(), nullable=False),
        sa.Column("primary_source", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["evidence_excerpt_id"], ["evidence_excerpts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("claim_id", "evidence_excerpt_id", "relationship", name="uq_claim_evidence_relation"),
        sa.CheckConstraint("relationship IN ('supports','contradicts','context')", name="ck_claim_evidence_relationship"),
    )

    op.create_table(
        "workflow_transitions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("aggregate_type", sa.String(80), nullable=False),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("from_stage", sa.String(50), nullable=True),
        sa.Column("to_stage", sa.String(50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", UUID, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("occurred_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_workflow_transition_aggregate", "workflow_transitions", ["aggregate_type", "aggregate_id", "occurred_at"])

    op.execute("CREATE TRIGGER source_snapshots_immutable BEFORE UPDATE OR DELETE ON source_snapshots FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()")
    op.execute("CREATE TRIGGER opportunity_scores_immutable BEFORE UPDATE OR DELETE ON opportunity_scores FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()")
    op.execute("CREATE TRIGGER evidence_excerpts_immutable BEFORE UPDATE OR DELETE ON evidence_excerpts FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()")
    op.execute("CREATE TRIGGER claim_evidence_immutable BEFORE UPDATE OR DELETE ON claim_evidence FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()")
    op.execute("CREATE TRIGGER workflow_transitions_immutable BEFORE UPDATE OR DELETE ON workflow_transitions FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()")


def downgrade() -> None:
    for table in ("workflow_transitions", "claim_evidence", "evidence_excerpts", "opportunity_scores", "source_snapshots"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.drop_index("ix_workflow_transition_aggregate", table_name="workflow_transitions")
    for table in (
        "workflow_transitions", "claim_evidence", "claims", "research_dossiers",
        "source_relationships", "evidence_excerpts", "source_snapshots", "source_documents",
        "research_runs", "opportunity_scores", "opportunities", "subject_profiles", "channel_profiles",
    ):
        op.drop_table(table)
