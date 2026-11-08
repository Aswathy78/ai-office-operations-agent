"""Persist interactive assistant run and step traces (Story 9.5).

Revision ID: 20261108_01
Revises: 20261104_01
Create Date: 2026-11-08
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261108_01"
down_revision: str | None = "20261104_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("assistant_runs"):
        op.create_table(
            "assistant_runs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("conversation_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('running', 'awaiting_approval', 'completed', 'failed', 'cancelled')",
                name="ck_assistant_runs_status",
            ),
            sa.ForeignKeyConstraint(["user_id"], ["employees.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["assistant_conversations.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_assistant_runs_user_started", "assistant_runs", ["user_id", "started_at"]
        )
        op.create_index(
            "ix_assistant_runs_status_started", "assistant_runs", ["status", "started_at"]
        )
        op.create_index(
            "ix_assistant_runs_conversation", "assistant_runs", ["conversation_id", "started_at"]
        )

    if not inspector.has_table("assistant_run_steps"):
        op.create_table(
            "assistant_run_steps",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("step_order", sa.Integer(), nullable=False),
            sa.Column("step_type", sa.String(length=60), nullable=False),
            sa.Column("step_name", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("input_data", sa.JSON(), nullable=True),
            sa.Column("output_data", sa.JSON(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_tokens", sa.Integer(), nullable=True),
            sa.Column("provider_prompt_tokens", sa.Integer(), nullable=True),
            sa.Column("provider_completion_tokens", sa.Integer(), nullable=True),
            sa.Column("provider_total_tokens", sa.Integer(), nullable=True),
            sa.Column("tool_execution_id", sa.String(length=36), nullable=True),
            sa.Column("approval_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN "
                "('pending', 'running', 'awaiting_approval', 'completed', 'failed', "
                "'skipped', 'escalated', 'cancelled')",
                name="ck_assistant_run_steps_status",
            ),
            sa.ForeignKeyConstraint(["run_id"], ["assistant_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["tool_execution_id"], ["assistant_tool_executions.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["approval_id"], ["assistant_action_approvals.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", "step_order", name="uq_assistant_run_steps_run_order"),
        )
        op.create_index(
            "ix_assistant_run_steps_run_order", "assistant_run_steps", ["run_id", "step_order"]
        )
        op.create_index(
            "ix_assistant_run_steps_tool_execution",
            "assistant_run_steps",
            ["tool_execution_id"],
        )
        op.create_index(
            "ix_assistant_run_steps_approval", "assistant_run_steps", ["approval_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_assistant_run_steps_approval", table_name="assistant_run_steps")
    op.drop_index("ix_assistant_run_steps_tool_execution", table_name="assistant_run_steps")
    op.drop_index("ix_assistant_run_steps_run_order", table_name="assistant_run_steps")
    op.drop_table("assistant_run_steps")
    op.drop_index("ix_assistant_runs_conversation", table_name="assistant_runs")
    op.drop_index("ix_assistant_runs_status_started", table_name="assistant_runs")
    op.drop_index("ix_assistant_runs_user_started", table_name="assistant_runs")
    op.drop_table("assistant_runs")
