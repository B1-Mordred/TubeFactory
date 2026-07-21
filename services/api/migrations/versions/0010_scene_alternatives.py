"""immutable generated scene alternatives

Revision ID: 0010_scene_alternatives
Revises: 0009_script_regeneration
Create Date: 2026-07-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0010_scene_alternatives"
down_revision = "0009_script_regeneration"
branch_labels = None
depends_on = None


_BASE = (
    "'fixture-research', 'live-discovery', 'source-acquisition', "
    "'live-research-dossier', 'source-semantic-index', "
    "'script-generation', 'storyboard-generation', 'script-verification', "
    "'script-regeneration'"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_BASE}, 'scene-alternative-generation')",
    )
    op.create_table(
        "scene_alternatives",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("scene_id", UUID, nullable=False),
        sa.Column("base_scene_version_id", UUID, nullable=False),
        sa.Column("alternative_number", sa.Integer(), nullable=False),
        sa.Column("scene_spec", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("model_id", UUID, nullable=False),
        sa.Column("prompt_template_id", UUID, nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False, unique=True),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["base_scene_version_id"], ["scene_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"], ["prompt_templates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "scene_id",
            "base_scene_version_id",
            "alternative_number",
            name="uq_scene_alternative_number",
        ),
        sa.CheckConstraint("alternative_number >= 1", name="ck_scene_alternative_number"),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_scene_alternative_content_hash"
        ),
    )
    op.create_index(
        "ix_scene_alternative_base",
        "scene_alternatives",
        ["scene_id", "base_scene_version_id", "alternative_number"],
    )
    op.execute(
        "CREATE TRIGGER scene_alternatives_immutable BEFORE UPDATE OR DELETE ON "
        "scene_alternatives FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS scene_alternatives_immutable ON scene_alternatives")
    op.drop_index("ix_scene_alternative_base", table_name="scene_alternatives")
    op.drop_table("scene_alternatives")
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type", "workflow_control_records", f"workflow_type IN ({_BASE})"
    )
