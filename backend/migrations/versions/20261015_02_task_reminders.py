"""Task completion and line managers for reminders / escalations (Story 4.4).

Revision ID: 20261015_02
Revises: 20261015_01
Create Date: 2026-10-15
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261015_02"
down_revision: str | None = "20261015_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    task_checks = {check["name"] for check in inspector.get_check_constraints("tasks")}
    # SQLite cannot alter a CHECK constraint in place, so batch mode rebuilds the table.
    with op.batch_alter_table("tasks", recreate="always") as batch:
        if "ck_tasks_status" in task_checks:
            batch.drop_constraint("ck_tasks_status", type_="check")
        batch.create_check_constraint(
            "ck_tasks_status", "status IN ('pending', 'completed')"
        )
        if "completed_at" not in task_columns:
            batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))

    employee_columns = {column["name"] for column in inspector.get_columns("employees")}
    if "manager_id" not in employee_columns:
        with op.batch_alter_table("employees") as batch:
            batch.add_column(sa.Column("manager_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key(
                "fk_employees_manager_id",
                "employees",
                ["manager_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    with op.batch_alter_table("employees") as batch:
        batch.drop_constraint("fk_employees_manager_id", type_="foreignkey")
        batch.drop_column("manager_id")

    op.execute("UPDATE tasks SET status = 'pending' WHERE status = 'completed'")
    with op.batch_alter_table("tasks", recreate="always") as batch:
        batch.drop_constraint("ck_tasks_status", type_="check")
        batch.create_check_constraint("ck_tasks_status", "status IN ('pending')")
        batch.drop_column("completed_at")
