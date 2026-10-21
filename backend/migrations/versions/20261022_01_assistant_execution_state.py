"""Add execution_state column to assistant_conversations (Story 6.4).

Stores the JSON execution checkpoint produced by the Plan & Retrieve planner.
This column is intentionally separate from pending_clarification (Story 6.3)
so that clarification state and plan-execution state never overload each other.

Revision ID: 20261022_01
Revises: 20261021_01
Create Date: 2026-10-22
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261022_01"
down_revision: str | None = "20261021_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversations",
        sa.Column("execution_state", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_conversations", "execution_state")
