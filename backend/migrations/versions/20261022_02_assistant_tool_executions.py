"""Assistant tool executions and assistant-created tasks (Story 6.5).

* ``assistant_tool_executions``: one row per tool call; the idempotency ledger
  for write tools (unique per actor and idempotency key).
* ``tasks``: the meeting provenance columns become nullable so the assistant can
  create tasks that did not come from a meeting action item.

Revision ID: 20261022_02
Revises: 20261022_01
Create Date: 2026-10-22
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261022_02"
down_revision: str | None = "20261022_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TASK_PROVENANCE_COLUMNS = (
    ("source_action_item_id", sa.String(length=36)),
    ("meeting_id", sa.String(length=36)),
    ("transcript_id", sa.String(length=36)),
    ("source_segment_index", sa.Integer()),
    ("source_timestamp_seconds", sa.Float()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("assistant_tool_executions"):
        op.create_table(
            "assistant_tool_executions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("tool_name", sa.String(length=80), nullable=False),
            sa.Column("actor_id", sa.String(length=36), nullable=False),
            sa.Column("actor_role", sa.String(length=30), nullable=False),
            sa.Column("conversation_id", sa.String(length=36), nullable=True),
            sa.Column("idempotency_key", sa.String(length=200), nullable=True),
            sa.Column("risk_tier", sa.String(length=20), nullable=False),
            sa.Column("arguments", sa.JSON(), nullable=False),
            sa.Column("arguments_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("progress", sa.JSON(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "status IN ('running', 'succeeded', 'failed', 'timed_out')",
                name="ck_assistant_tool_executions_status",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "actor_id", "idempotency_key", name="uq_assistant_tool_executions_actor_key"
            ),
        )
        op.create_index(
            "ix_assistant_tool_executions_actor_created",
            "assistant_tool_executions",
            ["actor_id", "created_at"],
        )

    nullable = {
        column["name"]: column["nullable"] for column in inspector.get_columns("tasks")
    }
    if any(not nullable.get(name, True) for name, _ in _TASK_PROVENANCE_COLUMNS):
        # SQLite cannot relax NOT NULL in place; batch mode rebuilds the table
        # and keeps its data, indexes, foreign keys and named CHECK constraints.
        with op.batch_alter_table("tasks", recreate="always") as batch:
            for name, type_ in _TASK_PROVENANCE_COLUMNS:
                batch.alter_column(name, existing_type=type_, nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    assistant_tasks = bind.execute(
        sa.text("SELECT COUNT(*) FROM tasks WHERE meeting_id IS NULL")
    ).scalar()
    if assistant_tasks:
        raise RuntimeError(
            f"{assistant_tasks} task(s) have no source meeting (created by the AI "
            "assistant). Reassign or remove them before downgrading."
        )
    with op.batch_alter_table("tasks", recreate="always") as batch:
        for name, type_ in _TASK_PROVENANCE_COLUMNS:
            batch.alter_column(name, existing_type=type_, nullable=False)

    op.drop_index(
        "ix_assistant_tool_executions_actor_created",
        table_name="assistant_tool_executions",
    )
    op.drop_table("assistant_tool_executions")
