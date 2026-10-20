"""Task deadline calendar sync (Story 5.5 / FR-28).

Revision ID: 20261020_01
Revises: 20261019_02
Create Date: 2026-10-20
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261020_01"
down_revision: str | None = "20261019_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("calendar_deadline_entries"):
        op.create_table(
            "calendar_deadline_entries",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("owner_email", sa.String(length=255), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reminder_minutes_before", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("external_ref", sa.String(length=100), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_calendar_deadline_entries_owner", "calendar_deadline_entries", ["owner_email"])
        op.create_index(
            "ix_calendar_deadline_entries_external_ref", "calendar_deadline_entries", ["external_ref"]
        )

    if not inspector.has_table("task_calendar_syncs"):
        op.create_table(
            "task_calendar_syncs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=False, server_default="local"),
            sa.Column("external_entry_id", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("pending_reason", sa.Text(), nullable=True),
            sa.Column("synced_due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("synced_title", sa.Text(), nullable=True),
            sa.Column("synced_assignee_id", sa.String(length=36), nullable=True),
            sa.Column("synced_owner_email", sa.String(length=255), nullable=True),
            sa.Column("synced_sequence", sa.Integer(), nullable=True),
            sa.Column("synced_entry_version", sa.Integer(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_poll_error", sa.Text(), nullable=True),
            sa.Column("external_change_detected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("external_change_detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'processing', 'completed', 'retrying', 'failed')",
                name="ck_task_calendar_syncs_status",
            ),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_id", name="uq_task_calendar_syncs_task_id"),
        )
        op.create_index(
            "ix_task_calendar_syncs_status_next", "task_calendar_syncs", ["status", "next_attempt_at"]
        )


def downgrade() -> None:
    op.drop_index("ix_task_calendar_syncs_status_next", table_name="task_calendar_syncs")
    op.drop_table("task_calendar_syncs")
    op.drop_index("ix_calendar_deadline_entries_external_ref", table_name="calendar_deadline_entries")
    op.drop_index("ix_calendar_deadline_entries_owner", table_name="calendar_deadline_entries")
    op.drop_table("calendar_deadline_entries")
