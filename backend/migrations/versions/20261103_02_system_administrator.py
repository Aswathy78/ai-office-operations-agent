"""Create system_administrators table for administrator database authentication (Story 8.6 / FR-48).

Revision ID: 20261103_02
Revises: 20261103_01
Create Date: 2026-11-03
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261103_02"
down_revision: str | None = "20261103_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("system_administrators"):
        op.create_table(
            "system_administrators",
            sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
            sa.Column("employee_id", sa.String(length=36), nullable=False),
            sa.Column("email_normalized", sa.String(length=255), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("full_name", sa.String(length=255), nullable=False, server_default="System Administrator"),
            sa.Column("role", sa.String(length=30), nullable=False, server_default="admin"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], name="fk_system_administrators_employee_id", ondelete="CASCADE"),
            sa.UniqueConstraint("email_normalized", name="uq_system_administrators_email"),
            sa.UniqueConstraint("employee_id", name="uq_system_administrators_employee_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("system_administrators"):
        op.drop_table("system_administrators")

