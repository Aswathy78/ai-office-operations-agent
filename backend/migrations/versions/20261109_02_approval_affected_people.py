"""Persist frozen people affected by an assistant approval.

Revision ID: 20261109_02
Revises: 20261109_01
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261109_02"
down_revision: str | None = "20261109_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    table_name = "assistant_action_approvals"
    if not inspector.has_table(table_name):
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if "affected_people" not in columns:
        op.add_column(
            table_name,
            sa.Column("affected_people", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    table_name = "assistant_action_approvals"
    if inspector.has_table(table_name) and "affected_people" in {
        column["name"] for column in inspector.get_columns(table_name)
    }:
        with op.batch_alter_table(table_name) as batch:
            batch.drop_column("affected_people")
