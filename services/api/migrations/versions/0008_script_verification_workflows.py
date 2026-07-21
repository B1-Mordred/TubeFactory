"""track script re-verification workflows

Revision ID: 0008_script_verification
Revises: 0007_editorial_workflows
Create Date: 2026-07-20
"""

from alembic import op


revision = "0008_script_verification"
down_revision = "0007_editorial_workflows"
branch_labels = None
depends_on = None


_BASE = (
    "'fixture-research', 'live-discovery', 'source-acquisition', "
    "'live-research-dossier', 'source-semantic-index', "
    "'script-generation', 'storyboard-generation'"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_BASE}, 'script-verification')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type", "workflow_control_records", f"workflow_type IN ({_BASE})"
    )
