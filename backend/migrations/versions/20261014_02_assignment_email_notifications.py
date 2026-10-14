"""Add idempotent assignment email notifications.

Revision ID: 20261014_02
Revises: 20261014_01
Create Date: 2026-10-14
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261014_02"
down_revision: str | None = "20261014_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "assignment_version" not in task_columns:
        op.add_column(
            "tasks",
            sa.Column(
                "assignment_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )

    notification_columns = {
        column["name"]
        for column in inspector.get_columns("task_notifications")
    }
    if "idempotency_key" not in notification_columns:
        op.add_column(
            "task_notifications",
            sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        )

    notification_indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("task_notifications")
    }
    if "uq_task_notifications_idempotency_key" not in notification_indexes:
        op.create_index(
            "uq_task_notifications_idempotency_key",
            "task_notifications",
            ["idempotency_key"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index(
        "uq_task_notifications_idempotency_key",
        table_name="task_notifications",
    )
    with op.batch_alter_table("task_notifications") as batch_op:
        batch_op.drop_column("idempotency_key")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("assignment_version")
