"""analytics, policy gates, controlled recommendations and budgets

Revision ID: 0015_optimization_hardening
Revises: 0014_youtube_publishing
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0015_optimization_hardening"
down_revision = "0014_youtube_publishing"
branch_labels = None
depends_on = None


APPEND_ONLY = (
    "analytics_metric_snapshots", "model_benchmark_runs", "model_recommendations",
    "model_recommendation_decisions", "model_recommendation_applications",
    "source_freshness_checks", "originality_reports", "correction_records",
    "budget_policy_versions", "budget_usage_records",
)


def _immutable(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"
    )


def upgrade() -> None:
    op.create_table(
        "analytics_metric_snapshots",
        sa.Column("id", UUID, primary_key=True), sa.Column("channel_profile_id", UUID, nullable=False),
        sa.Column("publication_id", UUID, nullable=True), sa.Column("youtube_video_id", sa.String(160), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metrics", JSONB, nullable=False), sa.Column("dimensions", JSONB, nullable=False),
        sa.Column("source", sa.String(40), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("ingested_by", UUID, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ingested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("channel_profile_id", "youtube_video_id", "period_start", "period_end", "content_hash", name="uq_analytics_snapshot_content"),
        sa.CheckConstraint("period_end > period_start", name="ck_analytics_period"),
        sa.CheckConstraint("source IN ('youtube_analytics','manual_fixture')", name="ck_analytics_source"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_analytics_hash"),
    )
    op.create_index("ix_analytics_video_period", "analytics_metric_snapshots", ["youtube_video_id", "period_end"])
    op.create_table(
        "model_benchmark_runs",
        sa.Column("id", UUID, primary_key=True), sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("suite_key", sa.String(160), nullable=False), sa.Column("suite_version", sa.String(80), nullable=False),
        sa.Column("candidates", JSONB, nullable=False), sa.Column("weights", JSONB, nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False), sa.Column("baseline_assignment_id", UUID, nullable=True),
        sa.Column("created_by", UUID, nullable=False), sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["baseline_assignment_id"], ["task_model_assignments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("input_hash ~ '^[0-9a-f]{64}$'", name="ck_benchmark_hash"),
    )
    op.create_table(
        "model_recommendations",
        sa.Column("id", UUID, primary_key=True), sa.Column("benchmark_run_id", UUID, nullable=False, unique=True),
        sa.Column("task_type", sa.String(80), nullable=False), sa.Column("recommended_model_id", UUID, nullable=False),
        sa.Column("baseline_assignment_id", UUID, nullable=True), sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reasoning", JSONB, nullable=False), sa.Column("recommendation_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["benchmark_run_id"], ["model_benchmark_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recommended_model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["baseline_assignment_id"], ["task_model_assignments.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_recommendation_score"),
        sa.CheckConstraint("recommendation_hash ~ '^[0-9a-f]{64}$'", name="ck_recommendation_hash"),
    )
    op.create_table(
        "model_recommendation_decisions",
        sa.Column("id", UUID, primary_key=True), sa.Column("recommendation_id", UUID, nullable=False),
        sa.Column("decision", sa.String(20), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", UUID, nullable=False), sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["recommendation_id"], ["model_recommendations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("decision IN ('approved','rejected')", name="ck_recommendation_decision"),
    )
    op.create_table(
        "model_recommendation_applications",
        sa.Column("id", UUID, primary_key=True), sa.Column("recommendation_id", UUID, nullable=False, unique=True),
        sa.Column("decision_id", UUID, nullable=False), sa.Column("previous_assignment_id", UUID, nullable=True),
        sa.Column("new_assignment_id", UUID, nullable=False), sa.Column("actor_id", UUID, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["recommendation_id"], ["model_recommendations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decision_id"], ["model_recommendation_decisions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["previous_assignment_id"], ["task_model_assignments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["new_assignment_id"], ["task_model_assignments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_table(
        "source_freshness_checks",
        sa.Column("id", UUID, primary_key=True), sa.Column("source_snapshot_id", UUID, nullable=False),
        sa.Column("maximum_age_seconds", sa.BigInteger(), nullable=False), sa.Column("age_seconds", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False), sa.Column("reason", sa.String(160), nullable=False),
        sa.Column("policy", JSONB, nullable=False), sa.Column("checked_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_snapshot_id"], ["source_snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["checked_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("maximum_age_seconds > 0 AND age_seconds >= 0", name="ck_freshness_age"),
        sa.CheckConstraint("verdict IN ('pass','review','block')", name="ck_freshness_verdict"),
    )
    op.create_table(
        "originality_reports",
        sa.Column("id", UUID, primary_key=True), sa.Column("script_version_id", UUID, nullable=False),
        sa.Column("comparisons", JSONB, nullable=False), sa.Column("maximum_overlap", sa.Float(), nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False), sa.Column("reason", sa.String(160), nullable=False),
        sa.Column("policy", JSONB, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("checked_by", UUID, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["script_version_id"], ["script_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["checked_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("maximum_overlap >= 0 AND maximum_overlap <= 1", name="ck_originality_overlap"),
        sa.CheckConstraint("verdict IN ('pass','review','block')", name="ck_originality_verdict"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_originality_hash"),
    )
    op.create_table(
        "correction_records",
        sa.Column("id", UUID, primary_key=True), sa.Column("publication_id", UUID, nullable=True),
        sa.Column("script_version_id", UUID, nullable=True), sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("finding", sa.Text(), nullable=False), sa.Column("required_action", sa.String(80), nullable=False),
        sa.Column("evidence", JSONB, nullable=False), sa.Column("actor_id", UUID, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["script_version_id"], ["script_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("publication_id IS NOT NULL OR script_version_id IS NOT NULL", name="ck_correction_target"),
        sa.CheckConstraint("severity IN ('low','medium','high','critical')", name="ck_correction_severity"),
    )
    op.create_table(
        "budget_policy_versions",
        sa.Column("id", UUID, primary_key=True), sa.Column("scope", sa.String(160), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False), sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("limit_amount", sa.Numeric(18, 6), nullable=False), sa.Column("period", sa.String(20), nullable=False),
        sa.Column("document", JSONB, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by", UUID, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("scope", "version_number", name="uq_budget_policy_version"),
        sa.UniqueConstraint("scope", "content_hash", name="uq_budget_policy_hash"),
        sa.CheckConstraint("version_number >= 1 AND limit_amount >= 0", name="ck_budget_policy_values"),
        sa.CheckConstraint("period IN ('workflow','daily','monthly')", name="ck_budget_period"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$' AND content_hash ~ '^[0-9a-f]{64}$'", name="ck_budget_policy_identity"),
    )
    op.create_table(
        "budget_policy_heads",
        sa.Column("scope", sa.String(160), primary_key=True), sa.Column("active_version_id", UUID, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["active_version_id"], ["budget_policy_versions.id"], ondelete="RESTRICT"),
    )
    op.create_table(
        "budget_usage_records",
        sa.Column("id", UUID, primary_key=True), sa.Column("scope", sa.String(160), nullable=False),
        sa.Column("policy_version_id", UUID, nullable=False), sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False), sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("category", sa.String(80), nullable=False), sa.Column("workflow_id", sa.String(240), nullable=True),
        sa.Column("details", JSONB, nullable=False), sa.Column("recorded_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["policy_version_id"], ["budget_policy_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("scope", "idempotency_key", name="uq_budget_usage_key"),
        sa.CheckConstraint("amount >= 0", name="ck_budget_usage_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_budget_usage_currency"),
    )
    for table in APPEND_ONLY:
        _immutable(table)


def downgrade() -> None:
    for table in reversed(APPEND_ONLY):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    for table in (
        "budget_usage_records", "budget_policy_heads", "budget_policy_versions", "correction_records",
        "originality_reports", "source_freshness_checks", "model_recommendation_applications",
        "model_recommendation_decisions", "model_recommendations", "model_benchmark_runs",
    ):
        op.drop_table(table)
    op.drop_index("ix_analytics_video_period", table_name="analytics_metric_snapshots")
    op.drop_table("analytics_metric_snapshots")
