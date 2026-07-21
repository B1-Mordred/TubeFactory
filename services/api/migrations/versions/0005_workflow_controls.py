"""immutable workflow control provenance

Revision ID: 0005_workflow_controls
Revises: 0004_semantic_chunks
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_workflow_controls"
down_revision = "0004_semantic_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_control_records",
        sa.Column("workflow_id", sa.String(240), primary_key=True),
        sa.Column("workflow_type", sa.String(120), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("parent_workflow_id", sa.String(240), nullable=True),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("started_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["started_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "workflow_type IN ('fixture-research', 'live-discovery', "
            "'source-acquisition', 'live-research-dossier', 'source-semantic-index')",
            name="ck_workflow_control_type",
        ),
    )
    op.create_index(
        "ix_workflow_control_parent", "workflow_control_records", ["parent_workflow_id"]
    )
    op.create_index(
        "ix_workflow_control_correlation", "workflow_control_records", ["correlation_id"]
    )
    op.execute(
        "CREATE TRIGGER workflow_control_records_immutable BEFORE UPDATE OR DELETE "
        "ON workflow_control_records FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS workflow_control_records_immutable ON workflow_control_records"
    )
    op.drop_index("ix_workflow_control_correlation", table_name="workflow_control_records")
    op.drop_index("ix_workflow_control_parent", table_name="workflow_control_records")
    op.drop_table("workflow_control_records")
