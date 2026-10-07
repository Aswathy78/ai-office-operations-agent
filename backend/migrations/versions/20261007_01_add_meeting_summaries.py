"""Add persisted meeting summaries.

Revision ID: 20261007_01
Revises: 20261005_01
Create Date: 2026-10-07
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261007_01"
down_revision: str | None = "20261005_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "meeting_summaries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("transcript_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("decisions", sa.JSON(), nullable=True),
        sa.Column("discussion_topics", sa.JSON(), nullable=True),
        sa.Column("open_questions", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_meeting_summaries_status",
        ),
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
        sa.UniqueConstraint(
            "transcript_id",
            name="uq_meeting_summaries_transcript_id",
        ),
    )
    op.create_index(
        "ix_meeting_summaries_meeting_id",
        "meeting_summaries",
        ["meeting_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_meeting_summaries_meeting_id",
        table_name="meeting_summaries",
    )
    op.drop_table("meeting_summaries")
