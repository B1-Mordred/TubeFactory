"""optional TOTP, durable login throttling and configurable OIDC

Revision ID: 0017_identity_hardening
Revises: 0016_operational_evidence
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0017_identity_hardening"
down_revision = "0016_operational_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("identity_provider", sa.String(20), nullable=False, server_default="local"))
    op.add_column("users", sa.Column("oidc_subject", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("totp_recovery_hashes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.alter_column("users", "identity_provider", server_default=None)
    op.alter_column("users", "totp_enabled", server_default=None)
    op.alter_column("users", "totp_recovery_hashes", server_default=None)
    op.create_check_constraint("ck_user_identity_provider", "users", "identity_provider IN ('local','oidc')")
    op.create_check_constraint(
        "ck_user_identity_credentials", "users",
        "(identity_provider = 'local' AND password_hash IS NOT NULL AND oidc_subject IS NULL) OR "
        "(identity_provider = 'oidc' AND password_hash IS NULL AND oidc_subject IS NOT NULL)",
    )
    op.create_unique_constraint("uq_user_oidc_subject", "users", ["oidc_subject"])

    op.create_table(
        "authentication_rate_limits",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("failure_count >= 0", name="ck_auth_rate_limit_failures"),
    )

    op.create_table(
        "oidc_configuration_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("version_number", sa.Integer(), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("client_id", sa.String(500), nullable=False),
        sa.Column("client_secret_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("authorization_endpoint", sa.String(1000), nullable=False),
        sa.Column("token_endpoint", sa.String(1000), nullable=False),
        sa.Column("jwks_uri", sa.String(1000), nullable=False),
        sa.Column("scopes", JSONB, nullable=False),
        sa.Column("username_claim", sa.String(120), nullable=False),
        sa.Column("display_name_claim", sa.String(120), nullable=False),
        sa.Column("role_claim", sa.String(120), nullable=False),
        sa.Column("role_mapping", JSONB, nullable=False),
        sa.Column("default_role", sa.String(20), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("content_hash", name="uq_oidc_configuration_hash"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_oidc_configuration_hash"),
        sa.CheckConstraint("default_role IS NULL OR default_role IN ('admin','operator','editor','reviewer','viewer')", name="ck_oidc_default_role"),
    )
    op.execute("CREATE TRIGGER oidc_configuration_immutable BEFORE UPDATE OR DELETE ON oidc_configuration_versions FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()")
    op.create_table(
        "oidc_configuration_heads",
        sa.Column("singleton", sa.Boolean(), primary_key=True),
        sa.Column("active_version_id", UUID, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["active_version_id"], ["oidc_configuration_versions.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("singleton", name="ck_oidc_configuration_singleton"),
    )
    op.create_table(
        "oidc_authentication_states",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("code_verifier_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("configuration_version_id", UUID, nullable=False),
        sa.Column("redirect_uri", sa.String(1000), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["configuration_version_id"], ["oidc_configuration_versions.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("oidc_authentication_states")
    op.drop_table("oidc_configuration_heads")
    op.execute("DROP TRIGGER IF EXISTS oidc_configuration_immutable ON oidc_configuration_versions")
    op.drop_table("oidc_configuration_versions")
    op.drop_table("authentication_rate_limits")
    op.drop_constraint("uq_user_oidc_subject", "users", type_="unique")
    op.drop_constraint("ck_user_identity_credentials", "users", type_="check")
    op.drop_constraint("ck_user_identity_provider", "users", type_="check")
    for column in ("totp_recovery_hashes", "totp_enabled", "oidc_subject", "identity_provider"):
        op.drop_column("users", column)
