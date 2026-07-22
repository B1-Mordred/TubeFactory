"""append-only source-bound AI evidence synthesis

Revision ID: 0020_ai_evidence_synthesis
Revises: 0019_hybrid_ai_reviews
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0020_ai_evidence_synthesis"
down_revision = "0019_hybrid_ai_reviews"
branch_labels = None
depends_on = None

UTC = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "research_ai_query_plans",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("opportunity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), nullable=False),
        sa.Column("prompt_template_id", UUID(as_uuid=True), nullable=False),
        sa.Column("rubric_version", sa.String(80), nullable=False),
        sa.Column("queries", JSONB, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("abstained", sa.Boolean(), nullable=False),
        sa.Column("uncertainty", JSONB, nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["prompt_template_id"], ["prompt_templates.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("opportunity_id", "plan_version", name="uq_research_ai_query_plan_version"),
        sa.CheckConstraint("plan_version >= 1", name="ck_research_ai_query_plan_version"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_research_ai_query_plan_confidence"),
    )
    op.execute(
        "CREATE TRIGGER research_ai_query_plans_immutable BEFORE UPDATE OR DELETE ON "
        "research_ai_query_plans FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"
    )
    op.create_table(
        "research_ai_syntheses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("research_run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("synthesis_version", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), nullable=False),
        sa.Column("prompt_template_id", UUID(as_uuid=True), nullable=False),
        sa.Column("rubric_version", sa.String(80), nullable=False),
        sa.Column("proposed_claims", JSONB, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("abstained", sa.Boolean(), nullable=False),
        sa.Column("uncertainty", JSONB, nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["research_run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["prompt_template_id"], ["prompt_templates.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "research_run_id", "synthesis_version", name="uq_research_ai_synthesis_version"
        ),
        sa.CheckConstraint("synthesis_version >= 1", name="ck_research_ai_synthesis_version"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_research_ai_synthesis_confidence"),
    )
    op.execute(
        "CREATE TRIGGER research_ai_syntheses_immutable BEFORE UPDATE OR DELETE ON "
        "research_ai_syntheses FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS research_ai_syntheses_immutable ON research_ai_syntheses")
    op.drop_table("research_ai_syntheses")
    op.execute("DROP TRIGGER IF EXISTS research_ai_query_plans_immutable ON research_ai_query_plans")
    op.drop_table("research_ai_query_plans")
