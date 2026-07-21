"""allow tracked editorial production workflows

Revision ID: 0007_editorial_workflows
Revises: 0006_editorial
Create Date: 2026-07-20
"""

from alembic import op


revision = "0007_editorial_workflows"
down_revision = "0006_editorial"
branch_labels = None
depends_on = None


_RESEARCH_TYPES = (
    "'fixture-research', 'live-discovery', 'source-acquisition', "
    "'live-research-dossier', 'source-semantic-index'"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_RESEARCH_TYPES}, 'script-generation', 'storyboard-generation')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_RESEARCH_TYPES})",
    )
