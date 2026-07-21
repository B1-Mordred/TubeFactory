"""semantic chunks and local near-duplicate vectors

Revision ID: 0004_semantic_chunks
Revises: 0003_opp_sources
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision = "0004_semantic_chunks"
down_revision = "0003_opp_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "semantic_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("location_anchor", sa.String(500), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("chunk_hash", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(120), nullable=False),
        sa.Column("embedding", Vector(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_snapshot_id"], ["source_snapshots.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "source_snapshot_id", "chunk_number", name="uq_semantic_chunk_number"
        ),
        sa.UniqueConstraint(
            "source_snapshot_id",
            "chunk_hash",
            "location_anchor",
            name="uq_semantic_chunk_anchor",
        ),
        sa.CheckConstraint("chunk_number >= 1", name="ck_semantic_chunk_number"),
        sa.CheckConstraint("token_count >= 1", name="ck_semantic_chunk_tokens"),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="ck_semantic_chunk_offsets",
        ),
    )
    op.create_index("ix_semantic_chunks_snapshot", "semantic_chunks", ["source_snapshot_id"])
    op.execute(
        "CREATE INDEX ix_semantic_chunks_embedding ON semantic_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE TRIGGER semantic_chunks_immutable BEFORE UPDATE OR DELETE ON semantic_chunks "
        "FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS semantic_chunks_immutable ON semantic_chunks")
    op.drop_index("ix_semantic_chunks_embedding", table_name="semantic_chunks")
    op.drop_index("ix_semantic_chunks_snapshot", table_name="semantic_chunks")
    op.drop_table("semantic_chunks")
