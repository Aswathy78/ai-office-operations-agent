"""Add meeting minutes and recipient delivery tracking.

Revision ID: 20261014_03
Revises: 20261014_02
Create Date: 2026-10-14
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261014_03"
down_revision: str | None = "20261014_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("meeting_minutes"):
        op.create_table(
            "meeting_minutes",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("meeting_id", sa.String(length=36), nullable=False),
            sa.Column("summary_id", sa.String(length=36), nullable=True),
            sa.Column(
                "status",
                sa.String(length=30),
                nullable=False,
                server_default="pending_approval",
            ),
            sa.Column(
                "policy",
                sa.String(length=30),
                nullable=False,
                server_default="manual_approval",
            ),
            sa.Column("approved_by", sa.String(length=36), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("content_summary", sa.Text(), nullable=True),
            sa.Column("content_decisions", sa.JSON(), nullable=True),
            sa.Column("content_action_items", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('draft', 'pending_approval', 'approved', 'sent', 'failed')",
                name="ck_meeting_minutes_status",
            ),
            sa.CheckConstraint(
                "policy IN ('manual_approval', 'auto_send')",
                name="ck_meeting_minutes_policy",
            ),
            sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["summary_id"], ["meeting_summaries.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_meeting_minutes_meeting_id",
            "meeting_minutes",
            ["meeting_id"],
            unique=False,
        )

    if not inspector.has_table("meeting_minutes_recipients"):
        op.create_table(
            "meeting_minutes_recipients",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("minutes_id", sa.String(length=36), nullable=False),
            sa.Column("recipient_name", sa.String(length=255), nullable=False),
            sa.Column("recipient_email", sa.String(length=255), nullable=False),
            sa.Column("attendance_log_id", sa.String(length=36), nullable=True),
            sa.Column(
                "delivery_status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("idempotency_key", sa.String(length=200), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "delivery_status IN ('pending', 'sent', 'failed')",
                name="ck_meeting_minutes_recipients_delivery_status",
            ),
            sa.UniqueConstraint(
                "minutes_id",
                "recipient_email",
                name="uq_meeting_minutes_recipients_minutes_email",
            ),
            sa.ForeignKeyConstraint(
                ["minutes_id"], ["meeting_minutes.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["attendance_log_id"],
                ["meeting_attendance_logs.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "uq_meeting_minutes_recipients_idempotency_key",
            "meeting_minutes_recipients",
            ["idempotency_key"],
            unique=True,
        )
        op.create_index(
            "ix_meeting_minutes_recipients_minutes_id",
            "meeting_minutes_recipients",
            ["minutes_id"],
            unique=False,
        )
        op.create_index(
            "ix_meeting_minutes_recipients_delivery_status",
            "meeting_minutes_recipients",
            ["delivery_status"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_meeting_minutes_recipients_delivery_status",
        table_name="meeting_minutes_recipients",
    )
    op.drop_index(
        "ix_meeting_minutes_recipients_minutes_id",
        table_name="meeting_minutes_recipients",
    )
    op.drop_index(
        "uq_meeting_minutes_recipients_idempotency_key",
        table_name="meeting_minutes_recipients",
    )
    op.drop_table("meeting_minutes_recipients")
    op.drop_index("ix_meeting_minutes_meeting_id", table_name="meeting_minutes")
    op.drop_table("meeting_minutes")
