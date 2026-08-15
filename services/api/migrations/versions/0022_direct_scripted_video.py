"""direct scripted-video production briefs

Revision ID: 0022_direct_scripted_video
Revises: 0021_explanation_readiness
Create Date: 2026-08-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0022_direct_scripted_video"
down_revision = "0021_explanation_readiness"
branch_labels = None
depends_on = None

UTC = sa.DateTime(timezone=True)

_WORKFLOW_TYPES = (
    "'fixture-research','live-discovery','source-acquisition','live-research-dossier',"
    "'source-semantic-index','script-generation','storyboard-generation','script-verification',"
    "'script-regeneration','scene-alternative-generation','media-production','scene-media-regeneration',"
    "'narration-segment-regeneration','youtube-private-upload','direct-scripted-video-import'"
)
_WORKFLOW_TYPES_DOWN = (
    "'fixture-research','live-discovery','source-acquisition','live-research-dossier',"
    "'source-semantic-index','script-generation','storyboard-generation','script-verification',"
    "'script-regeneration','scene-alternative-generation','media-production','scene-media-regeneration',"
    "'narration-segment-regeneration','youtube-private-upload'"
)


def upgrade() -> None:
    op.create_table(
        "production_briefs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("updated_at", UTC, nullable=False),
        sa.Column("deleted_at", UTC, nullable=True),
        sa.Column("channel_profile_id", UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("source_kind", sa.String(40), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_text_hash", sa.String(64), nullable=False),
        sa.Column("parse_report", JSONB, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("workflow_id", name="uq_production_brief_workflow"),
        sa.CheckConstraint("version >= 1", name="ck_production_brief_version"),
        sa.CheckConstraint("source_kind IN ('direct_scripted_video')", name="ck_production_brief_source_kind"),
        sa.CheckConstraint("status IN ('imported','archived')", name="ck_production_brief_status"),
        sa.CheckConstraint("source_text_hash ~ '^[0-9a-f]{64}$'", name="ck_production_brief_source_hash"),
    )
    op.add_column(
        "scripts",
        sa.Column("production_brief_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scripts",
        sa.Column("source_kind", sa.String(40), nullable=False, server_default="research_dossier"),
    )
    op.create_foreign_key(
        "fk_scripts_production_brief",
        "scripts",
        "production_briefs",
        ["production_brief_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_scripts_production_brief_id",
        "scripts",
        ["production_brief_id"],
    )
    op.alter_column("scripts", "opportunity_id", existing_type=UUID(as_uuid=True), nullable=True)
    op.alter_column("scripts", "research_dossier_id", existing_type=UUID(as_uuid=True), nullable=True)
    op.create_check_constraint(
        "ck_script_source_lineage",
        "scripts",
        "("
        "source_kind='research_dossier' AND opportunity_id IS NOT NULL "
        "AND research_dossier_id IS NOT NULL AND production_brief_id IS NULL"
        ") OR ("
        "source_kind='direct_scripted_video' AND opportunity_id IS NULL "
        "AND research_dossier_id IS NULL AND production_brief_id IS NOT NULL"
        ")",
    )
    op.alter_column("scripts", "source_kind", server_default=None)
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_WORKFLOW_TYPES})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_WORKFLOW_TYPES_DOWN})",
    )
    op.drop_constraint("ck_script_source_lineage", "scripts", type_="check")
    op.alter_column("scripts", "research_dossier_id", existing_type=UUID(as_uuid=True), nullable=False)
    op.alter_column("scripts", "opportunity_id", existing_type=UUID(as_uuid=True), nullable=False)
    op.drop_constraint("uq_scripts_production_brief_id", "scripts", type_="unique")
    op.drop_constraint("fk_scripts_production_brief", "scripts", type_="foreignkey")
    op.drop_column("scripts", "source_kind")
    op.drop_column("scripts", "production_brief_id")
    op.drop_table("production_briefs")
