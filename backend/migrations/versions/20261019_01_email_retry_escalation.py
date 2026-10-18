"""E-mail retry escalation, bounce recording and failure notifications (Story 4.6 / FR-23).

Revision ID: 20261019_01
Revises: 20261018_01
Create Date: 2026-10-19
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261019_01"
down_revision: str | None = "20261018_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("email_drafts")}
    with op.batch_alter_table("email_drafts") as batch:
        if "bounces" not in columns:
            batch.add_column(sa.Column("bounces", sa.JSON(), nullable=False, server_default="[]"))
        if "escalated_at" not in columns:
            batch.add_column(sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True))

    if not inspector.has_table("email_failure_notifications"):
        op.create_table(
            "email_failure_notifications",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("draft_id", sa.String(length=36), nullable=False),
            sa.Column("failure_event_sequence", sa.Integer(), nullable=False),
            sa.Column("audience", sa.String(length=20), nullable=False),
            sa.Column("channel", sa.String(length=20), nullable=False),
            sa.Column("recipient_email", sa.String(length=255), nullable=True),
            sa.Column("subject", sa.Text(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("idempotency_key", sa.String(length=300), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provider", sa.String(length=50), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'sent', 'failed', 'recorded')",
                name="ck_email_failure_notifications_status",
            ),
            sa.CheckConstraint(
                "audience IN ('requester', 'admin')",
                name="ck_email_failure_notifications_audience",
            ),
            sa.CheckConstraint(
                "channel IN ('email', 'in_app')",
                name="ck_email_failure_notifications_channel",
            ),
            sa.ForeignKeyConstraint(["draft_id"], ["email_drafts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("idempotency_key", name="uq_email_failure_notifications_idempotency_key"),
        )
        op.create_index(
            "ix_email_failure_notifications_draft",
            "email_failure_notifications",
            ["draft_id", "created_at"],
        )
        op.create_index(
            "ix_email_failure_notifications_status_next",
            "email_failure_notifications",
            ["status", "next_attempt_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_email_failure_notifications_status_next", table_name="email_failure_notifications")
    op.drop_index("ix_email_failure_notifications_draft", table_name="email_failure_notifications")
    op.drop_table("email_failure_notifications")
    with op.batch_alter_table("email_drafts") as batch:
        batch.drop_column("escalated_at")
        batch.drop_column("bounces")
