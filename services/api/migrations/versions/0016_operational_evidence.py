"""fallback recommendations, correction impact and operational evidence

Revision ID: 0016_operational_evidence
Revises: 0015_optimization_hardening
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0016_operational_evidence"
down_revision = "0015_optimization_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_recommendations", sa.Column("recommended_fallback_model_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.alter_column("model_recommendations", "recommended_fallback_model_ids", server_default=None)
    op.drop_constraint("ck_correction_target", "correction_records", type_="check")
    op.add_column("correction_records", sa.Column("source_snapshot_id", UUID, nullable=True))
    op.add_column("correction_records", sa.Column("affected_claims", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("correction_records", sa.Column("affected_timecodes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("correction_records", sa.Column("proposal", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.create_foreign_key("fk_correction_source_snapshot", "correction_records", "source_snapshots", ["source_snapshot_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint("ck_correction_target", "correction_records", "publication_id IS NOT NULL OR script_version_id IS NOT NULL OR source_snapshot_id IS NOT NULL")
    for column in ("affected_claims", "affected_timecodes", "proposal"):
        op.alter_column("correction_records", column, server_default=None)
    op.drop_constraint("ck_analytics_source", "analytics_metric_snapshots", type_="check")
    op.create_check_constraint("ck_analytics_source", "analytics_metric_snapshots", "source IN ('youtube_analytics','operator_import','manual_fixture')")
    op.create_table(
        "operational_evidence",
        sa.Column("id", UUID, primary_key=True), sa.Column("evidence_kind", sa.String(40), nullable=False),
        sa.Column("document", JSONB, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=True), sa.Column("recorded_by", UUID, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("evidence_kind IN ('backup','restore_drill','sbom','security_audit','observability')", name="ck_operational_evidence_kind"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$' AND (artifact_hash IS NULL OR artifact_hash ~ '^[0-9a-f]{64}$')", name="ck_operational_evidence_hash"),
    )
    op.execute("CREATE TRIGGER operational_evidence_immutable BEFORE UPDATE OR DELETE ON operational_evidence FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS operational_evidence_immutable ON operational_evidence")
    op.drop_table("operational_evidence")
    op.drop_constraint("ck_analytics_source", "analytics_metric_snapshots", type_="check")
    op.create_check_constraint("ck_analytics_source", "analytics_metric_snapshots", "source IN ('youtube_analytics','manual_fixture')")
    op.drop_constraint("ck_correction_target", "correction_records", type_="check")
    op.drop_constraint("fk_correction_source_snapshot", "correction_records", type_="foreignkey")
    for column in ("proposal", "affected_timecodes", "affected_claims", "source_snapshot_id"):
        op.drop_column("correction_records", column)
    op.create_check_constraint("ck_correction_target", "correction_records", "publication_id IS NOT NULL OR script_version_id IS NOT NULL")
    op.drop_column("model_recommendations", "recommended_fallback_model_ids")
