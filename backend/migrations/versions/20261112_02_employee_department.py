"""Add an explicit employee department without inferring existing values.

Revision ID: 20261112_02
Revises: 20261112_01
Create Date: 2026-11-12
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261112_02"
down_revision: str | None = "20261112_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("employees") and "department" not in {
        column["name"] for column in inspector.get_columns("employees")
    }:
        with op.batch_alter_table("employees") as batch:
            batch.add_column(sa.Column("department", sa.String(length=100), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("employees") and "department" in {
        column["name"] for column in inspector.get_columns("employees")
    }:
        with op.batch_alter_table("employees") as batch:
            batch.drop_column("department")
