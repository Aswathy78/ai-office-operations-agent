"""Add meeting transcript correction history.

Revision ID: 20261005_01
Revises:
Create Date: 2026-10-05
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261005_01"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "meeting_transcript_corrections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("transcript_id", sa.String(length=36), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("corrected_text", sa.Text(), nullable=False),
        sa.Column("original_speaker", sa.String(length=255), nullable=False),
        sa.Column("corrected_speaker", sa.String(length=255), nullable=False),
        sa.Column("editor_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["meeting_id"],
            ["meetings.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["meeting_transcripts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_meeting_transcript_corrections_meeting_id",
        "meeting_transcript_corrections",
        ["meeting_id"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_transcript_corrections_transcript_created_at",
        "meeting_transcript_corrections",
        ["transcript_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_meeting_transcript_corrections_transcript_created_at",
        table_name="meeting_transcript_corrections",
    )
    op.drop_index(
        "ix_meeting_transcript_corrections_meeting_id",
        table_name="meeting_transcript_corrections",
    )
    op.drop_table("meeting_transcript_corrections")
