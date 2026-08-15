"""narration-first media timeline editor

Revision ID: 0023_media_timeline_editor
Revises: 0022_direct_scripted_video
Create Date: 2026-08-15
"""

from alembic import op


revision = "0023_media_timeline_editor"
down_revision = "0022_direct_scripted_video"
branch_labels = None
depends_on = None


_WORKFLOW_TYPES = (
    "'fixture-research','live-discovery','source-acquisition','live-research-dossier',"
    "'source-semantic-index','script-generation','storyboard-generation','script-verification',"
    "'script-regeneration','scene-alternative-generation','media-production','media-timeline-draft',"
    "'media-timeline-render','scene-media-regeneration','narration-segment-regeneration',"
    "'youtube-private-upload','direct-scripted-video-import'"
)
_WORKFLOW_TYPES_DOWN = (
    "'fixture-research','live-discovery','source-acquisition','live-research-dossier',"
    "'source-semantic-index','script-generation','storyboard-generation','script-verification',"
    "'script-regeneration','scene-alternative-generation','media-production','scene-media-regeneration',"
    "'narration-segment-regeneration','youtube-private-upload','direct-scripted-video-import'"
)

_MEDIA_STATES = (
    "'queued','generating_assets','generating_narration','assembling','quality_assurance',"
    "'timeline_ready','ready','blocked','failed','cancelled'"
)
_MEDIA_STATES_DOWN = (
    "'queued','generating_assets','generating_narration','assembling','quality_assurance',"
    "'ready','blocked','failed','cancelled'"
)

_ASSET_KINDS = (
    "'visual','operator_clip','narration_original','narration_mastered','caption_vtt',"
    "'caption_srt','thumbnail','chapter','description','source_list','render_master',"
    "'render_preview','manifest'"
)
_ASSET_KINDS_DOWN = (
    "'visual','narration_original','narration_mastered','caption_vtt','caption_srt',"
    "'thumbnail','chapter','description','source_list','render_master','render_preview','manifest'"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_WORKFLOW_TYPES})",
    )
    op.drop_constraint("ck_media_production_state", "media_productions", type_="check")
    op.create_check_constraint(
        "ck_media_production_state",
        "media_productions",
        f"state IN ({_MEDIA_STATES})",
    )
    op.drop_constraint("ck_media_asset_kind", "media_assets", type_="check")
    op.create_check_constraint(
        "ck_media_asset_kind",
        "media_assets",
        f"asset_kind IN ({_ASSET_KINDS})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_media_asset_kind", "media_assets", type_="check")
    op.create_check_constraint(
        "ck_media_asset_kind",
        "media_assets",
        f"asset_kind IN ({_ASSET_KINDS_DOWN})",
    )
    op.drop_constraint("ck_media_production_state", "media_productions", type_="check")
    op.create_check_constraint(
        "ck_media_production_state",
        "media_productions",
        f"state IN ({_MEDIA_STATES_DOWN})",
    )
    op.drop_constraint("ck_workflow_control_type", "workflow_control_records", type_="check")
    op.create_check_constraint(
        "ck_workflow_control_type",
        "workflow_control_records",
        f"workflow_type IN ({_WORKFLOW_TYPES_DOWN})",
    )
