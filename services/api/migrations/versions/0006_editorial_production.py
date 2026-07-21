"""editorial provider, script, verification and storyboard model

Revision ID: 0006_editorial
Revises: 0005_workflow_controls
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_editorial"
down_revision = "0005_workflow_controls"
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


def _versioned_columns() -> list[sa.Column]:
    return [
        sa.Column("id", UUID, primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "providers",
        *_versioned_columns(),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("driver_type", sa.String(40), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("location", sa.String(20), nullable=False),
        sa.Column("authentication_scheme", sa.String(30), nullable=False),
        sa.Column("secret_reference", sa.String(240), nullable=True),
        sa.Column("data_policy", sa.String(40), nullable=False),
        sa.Column("residency_policy", JSONB, nullable=False),
        sa.Column("capabilities", JSONB, nullable=False),
        sa.Column("concurrency_limit", sa.Integer(), nullable=False),
        sa.Column("requests_per_minute", sa.Integer(), nullable=False),
        sa.Column("health_status", JSONB, nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "driver_type IN ('fake','openai_compatible','ollama','anthropic','gemini','generic_rest')",
            name="ck_provider_driver",
        ),
        sa.CheckConstraint("location IN ('local','remote')", name="ck_provider_location"),
        sa.CheckConstraint(
            "authentication_scheme IN ('none','bearer','api_key','oauth')",
            name="ck_provider_auth_scheme",
        ),
        sa.CheckConstraint(
            "data_policy IN ('local_only','remote_allowed','remote_after_redaction')",
            name="ck_provider_data_policy",
        ),
        sa.CheckConstraint("concurrency_limit BETWEEN 1 AND 1000", name="ck_provider_concurrency"),
        sa.CheckConstraint("requests_per_minute BETWEEN 1 AND 100000", name="ck_provider_rate"),
    )
    op.create_table(
        "models",
        *_versioned_columns(),
        sa.Column("provider_id", UUID, nullable=False),
        sa.Column("model_name", sa.String(240), nullable=False),
        sa.Column("display_name", sa.String(240), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("model_version", sa.String(160), nullable=False),
        sa.Column("capabilities", JSONB, nullable=False),
        sa.Column("context_limit", sa.Integer(), nullable=False),
        sa.Column("output_limit", sa.Integer(), nullable=False),
        sa.Column("cost_policy", JSONB, nullable=False),
        sa.Column("data_policy_override", sa.String(40), nullable=True),
        sa.Column("created_by", UUID, nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("provider_id", "model_name", name="uq_provider_model_name"),
        sa.CheckConstraint("context_limit BETWEEN 256 AND 10000000", name="ck_model_context"),
        sa.CheckConstraint("output_limit BETWEEN 64 AND 1000000", name="ck_model_output"),
        sa.CheckConstraint(
            "data_policy_override IS NULL OR data_policy_override IN "
            "('local_only','remote_allowed','remote_after_redaction')",
            name="ck_model_data_policy",
        ),
    )
    op.create_table(
        "task_model_assignments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("assignment_version", sa.Integer(), nullable=False),
        sa.Column("primary_model_id", UUID, nullable=False),
        sa.Column("fallback_model_ids", JSONB, nullable=False),
        sa.Column("routing_policy", JSONB, nullable=False),
        sa.Column("budget_policy", JSONB, nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["primary_model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("task_type", "assignment_version", name="uq_task_assignment_version"),
        sa.CheckConstraint("assignment_version >= 1", name="ck_task_assignment_version"),
    )
    op.create_table(
        "task_model_assignment_heads",
        sa.Column("task_type", sa.String(80), primary_key=True),
        sa.Column("active_assignment_id", UUID, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["active_assignment_id"], ["task_model_assignments.id"], ondelete="RESTRICT"
        ),
    )
    op.create_table(
        "prompt_templates",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("template_key", sa.String(160), nullable=False),
        sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("system_instructions", sa.Text(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("input_schema", JSONB, nullable=False),
        sa.Column("response_schema", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("template_key", "template_version", name="uq_prompt_template_version"),
        sa.UniqueConstraint("template_key", "content_hash", name="uq_prompt_template_hash"),
        sa.CheckConstraint("template_version >= 1", name="ck_prompt_template_version"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_prompt_content_hash"),
    )
    op.create_table(
        "prompt_template_heads",
        sa.Column("template_key", sa.String(160), primary_key=True),
        sa.Column("active_template_id", UUID, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["active_template_id"], ["prompt_templates.id"], ondelete="RESTRICT"),
    )
    op.create_table(
        "ai_usage_records",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("activity_id", sa.String(240), nullable=False),
        sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("provider_id", UUID, nullable=False),
        sa.Column("model_id", UUID, nullable=False),
        sa.Column("prompt_template_id", UUID, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_hash", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost", JSONB, nullable=False),
        sa.Column("redaction_summary", JSONB, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["prompt_template_id"], ["prompt_templates.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="ck_ai_usage_tokens"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_ai_usage_latency"),
    )
    op.create_index("ix_ai_usage_workflow", "ai_usage_records", ["workflow_id"])

    op.create_table(
        "scripts",
        *_versioned_columns(),
        sa.Column("opportunity_id", UUID, nullable=False),
        sa.Column("research_dossier_id", UUID, nullable=False, unique=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("current_version_id", UUID, nullable=True),
        sa.Column("created_by", UUID, nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["research_dossier_id"], ["research_dossiers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('draft','verifying','verified','blocked','approved','rejected')",
            name="ck_script_status",
        ),
    )
    op.create_table(
        "script_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("script_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("total_duration_seconds", sa.Float(), nullable=False),
        sa.Column("writer_model_id", UUID, nullable=True),
        sa.Column("verifier_model_id", UUID, nullable=True),
        sa.Column("writer_prompt_id", UUID, nullable=True),
        sa.Column("verifier_prompt_id", UUID, nullable=True),
        sa.Column("verification_report", JSONB, nullable=False),
        sa.Column("coverage_percent", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parent_version_id", UUID, nullable=True),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["script_id"], ["scripts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["writer_model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["verifier_model_id"], ["models.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["writer_prompt_id"], ["prompt_templates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["verifier_prompt_id"], ["prompt_templates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_version_id"], ["script_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("script_id", "version_number", name="uq_script_version_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_script_version_number"),
        sa.CheckConstraint("coverage_percent BETWEEN 0 AND 100", name="ck_script_coverage"),
        sa.CheckConstraint("total_duration_seconds > 0", name="ck_script_duration"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_script_content_hash"),
    )
    op.create_foreign_key(
        "fk_script_current_version", "scripts", "script_versions", ["current_version_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "script_segments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("script_version_id", UUID, nullable=False),
        sa.Column("segment_key", sa.String(120), nullable=False),
        sa.Column("segment_order", sa.Integer(), nullable=False),
        sa.Column("segment_type", sa.String(40), nullable=False),
        sa.Column("narration", sa.Text(), nullable=False),
        sa.Column("presentation_purpose", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("citation_display", JSONB, nullable=False),
        sa.Column("annotations", JSONB, nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["script_version_id"], ["script_versions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("script_version_id", "segment_key", name="uq_script_segment_key"),
        sa.UniqueConstraint("script_version_id", "segment_order", name="uq_script_segment_order"),
        sa.CheckConstraint("segment_order >= 1", name="ck_script_segment_order"),
        sa.CheckConstraint("duration_seconds > 0", name="ck_script_segment_duration"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_segment_content_hash"),
    )
    op.create_table(
        "segment_claims",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("script_segment_id", UUID, nullable=False),
        sa.Column("claim_id", UUID, nullable=False),
        sa.Column("evidence_excerpt_id", UUID, nullable=True),
        sa.Column("statement_text", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("statement_kind", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["script_segment_id"], ["script_segments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_excerpt_id"], ["evidence_excerpts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "script_segment_id", "claim_id", "start_offset", "end_offset",
            name="uq_segment_claim_statement",
        ),
        sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="ck_segment_claim_offsets"),
        sa.CheckConstraint(
            "statement_kind IN ('fact','inference','opinion','quote','editorial')",
            name="ck_segment_claim_kind",
        ),
    )

    op.create_table(
        "storyboards",
        *_versioned_columns(),
        sa.Column("script_id", UUID, nullable=False, unique=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("current_version_id", UUID, nullable=True),
        sa.Column("created_by", UUID, nullable=False),
        sa.ForeignKeyConstraint(["script_id"], ["scripts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('draft','in_review','approved','blocked','rejected')",
            name="ck_storyboard_status",
        ),
    )
    op.create_table(
        "storyboard_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("storyboard_id", UUID, nullable=False),
        sa.Column("script_version_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parent_version_id", UUID, nullable=True),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["storyboard_id"], ["storyboards.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["script_version_id"], ["script_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_version_id"], ["storyboard_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("storyboard_id", "version_number", name="uq_storyboard_version_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_storyboard_version_number"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_storyboard_content_hash"),
    )
    op.create_foreign_key(
        "fk_storyboard_current_version", "storyboards", "storyboard_versions",
        ["current_version_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_table(
        "scenes",
        *_versioned_columns(),
        sa.Column("storyboard_id", UUID, nullable=False),
        sa.Column("scene_key", sa.String(120), nullable=False),
        sa.Column("current_version_id", UUID, nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["storyboard_id"], ["storyboards.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("storyboard_id", "scene_key", name="uq_storyboard_scene_key"),
    )
    op.create_table(
        "scene_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("scene_id", UUID, nullable=False),
        sa.Column("storyboard_version_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("scene_order", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("visual_type", sa.String(40), nullable=False),
        sa.Column("scene_spec", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parent_version_id", UUID, nullable=True),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["storyboard_version_id"], ["storyboard_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_version_id"], ["scene_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("scene_id", "version_number", name="uq_scene_version_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_scene_version_number"),
        sa.CheckConstraint("scene_order >= 1", name="ck_scene_order"),
        sa.CheckConstraint("duration_seconds BETWEEN 0.5 AND 900", name="ck_scene_duration"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_scene_content_hash"),
    )
    op.create_foreign_key(
        "fk_scene_current_version", "scenes", "scene_versions", ["current_version_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "approvals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("target_type", sa.String(80), nullable=False),
        sa.Column("target_id", UUID, nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("target_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("policy_snapshot", JSONB, nullable=False),
        sa.Column("supersedes_approval_id", UUID, nullable=True),
        sa.Column("actor_id", UUID, nullable=False),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["supersedes_approval_id"], ["approvals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("target_version >= 1", name="ck_approval_target_version"),
        sa.CheckConstraint("target_hash ~ '^[0-9a-f]{64}$'", name="ck_approval_target_hash"),
        sa.CheckConstraint(
            "decision IN ('approved','rejected','override','revoked')", name="ck_approval_decision"
        ),
    )

    for table in (
        "task_model_assignments",
        "prompt_templates",
        "ai_usage_records",
        "script_versions",
        "script_segments",
        "segment_claims",
        "storyboard_versions",
        "scene_versions",
        "approvals",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"
        )


def downgrade() -> None:
    op.drop_constraint("fk_scene_current_version", "scenes", type_="foreignkey")
    op.drop_constraint("fk_storyboard_current_version", "storyboards", type_="foreignkey")
    op.drop_constraint("fk_script_current_version", "scripts", type_="foreignkey")
    for table in (
        "approvals",
        "scene_versions",
        "storyboard_versions",
        "segment_claims",
        "script_segments",
        "script_versions",
        "ai_usage_records",
        "prompt_templates",
        "task_model_assignments",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.drop_table("approvals")
    op.drop_table("scene_versions")
    op.drop_table("scenes")
    op.drop_table("storyboard_versions")
    op.drop_table("storyboards")
    op.drop_table("segment_claims")
    op.drop_table("script_segments")
    op.drop_table("script_versions")
    op.drop_table("scripts")
    op.drop_index("ix_ai_usage_workflow", table_name="ai_usage_records")
    op.drop_table("ai_usage_records")
    op.drop_table("prompt_template_heads")
    op.drop_table("prompt_templates")
    op.drop_table("task_model_assignment_heads")
    op.drop_table("task_model_assignments")
    op.drop_table("models")
    op.drop_table("providers")
