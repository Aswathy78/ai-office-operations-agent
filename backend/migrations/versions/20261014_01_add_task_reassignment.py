"""Add employee teams, task reassignment requests, audit log and notifications.

Revision ID: 20261014_01
Revises: 20261013_01
Create Date: 2026-10-14
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261014_01"
down_revision: str | None = "20261013_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if "team" not in _columns(inspector, "employees"):
        op.add_column(
            "employees",
            sa.Column("team", sa.String(length=100), nullable=True),
        )

    task_columns = _columns(inspector, "tasks")
    if "calendar_sequence" not in task_columns:
        op.add_column(
            "tasks",
            sa.Column(
                "calendar_sequence",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if "updated_at" not in task_columns:
        op.add_column(
            "tasks",
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not inspector.has_table("task_reassignment_requests"):
        op.create_table(
            "task_reassignment_requests",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("from_assignee_id", sa.String(length=36), nullable=False),
            sa.Column("to_assignee_id", sa.String(length=36), nullable=False),
            sa.Column("requested_by", sa.String(length=36), nullable=False),
            sa.Column("requester_role", sa.String(length=40), nullable=False),
            sa.Column("requester_team", sa.String(length=100), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("decided_by", sa.String(length=36), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decision_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'approved', 'rejected')",
                name="ck_task_reassignment_requests_status",
            ),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["from_assignee_id"], ["employees.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["to_assignee_id"], ["employees.id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_task_reassignment_requests_task_status",
            "task_reassignment_requests",
            ["task_id", "status"],
            unique=False,
        )

    if not inspector.has_table("task_audit_logs"):
        op.create_table(
            "task_audit_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("actor_id", sa.String(length=36), nullable=False),
            sa.Column("actor_role", sa.String(length=40), nullable=False),
            sa.Column("action", sa.String(length=40), nullable=False),
            sa.Column("outcome", sa.String(length=20), nullable=False),
            sa.Column("detail", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "outcome IN ('applied', 'pending_approval', 'denied', 'rejected')",
                name="ck_task_audit_logs_outcome",
            ),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_task_audit_logs_task_created_at",
            "task_audit_logs",
            ["task_id", "created_at"],
            unique=False,
        )

    if not inspector.has_table("task_notifications"):
        op.create_table(
            "task_notifications",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("recipient_employee_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=40), nullable=False),
            sa.Column("subject", sa.Text(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'sent', 'failed')",
                name="ck_task_notifications_status",
            ),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["recipient_employee_id"], ["employees.id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_task_notifications_task_created_at",
            "task_notifications",
            ["task_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_task_notifications_task_created_at", table_name="task_notifications")
    op.drop_table("task_notifications")
    op.drop_index("ix_task_audit_logs_task_created_at", table_name="task_audit_logs")
    op.drop_table("task_audit_logs")
    op.drop_index(
        "ix_task_reassignment_requests_task_status",
        table_name="task_reassignment_requests",
    )
    op.drop_table("task_reassignment_requests")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("calendar_sequence")
    with op.batch_alter_table("employees") as batch_op:
        batch_op.drop_column("team")
