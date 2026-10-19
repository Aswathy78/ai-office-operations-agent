"""Add provisioned employee authentication accounts and revocable sessions.

Revision ID: 20261018_03
Revises: 20261018_02
Create Date: 2026-10-18
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261018_03"
down_revision: str | None = "20261018_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("auth_accounts"):
        op.create_table(
            "auth_accounts",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("employee_id", sa.String(length=36), nullable=False),
            sa.Column("email_normalized", sa.String(length=255), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=30), nullable=False, server_default="employee"),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "role IN ('employee', 'manager', 'hr_operations', 'admin')",
                name="ck_auth_accounts_role",
            ),
            sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("employee_id", name="uq_auth_accounts_employee_id"),
            sa.UniqueConstraint("email_normalized", name="uq_auth_accounts_email_normalized"),
        )
        op.create_index(
            "ix_auth_accounts_enabled_role",
            "auth_accounts",
            ["is_enabled", "role"],
            unique=False,
        )

    if not inspector.has_table("auth_sessions"):
        op.create_table(
            "auth_sessions",
            sa.Column("jti", sa.String(length=36), nullable=False),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["auth_accounts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("jti"),
        )
        op.create_index(
            "ix_auth_sessions_account_expires",
            "auth_sessions",
            ["account_id", "expires_at"],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("auth_sessions"):
        op.drop_index("ix_auth_sessions_account_expires", table_name="auth_sessions")
        op.drop_table("auth_sessions")
    if inspector.has_table("auth_accounts"):
        op.drop_index("ix_auth_accounts_enabled_role", table_name="auth_accounts")
        op.drop_table("auth_accounts")