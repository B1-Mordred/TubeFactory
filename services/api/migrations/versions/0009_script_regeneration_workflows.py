"""track selected script regeneration workflows

Revision ID: 0009_script_regeneration
Revises: 0008_script_verification
Create Date: 2026-07-20
"""

from alembic import op


revision = "0009_script_regeneration"
down_revision = "0008_script_verification"
branch_labels = None
depends_on = None


_BASE = (
    "'fixture-research', 'live-discovery', 'source-acquisition', "
    "'live-research-dossier', 'source-semantic-index', "
    "'script-generation', 'storyboard-generation', 'script-verification'"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_BASE}, 'script-regeneration')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type", "workflow_control_records", f"workflow_type IN ({_BASE})"
    )
