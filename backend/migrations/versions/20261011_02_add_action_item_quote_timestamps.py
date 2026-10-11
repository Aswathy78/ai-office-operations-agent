"""Add exact action-item quote timestamps.

Revision ID: 20261011_02
Revises: 20261011_01
Create Date: 2026-10-11
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261011_02"
down_revision: str | None = "20261011_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "meeting_action_items",
        sa.Column("source_quote_start_seconds", sa.Float(), nullable=True),
    )
    op.add_column(
        "meeting_action_items",
        sa.Column("source_quote_end_seconds", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meeting_action_items", "source_quote_end_seconds")
    op.drop_column("meeting_action_items", "source_quote_start_seconds")
