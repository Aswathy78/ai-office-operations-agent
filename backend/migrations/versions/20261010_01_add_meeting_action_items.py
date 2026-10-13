"""Add persisted meeting action item extractions and items.

Revision ID: 20261010_01
Revises: 20261007_01
Create Date: 2026-10-10
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261010_01"
down_revision: str | None = "20261007_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    action_item_tables = {
        "meeting_action_item_extractions",
        "meeting_action_items",
    }
    if action_item_tables.issubset(existing_tables):
        return
    if action_item_tables.intersection(existing_tables):
        raise RuntimeError(
            "Only part of the meeting action-item schema exists; "
            "refusing to mark the migration complete."
        )

    op.create_table(
        "meeting_action_item_extractions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("transcript_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_meeting_action_item_extractions_status",
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
            name="uq_meeting_action_item_extractions_transcript_id",
        ),
    )
    op.create_index(
        "ix_meeting_action_item_extractions_meeting_id",
        "meeting_action_item_extractions",
        ["meeting_id"],
        unique=False,
    )
    op.create_table(
        "meeting_action_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("extraction_id", sa.String(length=36), nullable=False),
        sa.Column("owner_name", sa.String(length=255), nullable=True),
        sa.Column("task_description", sa.Text(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_text", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_segment_index", sa.Integer(), nullable=False),
        sa.Column("source_start_seconds", sa.Float(), nullable=False),
        sa.Column("source_end_seconds", sa.Float(), nullable=False),
        sa.Column("source_quote", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name="ck_meeting_action_items_priority",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_meeting_action_items_confidence_range",
        ),
        sa.CheckConstraint(
            "source_segment_index >= 0",
            name="ck_meeting_action_items_source_segment_index",
        ),
        sa.CheckConstraint(
            "source_start_seconds >= 0 "
            "AND source_end_seconds >= source_start_seconds",
            name="ck_meeting_action_items_source_time_range",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["meeting_action_item_extractions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_id",
            "source_segment_index",
            "task_description",
            name="uq_meeting_action_items_extraction_segment_task",
        ),
    )
    op.create_index(
        "ix_meeting_action_items_extraction_id",
        "meeting_action_items",
        ["extraction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_meeting_action_items_extraction_id",
        table_name="meeting_action_items",
    )
    op.drop_table("meeting_action_items")
    op.drop_index(
        "ix_meeting_action_item_extractions_meeting_id",
        table_name="meeting_action_item_extractions",
    )
    op.drop_table("meeting_action_item_extractions")
