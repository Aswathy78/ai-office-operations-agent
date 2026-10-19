"""Add meeting cancellation approvals and audit history (Story 5.4).

Revision ID: 20261018_02
Revises: 20261018_01
Create Date: 2026-10-18
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261018_02"
down_revision: str | None = "20261018_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("meeting_cancellation_requests"):
        op.create_table(
            "meeting_cancellation_requests",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("meeting_id", sa.String(length=36), nullable=False),
            sa.Column("requested_by", sa.String(length=36), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("decided_by", sa.String(length=36), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decision_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'approved', 'rejected')",
                name="ck_meeting_cancellation_requests_status",
            ),
            sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_meeting_cancellation_requests_meeting_status",
            "meeting_cancellation_requests",
            ["meeting_id", "status"],
            unique=False,
        )

    if not inspector.has_table("meeting_audit_logs"):
        op.create_table(
            "meeting_audit_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("meeting_id", sa.String(length=36), nullable=False),
            sa.Column("actor_id", sa.String(length=36), nullable=False),
            sa.Column("action", sa.String(length=50), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_meeting_audit_logs_meeting_created_at",
            "meeting_audit_logs",
            ["meeting_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("meeting_audit_logs"):
        op.drop_index("ix_meeting_audit_logs_meeting_created_at", table_name="meeting_audit_logs")
        op.drop_table("meeting_audit_logs")
    if inspector.has_table("meeting_cancellation_requests"):
        op.drop_index(
            "ix_meeting_cancellation_requests_meeting_status",
            table_name="meeting_cancellation_requests",
        )
        op.drop_table("meeting_cancellation_requests")