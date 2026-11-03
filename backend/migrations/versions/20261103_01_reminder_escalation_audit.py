"""Add system_event outcome to task_audit_logs for automated workflows (Story 8.5 / FR-47).

Revision ID: 20261103_01
Revises: 20261102_02
Create Date: 2026-11-03
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261103_01"
down_revision: str | None = "20261102_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

OLD_OUTCOMES = "('applied', 'pending_approval', 'denied', 'rejected')"
NEW_OUTCOMES = "('applied', 'pending_approval', 'denied', 'rejected', 'system_event')"
CHECK_NAME = "ck_task_audit_logs_outcome"


def _task_audit_logs_table(outcomes: str) -> sa.Table:
    """task_audit_logs schema used for batch rebuild on SQLite/PostgreSQL."""
    metadata = sa.MetaData()
    sa.Table("tasks", metadata, sa.Column("id", sa.String(length=36), primary_key=True))
    return sa.Table(
        "task_audit_logs",
        metadata,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("actor_role", sa.String(length=40), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"outcome IN {outcomes}", name=CHECK_NAME),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_task_audit_logs_task_created_at", "task_id", "created_at"),
    )


def _swap_audit_outcome_check(outcomes: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("task_audit_logs"):
        return
    with op.batch_alter_table(
        "task_audit_logs", copy_from=_task_audit_logs_table(outcomes), recreate="always"
    ):
        pass


def upgrade() -> None:
    _swap_audit_outcome_check(NEW_OUTCOMES)


def downgrade() -> None:
    _swap_audit_outcome_check(OLD_OUTCOMES)

