"""Retain superseded document chunks as soft-deleted history — Story 7.6.

Revision ID: 20261101_01
Revises: 20261031_01
Create Date: 2026-11-01
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261101_01"
down_revision: str | None = "20261031_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("document_chunks")
    }
    if "soft_deleted_at" not in columns:
        op.add_column(
            "document_chunks",
            sa.Column("soft_deleted_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("document_chunks")
    }
    if "soft_deleted_at" in columns:
        op.drop_column("document_chunks", "soft_deleted_at")
