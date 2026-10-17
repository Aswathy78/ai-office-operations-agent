"""Add employee timezone and working hours for meeting scheduling.

Revision ID: 20261017_01
Revises: 20261016_01
Create Date: 2026-10-17
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261017_01"
down_revision: str | None = "20261016_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("employees")}
    if "timezone_name" not in columns:
        op.add_column("employees", sa.Column("timezone_name", sa.String(length=100), nullable=True))
    if "working_days" not in columns:
        op.add_column(
            "employees",
            sa.Column(
                "working_days",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[0,1,2,3,4]'"),
            ),
        )
    if "working_hours_start" not in columns:
        op.add_column(
            "employees",
            sa.Column("working_hours_start", sa.String(length=5), nullable=False, server_default="09:00"),
        )
    if "working_hours_end" not in columns:
        op.add_column(
            "employees",
            sa.Column("working_hours_end", sa.String(length=5), nullable=False, server_default="17:00"),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("employees")}
    for column in ("working_hours_end", "working_hours_start", "working_days", "timezone_name"):
        if column in columns:
            op.drop_column("employees", column)