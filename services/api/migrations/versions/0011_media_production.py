"""media workflow, narration, render, manifest and QA provenance

Revision ID: 0011_media_production
Revises: 0010_scene_alternatives
Create Date: 2026-07-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0011_media_production"
down_revision = "0010_scene_alternatives"
branch_labels = None
depends_on = None


_WORKFLOW_TYPES = (
    "'fixture-research', 'live-discovery', 'source-acquisition', "
    "'live-research-dossier', 'source-semantic-index', "
    "'script-generation', 'storyboard-generation', 'script-verification', "
    "'script-regeneration', 'scene-alternative-generation'"
)


def _immutable(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"
    )


def upgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_WORKFLOW_TYPES}, 'media-production', "
        "'scene-media-regeneration', 'narration-segment-regeneration')",
    )
    op.create_table(
        "comfy_workflow_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workflow_key", sa.String(160), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(160), nullable=False),
        sa.Column("api_workflow", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("required_nodes", JSONB, nullable=False),
        sa.Column("required_models", JSONB, nullable=False),
        sa.Column("typed_inputs", JSONB, nullable=False),
        sa.Column("output_contract", JSONB, nullable=False),
        sa.Column("allowed_resolutions", JSONB, nullable=False),
        sa.Column("allowed_durations", JSONB, nullable=False),
        sa.Column("approval_state", sa.String(20), nullable=False),
        sa.Column("approved_by", UUID, nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("workflow_key", "version_number", name="uq_comfy_workflow_version"),
        sa.UniqueConstraint("workflow_key", "content_hash", name="uq_comfy_workflow_hash"),
        sa.CheckConstraint("version_number >= 1", name="ck_comfy_workflow_version"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_comfy_workflow_hash"),
        sa.CheckConstraint("approval_state IN ('draft','approved','rejected')", name="ck_comfy_workflow_approval"),
        sa.CheckConstraint(
            "(approval_state = 'approved' AND approved_by IS NOT NULL AND approved_at IS NOT NULL) OR "
            "(approval_state <> 'approved' AND approved_by IS NULL AND approved_at IS NULL)",
            name="ck_comfy_workflow_approval_actor",
        ),
    )
    op.create_table(
        "comfy_workflow_heads",
        sa.Column("workflow_key", sa.String(160), primary_key=True),
        sa.Column("active_version_id", UUID, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["active_version_id"], ["comfy_workflow_versions.id"], ondelete="RESTRICT"),
    )
    op.create_table(
        "voice_profile_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("profile_key", sa.String(160), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("provider_type", sa.String(40), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("voice_id", sa.String(240), nullable=False),
        sa.Column("language", sa.String(40), nullable=False),
        sa.Column("engine", sa.String(160), nullable=False),
        sa.Column("model_version", sa.String(160), nullable=False),
        sa.Column("delivery", JSONB, nullable=False),
        sa.Column("pronunciation", JSONB, nullable=False),
        sa.Column("output_settings", JSONB, nullable=False),
        sa.Column("consent", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("profile_key", "version_number", name="uq_voice_profile_version"),
        sa.UniqueConstraint("profile_key", "content_hash", name="uq_voice_profile_hash"),
        sa.CheckConstraint("version_number >= 1", name="ck_voice_profile_version"),
        sa.CheckConstraint("provider_type IN ('fake','voicebox_rest','voicebox_ws')", name="ck_voice_provider"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_voice_profile_hash"),
    )
    op.create_table(
        "voice_profile_heads",
        sa.Column("profile_key", sa.String(160), primary_key=True),
        sa.Column("active_version_id", UUID, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["active_version_id"], ["voice_profile_versions.id"], ondelete="RESTRICT"),
    )
    op.create_table(
        "media_productions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("storyboard_version_id", UUID, nullable=False),
        sa.Column("storyboard_hash", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False, unique=True),
        sa.Column("render_tier", sa.String(20), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("settings", JSONB, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("started_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["storyboard_version_id"], ["storyboard_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["started_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("storyboard_version_id", "render_tier", name="uq_media_production_tier"),
        sa.CheckConstraint("storyboard_hash ~ '^[0-9a-f]{64}$'", name="ck_media_storyboard_hash"),
        sa.CheckConstraint("render_tier IN ('preview','full')", name="ck_media_render_tier"),
        sa.CheckConstraint(
            "state IN ('queued','generating_assets','generating_narration','assembling','quality_assurance','ready','blocked','failed','cancelled')",
            name="ck_media_production_state",
        ),
    )
    op.create_table(
        "media_assets",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("production_id", UUID, nullable=False),
        sa.Column("scene_version_id", UUID, nullable=True),
        sa.Column("asset_kind", sa.String(40), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("licence", JSONB, nullable=False),
        sa.Column("generation_provenance", JSONB, nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["production_id"], ["media_productions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["scene_version_id"], ["scene_versions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("production_id", "object_key", name="uq_media_asset_object"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_media_asset_hash"),
        sa.CheckConstraint("cache_key IS NULL OR cache_key ~ '^[0-9a-f]{64}$'", name="ck_media_cache_key"),
        sa.CheckConstraint("byte_size >= 0", name="ck_media_asset_size"),
        sa.CheckConstraint(
            "asset_kind IN ('visual','narration_original','narration_mastered','caption_vtt','caption_srt','thumbnail','chapter','description','source_list','render_master','render_preview','manifest')",
            name="ck_media_asset_kind",
        ),
    )
    op.create_table(
        "narration_segments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("production_id", UUID, nullable=False),
        sa.Column("script_segment_id", UUID, nullable=False),
        sa.Column("segment_order", sa.Integer(), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("response", JSONB, nullable=False),
        sa.Column("original_asset_id", UUID, nullable=False),
        sa.Column("mastered_asset_id", UUID, nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=False),
        sa.Column("word_alignment", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parent_segment_id", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["production_id"], ["media_productions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["script_segment_id"], ["script_segments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["original_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mastered_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_segment_id"], ["narration_segments.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("production_id", "segment_order", name="uq_narration_segment_order"),
        sa.CheckConstraint("segment_order >= 1", name="ck_narration_segment_order"),
        sa.CheckConstraint("duration_seconds > 0", name="ck_narration_duration"),
        sa.CheckConstraint("sample_rate BETWEEN 8000 AND 192000", name="ck_narration_sample_rate"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_narration_hash"),
    )
    op.create_table(
        "production_manifests",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("production_id", UUID, nullable=False),
        sa.Column("manifest_version", sa.Integer(), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["production_id"], ["media_productions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("production_id", "manifest_version", name="uq_production_manifest_version"),
        sa.UniqueConstraint("production_id", "content_hash", name="uq_production_manifest_hash"),
        sa.CheckConstraint("manifest_version >= 1", name="ck_production_manifest_version"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_production_manifest_hash"),
    )
    op.create_table(
        "production_renders",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("production_id", UUID, nullable=False),
        sa.Column("render_number", sa.Integer(), nullable=False),
        sa.Column("tier", sa.String(20), nullable=False),
        sa.Column("video_asset_id", UUID, nullable=False),
        sa.Column("manifest_id", UUID, nullable=False),
        sa.Column("render_engine", sa.String(80), nullable=False),
        sa.Column("render_engine_version", sa.String(80), nullable=False),
        sa.Column("composition", sa.String(160), nullable=False),
        sa.Column("settings", JSONB, nullable=False),
        sa.Column("probe", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["production_id"], ["media_productions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["video_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["manifest_id"], ["production_manifests.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("production_id", "render_number", name="uq_production_render_number"),
        sa.CheckConstraint("render_number >= 1", name="ck_production_render_number"),
        sa.CheckConstraint("tier IN ('preview','full')", name="ck_production_render_tier"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_production_render_hash"),
    )
    op.create_table(
        "qa_reports",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("render_id", UUID, nullable=False, unique=True),
        sa.Column("verdict", sa.String(10), nullable=False),
        sa.Column("policy_snapshot", JSONB, nullable=False),
        sa.Column("metrics", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["render_id"], ["production_renders.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("verdict IN ('pass','warn','fail')", name="ck_qa_report_verdict"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_qa_report_hash"),
    )
    op.create_table(
        "qa_findings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("report_id", UUID, nullable=False),
        sa.Column("finding_order", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(120), nullable=False),
        sa.Column("verdict", sa.String(10), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("scene_version_id", UUID, nullable=True),
        sa.Column("timecode_seconds", sa.Float(), nullable=True),
        sa.Column("details", JSONB, nullable=False),
        sa.Column("override_policy", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["qa_reports.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["scene_version_id"], ["scene_versions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("report_id", "finding_order", name="uq_qa_finding_order"),
        sa.UniqueConstraint("report_id", "code", "scene_version_id", name="uq_qa_finding_code_scene"),
        sa.CheckConstraint("finding_order >= 1", name="ck_qa_finding_order"),
        sa.CheckConstraint("verdict IN ('pass','warn','fail')", name="ck_qa_finding_verdict"),
        sa.CheckConstraint("override_policy IN ('never','reasoned')", name="ck_qa_override_policy"),
        sa.CheckConstraint("timecode_seconds IS NULL OR timecode_seconds >= 0", name="ck_qa_timecode"),
    )
    op.create_table(
        "qa_overrides",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("finding_id", UUID, nullable=False, unique=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", UUID, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["finding_id"], ["qa_findings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("length(trim(reason)) >= 10", name="ck_qa_override_reason"),
    )
    for table in (
        "comfy_workflow_versions", "voice_profile_versions", "media_assets",
        "narration_segments", "production_manifests", "production_renders",
        "qa_reports", "qa_findings", "qa_overrides",
    ):
        _immutable(table)
    op.create_index("ix_media_production_storyboard", "media_productions", ["storyboard_version_id"])
    op.create_index("ix_media_asset_production_scene", "media_assets", ["production_id", "scene_version_id"])


def downgrade() -> None:
    op.drop_index("ix_media_asset_production_scene", table_name="media_assets")
    op.drop_index("ix_media_production_storyboard", table_name="media_productions")
    for table in ("qa_overrides", "qa_findings", "qa_reports", "production_renders", "production_manifests", "narration_segments", "media_assets"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.drop_table(table)
    op.drop_table("media_productions")
    op.drop_table("voice_profile_heads")
    op.execute("DROP TRIGGER IF EXISTS voice_profile_versions_immutable ON voice_profile_versions")
    op.drop_table("voice_profile_versions")
    op.drop_table("comfy_workflow_heads")
    op.execute("DROP TRIGGER IF EXISTS comfy_workflow_versions_immutable ON comfy_workflow_versions")
    op.drop_table("comfy_workflow_versions")
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type", "workflow_control_records", f"workflow_type IN ({_WORKFLOW_TYPES})"
    )
