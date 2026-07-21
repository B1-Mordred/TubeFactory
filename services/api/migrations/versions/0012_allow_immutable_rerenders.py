"""allow multiple immutable productions per storyboard render tier

Revision ID: 0012_allow_immutable_rerenders
Revises: 0011_media_production
"""

from alembic import op


revision = "0012_allow_immutable_rerenders"
down_revision = "0011_media_production"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_media_production_tier", "media_productions", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_media_production_tier",
        "media_productions",
        ["storyboard_version_id", "render_tier"],
    )
