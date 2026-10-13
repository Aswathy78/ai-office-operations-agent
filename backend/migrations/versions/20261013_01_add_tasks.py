"""Add idempotent tasks created from confirmed meeting action items.

Revision ID: 20261013_01
Revises: 20261012_01
Create Date: 2026-10-13
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261013_01"
down_revision: str | None = "20261012_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("tasks"):
        columns = {
            column["name"]
            for column in inspector.get_columns("tasks")
        }
        if "source_segment_index" not in columns:
            op.add_column(
                "tasks",
                sa.Column(
                    "source_segment_index",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )
            op.execute(
                sa.text(
                    "UPDATE tasks SET source_segment_index = COALESCE("
                    "(SELECT source_segment_index FROM meeting_action_items "
                    "WHERE meeting_action_items.id = tasks.source_action_item_id), 0)"
                )
            )
        return

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("assignee_id", sa.String(length=36), nullable=False),
        sa.Column("creator_id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_action_item_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("transcript_id", sa.String(length=36), nullable=False),
        sa.Column("source_segment_index", sa.Integer(), nullable=False),
        sa.Column("source_timestamp_seconds", sa.Float(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "assignment_email_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("assignment_email_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name="ck_tasks_priority",
        ),
        sa.CheckConstraint(
            "assignment_email_status IN ('pending', 'sent', 'failed')",
            name="ck_tasks_assignment_email_status",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id"],
            ["employees.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_action_item_id"],
            ["meeting_action_items.id"],
            ondelete="CASCADE",
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
            "idempotency_key",
            name="uq_tasks_idempotency_key",
        ),
        sa.UniqueConstraint(
            "source_action_item_id",
            name="uq_tasks_source_action_item_id",
        ),
    )
    op.create_index(
        "ix_tasks_assignee_due_at",
        "tasks",
        ["assignee_id", "due_at"],
        unique=False,
    )
    op.create_index("ix_tasks_meeting_id", "tasks", ["meeting_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tasks_meeting_id", table_name="tasks")
    op.drop_index("ix_tasks_assignee_due_at", table_name="tasks")
    op.drop_table("tasks")
