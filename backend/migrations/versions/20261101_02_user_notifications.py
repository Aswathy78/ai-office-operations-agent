"""Add recipient-scoped notifications and preferences for Story 8.1 / FR-43.

Revision ID: 20261101_02
Revises: 20261101_01
Create Date: 2026-11-01
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261101_02"
down_revision: str | None = "20261101_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("user_notifications"):
        op.create_table(
            "user_notifications",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("recipient_employee_id", sa.String(length=36), nullable=False),
            sa.Column("event_type", sa.String(length=60), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("entity_type", sa.String(length=60), nullable=True),
            sa.Column("entity_id", sa.String(length=100), nullable=True),
            sa.Column("entity_url", sa.String(length=500), nullable=True),
            sa.Column("idempotency_key", sa.String(length=300), nullable=False),
            sa.Column("in_app_status", sa.String(length=20), nullable=False, server_default="delivered"),
            sa.Column("email_status", sa.String(length=20), nullable=False, server_default="disabled"),
            sa.Column("email_subject", sa.Text(), nullable=True),
            sa.Column("email_body", sa.Text(), nullable=True),
            sa.Column("email_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("email_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("email_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("email_provider", sa.String(length=50), nullable=True),
            sa.Column("email_error", sa.Text(), nullable=True),
            sa.Column("task_notification_id", sa.String(length=36), nullable=True),
            sa.Column("email_failure_notification_id", sa.String(length=36), nullable=True),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "in_app_status IN ('delivered', 'disabled')",
                name="ck_user_notifications_in_app_status",
            ),
            sa.CheckConstraint(
                "email_status IN ('disabled', 'pending', 'deferred', 'sending', "
                "'retrying', 'sent', 'failed')",
                name="ck_user_notifications_email_status",
            ),
            sa.ForeignKeyConstraint(
                ["recipient_employee_id"], ["employees.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["task_notification_id"], ["task_notifications.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["email_failure_notification_id"],
                ["email_failure_notifications.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("idempotency_key", name="uq_user_notifications_idempotency_key"),
            sa.UniqueConstraint("task_notification_id", name="uq_user_notifications_task_notification"),
        )
        op.create_index(
            "ix_user_notifications_recipient_created",
            "user_notifications",
            ["recipient_employee_id", "created_at"],
        )
        op.create_index(
            "ix_user_notifications_email_due",
            "user_notifications",
            ["email_status", "email_next_attempt_at"],
        )

    if not inspector.has_table("notification_preferences"):
        op.create_table(
            "notification_preferences",
            sa.Column("employee_id", sa.String(length=36), nullable=False),
            sa.Column("in_app_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("quiet_hours_start", sa.String(length=5), nullable=True),
            sa.Column("quiet_hours_end", sa.String(length=5), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "(quiet_hours_start IS NULL AND quiet_hours_end IS NULL) OR "
                "(quiet_hours_start IS NOT NULL AND quiet_hours_end IS NOT NULL)",
                name="ck_notification_preferences_quiet_hours_pair",
            ),
            sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("employee_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("notification_preferences"):
        op.drop_table("notification_preferences")
    if inspector.has_table("user_notifications"):
        op.drop_index(
            "ix_user_notifications_email_due",
            table_name="user_notifications",
        )
        op.drop_index(
            "ix_user_notifications_recipient_created",
            table_name="user_notifications",
        )
        op.drop_table("user_notifications")
