"""Add review status, reviewer, and review timestamp to meeting action items.

Revision ID: 20261012_01
Revises: 20261011_04
Create Date: 2026-10-12
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261012_01"
down_revision: str | None = "20261011_04"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("meeting_action_items") as batch_op:
        batch_op.add_column(
            sa.Column(
                "review_status",
                sa.String(length=30),
                nullable=False,
                server_default="pending_review",
            )
        )
        batch_op.add_column(
            sa.Column("reviewed_by", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "reviewed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_meeting_action_items_review_status",
            ["review_status"],
            unique=False,
        )
        batch_op.create_check_constraint(
            "ck_meeting_action_items_review_status",
            "review_status IN ('pending_review', 'approval_required', 'confirmed', 'rejected')",
        )


def downgrade() -> None:
    with op.batch_alter_table("meeting_action_items") as batch_op:
        batch_op.drop_constraint(
            "ck_meeting_action_items_review_status",
            type_="check",
        )
        batch_op.drop_index("ix_meeting_action_items_review_status")
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("reviewed_by")
        batch_op.drop_column("review_status")
