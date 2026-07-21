"""deterministic operating policy and required opportunity rationale

Revision ID: 0018_operating_policy
Revises: 0017_identity_hardening
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0018_operating_policy"
down_revision = "0017_identity_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "opportunities",
        sa.Column("editorial_rationale", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "opportunities",
        sa.Column("policy_snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    # Research workers insert discovered opportunities with narrow SQL. Keep
    # fail-closed empty/default values until a reviewer supplies the rationale
    # and the API binds the current policy snapshot at shortlist time.


def downgrade() -> None:
    op.drop_column("opportunities", "policy_snapshot")
    op.drop_column("opportunities", "editorial_rationale")
