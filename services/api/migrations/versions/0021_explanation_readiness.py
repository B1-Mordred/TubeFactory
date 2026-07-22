"""explanation readiness and claim coverage lineage

Revision ID: 0021_explanation_readiness
Revises: 0020_ai_evidence_synthesis
Create Date: 2026-07-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0021_explanation_readiness"
down_revision = "0020_ai_evidence_synthesis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_dossiers",
        sa.Column("explanation_plan", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "claims",
        sa.Column("coverage_unit_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "research_ai_query_plans",
        sa.Column("coverage_units", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.alter_column("research_dossiers", "explanation_plan", server_default=None)
    op.alter_column("claims", "coverage_unit_ids", server_default=None)
    op.alter_column("research_ai_query_plans", "coverage_units", server_default=None)


def downgrade() -> None:
    op.drop_column("research_ai_query_plans", "coverage_units")
    op.drop_column("claims", "coverage_unit_ids")
    op.drop_column("research_dossiers", "explanation_plan")
