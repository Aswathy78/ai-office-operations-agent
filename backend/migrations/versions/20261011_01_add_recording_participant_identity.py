"""Add participant identity fields to meeting recordings.

Revision ID: 20261011_01
Revises: 20261010_01
Create Date: 2026-10-11
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261011_01"
down_revision: str | None = "20261010_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "meeting_recordings",
        sa.Column("participant_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "meeting_recordings",
        sa.Column("connection_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meeting_recordings", "connection_id")
    op.drop_column("meeting_recordings", "participant_name")
