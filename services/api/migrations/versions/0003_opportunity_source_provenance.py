"""link discovered opportunities to normalized source candidates

Revision ID: 0003_opp_sources
Revises: 0002_discovery_evidence
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_opp_sources"
down_revision = "0002_discovery_evidence"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
UTC = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "opportunity_sources",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("opportunity_id", UUID, nullable=False),
        sa.Column("source_document_id", UUID, nullable=False),
        sa.Column("search_purpose", sa.String(80), nullable=False),
        sa.Column("search_query", sa.Text(), nullable=False),
        sa.Column("result_rank", sa.Integer(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("opportunity_id", "source_document_id", name="uq_opportunity_source"),
        sa.CheckConstraint("result_rank >= 1", name="ck_opportunity_source_rank"),
    )
    op.execute(
        """CREATE TRIGGER opportunity_sources_immutable
           BEFORE UPDATE OR DELETE ON opportunity_sources
           FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"""
    )


def downgrade() -> None:
    op.drop_table("opportunity_sources")
