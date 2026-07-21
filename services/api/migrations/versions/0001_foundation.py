"""foundation identity audit configuration and idempotency

Revision ID: 0001_foundation
Revises:
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    role = postgresql.ENUM(
        "admin",
        "operator",
        "editor",
        "reviewer",
        "viewer",
        name="user_role",
        create_type=False,
    )
    op.execute("CREATE TYPE user_role AS ENUM ('admin','operator','editor','reviewer','viewer')")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("role", role, nullable=False),
        sa.Column("totp_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint("version >= 1", name="ck_users_version_positive"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(160), nullable=False),
        sa.Column("target_type", sa.String(120), nullable=True),
        sa.Column("target_id", sa.String(160), nullable=True),
        sa.Column("correlation_id", sa.String(160), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("event_hash", name="uq_audit_events_event_hash"),
    )
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"])

    op.create_table(
        "configuration_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("namespace", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(40), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("document_hash", sa.String(64), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("namespace", "version", name="uq_config_namespace_version"),
        sa.UniqueConstraint("namespace", "document_hash", name="uq_config_namespace_hash"),
        sa.CheckConstraint("version >= 1", name="ck_config_version_positive"),
    )

    op.create_table(
        "configuration_heads",
        sa.Column("namespace", sa.String(120), primary_key=True),
        sa.Column("active_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["active_version_id"], ["configuration_versions.id"], ondelete="RESTRICT"
        ),
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(240), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(240), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
        sa.CheckConstraint(
            "status IN ('pending','succeeded','failed','indeterminate')",
            name="ck_idempotency_status",
        ),
    )

    op.execute(
        """
        CREATE FUNCTION reject_immutable_row_change() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_immutable
          BEFORE UPDATE OR DELETE ON audit_events
          FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()
        """
    )
    op.execute(
        """
        CREATE TRIGGER configuration_versions_immutable
          BEFORE UPDATE OR DELETE ON configuration_versions
          FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS configuration_versions_immutable ON configuration_versions")
    op.execute("DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS reject_immutable_row_change")
    op.drop_table("idempotency_records")
    op.drop_table("configuration_heads")
    op.drop_table("configuration_versions")
    op.drop_index("ix_audit_events_correlation_id", table_name="audit_events")
    op.drop_index("ix_audit_events_occurred_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("users")
    postgresql.ENUM(name="user_role").drop(op.get_bind(), checkfirst=True)
