"""Add versioned document chunks and indexing state — Story 7.2.

Revision ID: 20261028_01
Revises: 20261027_01
Create Date: 2026-10-28
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261028_01"
down_revision: str | None = "20261027_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    document_columns = {
        column["name"] for column in inspector.get_columns("documents")
    }
    if "version" not in document_columns:
        op.add_column(
            "documents",
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
    if "indexing_status" not in document_columns:
        op.add_column(
            "documents",
            sa.Column(
                "indexing_status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
        )
    if "indexing_failure_reason" not in document_columns:
        op.add_column(
            "documents",
            sa.Column("indexing_failure_reason", sa.Text(), nullable=True),
        )

    if not inspector.has_table("document_chunks"):
        op.create_table(
            "document_chunks",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("document_version", sa.Integer(), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("heading", sa.Text(), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False),
            sa.Column("department", sa.String(length=100), nullable=True),
            sa.Column("acl_employee_ids", sa.JSON(), nullable=False),
            sa.Column("embedding", sa.JSON(), nullable=False),
            sa.Column("embedding_model", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["document_id"],
                ["documents.id"],
                ondelete="CASCADE",
                name="fk_document_chunks_document_id",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "document_id",
                "document_version",
                "chunk_index",
                name="uq_document_chunks_version_index",
            ),
        )
        op.create_index(
            "ix_document_chunks_version",
            "document_chunks",
            ["document_id", "document_version"],
        )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_version", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_column("documents", "indexing_failure_reason")
    op.drop_column("documents", "indexing_status")
    op.drop_column("documents", "version")
