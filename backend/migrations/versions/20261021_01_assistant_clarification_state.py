"""Persist pending assistant clarification state (Story 6.3).

Revision ID: 20261021_01
Revises: 20261020_02
Create Date: 2026-10-21
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261021_01"
down_revision: str | None = "20261020_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversations",
        sa.Column("pending_clarification", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_conversations", "pending_clarification")
