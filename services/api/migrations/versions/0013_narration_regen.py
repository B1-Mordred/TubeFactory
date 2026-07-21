"""allow immutable narration segment regeneration

Revision ID: 0013_narration_regen
Revises: 0012_allow_immutable_rerenders
"""

from alembic import op


revision = "0013_narration_regen"
down_revision = "0012_allow_immutable_rerenders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_narration_segment_order", "narration_segments", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_narration_segment_order",
        "narration_segments",
        ["production_id", "segment_order"],
    )
