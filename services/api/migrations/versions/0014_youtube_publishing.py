"""youtube publishing, encrypted OAuth and exact release approvals

Revision ID: 0014_youtube_publishing
Revises: 0013_narration_regen
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0014_youtube_publishing"
down_revision = "0013_narration_regen"
branch_labels = None
depends_on = None


def _immutable(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"
    )


def upgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type", "workflow_control_records",
        "workflow_type IN ('fixture-research','live-discovery','source-acquisition','live-research-dossier',"
        "'source-semantic-index','script-generation','storyboard-generation','script-verification',"
        "'script-regeneration','scene-alternative-generation','media-production','scene-media-regeneration',"
        "'narration-segment-regeneration','youtube-private-upload')",
    )
    op.create_table(
        "youtube_connections",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("channel_profile_id", UUID, nullable=False, unique=True),
        sa.Column("youtube_channel_id", sa.String(160), nullable=False),
        sa.Column("youtube_channel_title", sa.String(240), nullable=False),
        sa.Column("refresh_token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("token_fingerprint", sa.String(64), nullable=False),
        sa.Column("granted_scopes", JSONB, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('active','revoked','invalid')", name="ck_youtube_connection_status"),
        sa.CheckConstraint("version >= 1", name="ck_youtube_connection_version"),
        sa.CheckConstraint("token_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_youtube_token_fingerprint"),
    )
    op.create_table(
        "youtube_oauth_states",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("channel_profile_id", UUID, nullable=False),
        sa.Column("initiated_by", UUID, nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["initiated_by"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("state_hash ~ '^[0-9a-f]{64}$'", name="ck_youtube_oauth_state_hash"),
    )
    op.create_table(
        "publishing_configuration_versions",
        sa.Column("id", UUID, primary_key=True), sa.Column("version_number", sa.Integer(), nullable=False, unique=True),
        sa.Column("real_uploads_enabled", sa.Boolean(), nullable=False), sa.Column("document", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True), sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("version_number >= 1", name="ck_publishing_config_version"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_publishing_config_hash"),
    )
    op.create_table(
        "publishing_configuration_head",
        sa.Column("singleton", sa.Boolean(), primary_key=True), sa.Column("active_version_id", UUID, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["active_version_id"], ["publishing_configuration_versions.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("singleton", name="ck_publishing_config_singleton"),
    )
    op.create_table(
        "publish_metadata_versions",
        sa.Column("id", UUID, primary_key=True), sa.Column("render_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False), sa.Column("document", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["render_id"], ["production_renders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("render_id", "version_number", name="uq_publish_metadata_version"),
        sa.UniqueConstraint("render_id", "content_hash", name="uq_publish_metadata_hash"),
        sa.CheckConstraint("version_number >= 1", name="ck_publish_metadata_version"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_publish_metadata_hash"),
    )
    op.create_table(
        "publication_approvals",
        sa.Column("id", UUID, primary_key=True), sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("render_id", UUID, nullable=False), sa.Column("render_hash", sa.String(64), nullable=False),
        sa.Column("metadata_version_id", UUID, nullable=False), sa.Column("metadata_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False), sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("actor_id", UUID, nullable=False), sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["render_id"], ["production_renders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["metadata_version_id"], ["publish_metadata_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("purpose IN ('private_upload','public_release')", name="ck_publication_approval_purpose"),
        sa.CheckConstraint("decision IN ('approved','rejected')", name="ck_publication_approval_decision"),
        sa.CheckConstraint("render_hash ~ '^[0-9a-f]{64}$' AND metadata_hash ~ '^[0-9a-f]{64}$'", name="ck_publication_approval_hashes"),
    )
    op.create_table(
        "publications",
        sa.Column("id", UUID, primary_key=True), sa.Column("workflow_id", sa.String(240), nullable=False, unique=True),
        sa.Column("connection_id", UUID, nullable=False), sa.Column("render_id", UUID, nullable=False),
        sa.Column("render_hash", sa.String(64), nullable=False), sa.Column("metadata_version_id", UUID, nullable=False),
        sa.Column("metadata_hash", sa.String(64), nullable=False), sa.Column("upload_approval_id", UUID, nullable=False),
        sa.Column("configuration_version_id", UUID, nullable=False), sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("state", sa.String(40), nullable=False), sa.Column("upload_idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("youtube_video_id", sa.String(160), nullable=True), sa.Column("resumable_session_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("uploaded_bytes", sa.BigInteger(), nullable=False), sa.Column("processing_status", JSONB, nullable=False),
        sa.Column("caption_status", JSONB, nullable=False), sa.Column("thumbnail_status", JSONB, nullable=False),
        sa.Column("failure", JSONB, nullable=True), sa.Column("started_by", UUID, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["youtube_connections.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["render_id"], ["production_renders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["metadata_version_id"], ["publish_metadata_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["upload_approval_id"], ["publication_approvals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["configuration_version_id"], ["publishing_configuration_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["started_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("mode IN ('dry_run','real')", name="ck_publication_mode"),
        sa.CheckConstraint("state IN ('queued','uploading','uploaded_private','processing','processed','failed')", name="ck_publication_state"),
        sa.CheckConstraint("render_hash ~ '^[0-9a-f]{64}$' AND metadata_hash ~ '^[0-9a-f]{64}$' AND upload_idempotency_key ~ '^[0-9a-f]{64}$'", name="ck_publication_hashes"),
        sa.CheckConstraint("uploaded_bytes >= 0 AND version >= 1", name="ck_publication_progress"),
    )
    op.create_table(
        "publication_schedules",
        sa.Column("id", UUID, primary_key=True), sa.Column("publication_id", UUID, nullable=False),
        sa.Column("approval_id", UUID, nullable=False), sa.Column("publish_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_idempotency_key", sa.String(64), nullable=False, unique=True), sa.Column("state", sa.String(30), nullable=False),
        sa.Column("provider_response", JSONB, nullable=False), sa.Column("created_by", UUID, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approval_id"], ["publication_approvals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("state IN ('scheduled','failed')", name="ck_publication_schedule_state"),
        sa.CheckConstraint("schedule_idempotency_key ~ '^[0-9a-f]{64}$'", name="ck_publication_schedule_hash"),
    )
    for table in ("publishing_configuration_versions", "publish_metadata_versions", "publication_approvals", "publication_schedules"):
        _immutable(table)
    op.create_index("ix_publications_render", "publications", ["render_id"])
    op.create_index("ix_publications_video", "publications", ["youtube_video_id"])


def downgrade() -> None:
    op.drop_index("ix_publications_video", table_name="publications")
    op.drop_index("ix_publications_render", table_name="publications")
    for table in ("publication_schedules", "publications", "publication_approvals", "publish_metadata_versions", "publishing_configuration_head", "publishing_configuration_versions", "youtube_oauth_states", "youtube_connections"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.drop_table(table)
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type", "workflow_control_records",
        "workflow_type IN ('fixture-research','live-discovery','source-acquisition','live-research-dossier',"
        "'source-semantic-index','script-generation','storyboard-generation','script-verification',"
        "'script-regeneration','scene-alternative-generation','media-production','scene-media-regeneration','narration-segment-regeneration')",
    )
