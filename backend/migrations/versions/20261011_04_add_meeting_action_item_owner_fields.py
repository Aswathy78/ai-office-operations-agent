"""Add owner-tracking fields to meeting action items.

Revision ID: 20261011_04
Revises: 20261011_03
Create Date: 2026-10-11
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261011_04"
down_revision: str | None = "20261011_03"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("meeting_action_items") as batch_op:
        batch_op.add_column(
            sa.Column("proposed_employee_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("confirmed_employee_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("employee_match_status", sa.String(length=30), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "ownership_status",
                sa.String(length=30),
                nullable=False,
                server_default="unresolved",
            )
        )
        batch_op.create_foreign_key(
            "fk_meeting_action_items_proposed_employee_id_employees",
            "employees",
            ["proposed_employee_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_meeting_action_items_confirmed_employee_id_employees",
            "employees",
            ["confirmed_employee_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_meeting_action_items_proposed_employee_id",
            ["proposed_employee_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_meeting_action_items_confirmed_employee_id",
            ["confirmed_employee_id"],
            unique=False,
        )
        batch_op.create_check_constraint(
            "ck_meeting_action_items_employee_match_status",
            "employee_match_status IS NULL OR employee_match_status IN "
            "('unique_match', 'ambiguous_match', 'no_match', 'missing_owner')",
        )
        batch_op.create_check_constraint(
            "ck_meeting_action_items_ownership_status",
            "ownership_status IN "
            "('unresolved', 'proposed', 'approval_required', 'confirmed')",
        )


def downgrade() -> None:
    with op.batch_alter_table("meeting_action_items") as batch_op:
        batch_op.drop_constraint(
            "ck_meeting_action_items_ownership_status",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_meeting_action_items_employee_match_status",
            type_="check",
        )
        batch_op.drop_index("ix_meeting_action_items_confirmed_employee_id")
        batch_op.drop_index("ix_meeting_action_items_proposed_employee_id")
        batch_op.drop_constraint(
            "fk_meeting_action_items_confirmed_employee_id_employees",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_meeting_action_items_proposed_employee_id_employees",
            type_="foreignkey",
        )
        batch_op.drop_column("ownership_status")
        batch_op.drop_column("employee_match_status")
        batch_op.drop_column("confirmed_employee_id")
        batch_op.drop_column("proposed_employee_id")
