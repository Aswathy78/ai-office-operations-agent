"""Approval reminders, expiry notifications, and alternate approvers (Story 9.7).

Revision ID: 20261109_01
Revises: 20261108_02
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261109_01"
down_revision: str | None = "20261108_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    table_name = "assistant_action_approvals"
    if not inspector.has_table(table_name):
        return

    existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
    new_columns = (
        ("alternate_approver_id", sa.String(length=36)),
        ("reminder_sent_at", sa.DateTime(timezone=True)),
        ("expiry_notified_at", sa.DateTime(timezone=True)),
    )
    for name, column_type in new_columns:
        if name not in existing_columns:
            op.add_column(
                table_name,
                sa.Column(name, column_type, nullable=True),
            )

    index_name = "ix_assistant_action_approvals_alternate_status"
    indexes = {index["name"]: index for index in sa.inspect(connection).get_indexes(table_name)}
    if index_name not in indexes:
        op.create_index(
            index_name,
            table_name,
            ["alternate_approver_id", "status"],
        )
    elif indexes[index_name].get("column_names") != ["alternate_approver_id", "status"]:
        raise RuntimeError(f"Existing index {index_name!r} has an unexpected definition.")


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_action_approvals_alternate_status",
        table_name="assistant_action_approvals",
    )
    with op.batch_alter_table("assistant_action_approvals") as batch:
        batch.drop_column("expiry_notified_at")
        batch.drop_column("reminder_sent_at")
        batch.drop_column("alternate_approver_id")
