"""Add employees table.

Revision ID: 20261011_03
Revises: 20261011_02
Create Date: 2026-10-11
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261011_03"
down_revision: str | None = "20261011_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "employees",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_employees_email"),
    )


def downgrade() -> None:
    op.drop_table("employees")
