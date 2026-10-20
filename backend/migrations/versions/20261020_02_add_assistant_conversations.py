"""Add assistant conversations and messages tables (Epic 6.1 / FR-30).

Revision ID: 20261020_02
Revises: 20261020_01
Create Date: 2026-10-20
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261020_02"
down_revision: str | None = "20261020_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("assistant_conversations"):
        op.create_table(
            "assistant_conversations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["employees.id"],
                name="fk_assistant_conversations_user_id",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_assistant_conversations_user_id",
            "assistant_conversations",
            ["user_id"],
        )

    if not inspector.has_table("assistant_messages"):
        op.create_table(
            "assistant_messages",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("conversation_id", sa.String(length=36), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["assistant_conversations.id"],
                name="fk_assistant_messages_conversation_id",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_assistant_messages_conversation_id",
            "assistant_messages",
            ["conversation_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if inspector.has_table("assistant_messages"):
        op.drop_table("assistant_messages")

    if inspector.has_table("assistant_conversations"):
        op.drop_table("assistant_conversations")
