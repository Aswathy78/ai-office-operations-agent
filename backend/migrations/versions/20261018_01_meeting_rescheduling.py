"""Add meeting reschedule requests and meeting delegates tables (Story 5.3).

Revision ID: 20261018_01
Revises: 20261017_01
Create Date: 2026-10-18
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261018_01"
down_revision: str | None = "20261017_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("meeting_reschedule_requests"):
        op.create_table(
            "meeting_reschedule_requests",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("meeting_id", sa.String(length=36), nullable=False),
            sa.Column("requested_by", sa.String(length=36), nullable=False),
            sa.Column("requester_role", sa.String(length=40), nullable=True),
            sa.Column("new_start_at", sa.DateTime(), nullable=False),
            sa.Column("new_end_at", sa.DateTime(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("decided_by", sa.String(length=36), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decision_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'approved', 'rejected')",
                name="ck_meeting_reschedule_requests_status",
            ),
            sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_meeting_reschedule_requests_meeting_status",
            "meeting_reschedule_requests",
            ["meeting_id", "status"],
            unique=False,
        )

    if not inspector.has_table("meeting_delegates"):
        op.create_table(
            "meeting_delegates",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("organizer_id", sa.String(length=36), nullable=False),
            sa.Column("delegate_id", sa.String(length=36), nullable=False),
            sa.Column("meeting_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["organizer_id"], ["employees.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["delegate_id"], ["employees.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "organizer_id",
                "delegate_id",
                "meeting_id",
                name="uq_meeting_delegates_organizer_delegate_meeting",
            ),
        )
        op.create_index(
            "ix_meeting_delegates_organizer_delegate",
            "meeting_delegates",
            ["organizer_id", "delegate_id"],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("meeting_delegates"):
        op.drop_table("meeting_delegates")
    if inspector.has_table("meeting_reschedule_requests"):
        op.drop_table("meeting_reschedule_requests")
