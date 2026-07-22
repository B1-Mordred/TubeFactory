"""append-only AI topic qualifications and evidence assessments

Revision ID: 0019_hybrid_ai_reviews
Revises: 0018_operating_policy
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0019_hybrid_ai_reviews"
down_revision = "0018_operating_policy"
branch_labels = None
depends_on = None

UTC = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "opportunity_ai_qualifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("opportunity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("qualification_version", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), nullable=False),
        sa.Column("prompt_template_id", UUID(as_uuid=True), nullable=False),
        sa.Column("rubric_version", sa.String(80), nullable=False),
        sa.Column("dimensions", JSONB, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("abstained", sa.Boolean(), nullable=False),
        sa.Column("rationale", JSONB, nullable=False),
        sa.Column("uncertainty", JSONB, nullable=False),
        sa.Column("resulting_score_version", sa.Integer(), nullable=True),
        sa.Column("created_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["prompt_template_id"], ["prompt_templates.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("opportunity_id", "qualification_version", name="uq_opportunity_ai_qualification_version"),
        sa.CheckConstraint("qualification_version >= 1", name="ck_opportunity_ai_qualification_version"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_opportunity_ai_qualification_confidence"),
    )
    op.create_table(
        "dossier_ai_assessments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("research_dossier_id", UUID(as_uuid=True), nullable=False),
        sa.Column("assessment_version", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), nullable=False),
        sa.Column("prompt_template_id", UUID(as_uuid=True), nullable=False),
        sa.Column("rubric_version", sa.String(80), nullable=False),
        sa.Column("claim_assessments", JSONB, nullable=False),
        sa.Column("source_assessments", JSONB, nullable=False),
        sa.Column("methodological_limits", JSONB, nullable=False),
        sa.Column("counterevidence_gaps", JSONB, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("abstained", sa.Boolean(), nullable=False),
        sa.Column("uncertainty", JSONB, nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["research_dossier_id"], ["research_dossiers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["prompt_template_id"], ["prompt_templates.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("research_dossier_id", "assessment_version", name="uq_dossier_ai_assessment_version"),
        sa.CheckConstraint("assessment_version >= 1", name="ck_dossier_ai_assessment_version"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_dossier_ai_assessment_confidence"),
    )
    op.execute("CREATE TRIGGER opportunity_ai_qualifications_immutable BEFORE UPDATE OR DELETE ON opportunity_ai_qualifications FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()")
    op.execute("CREATE TRIGGER dossier_ai_assessments_immutable BEFORE UPDATE OR DELETE ON dossier_ai_assessments FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS dossier_ai_assessments_immutable ON dossier_ai_assessments")
    op.execute("DROP TRIGGER IF EXISTS opportunity_ai_qualifications_immutable ON opportunity_ai_qualifications")
    op.drop_table("dossier_ai_assessments")
    op.drop_table("opportunity_ai_qualifications")
