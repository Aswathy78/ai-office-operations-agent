"""Add persistent workflow jobs and checkpoints for Epic 8.

Revision ID: 20261102_01
Revises: 20261101_02
Create Date: 2026-11-02
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261102_01"
down_revision: str | None = "20261101_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # The API's start-up creates any missing table from the models (create_all). A database
    # that was started before this migration ran therefore already has these tables, and
    # creating them again failed with "table workflow_runs already exists". Each part is
    # skipped when it is already there, so the migration is safe to run either way.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("workflow_runs"):
        _create_workflow_runs()
    if not inspector.has_table("workflow_steps"):
        _create_workflow_steps()


def _create_workflow_runs() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_type", sa.String(length=60), nullable=False),
        sa.Column("meeting_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retrying', 'completed', 'failed')",
            name="ck_workflow_runs_status",
        ),
        sa.CheckConstraint("priority >= 0", name="ck_workflow_runs_priority"),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_workflow_runs_idempotency_key"),
    )
    op.create_index(
        "ix_workflow_runs_queue",
        "workflow_runs",
        ["status", "next_attempt_at", "priority", "created_at"],
    )
    op.create_index("ix_workflow_runs_meeting", "workflow_runs", ["meeting_id", "created_at"])


def _create_workflow_steps() -> None:
    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
        sa.Column("step_key", sa.String(length=80), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'retrying')",
            name="ck_workflow_steps_status",
        ),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_run_id", "step_key", name="uq_workflow_steps_run_key"),
    )
    op.create_index(
        "ix_workflow_steps_run_order", "workflow_steps", ["workflow_run_id", "step_order"]
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_steps_run_order", table_name="workflow_steps")
    op.drop_table("workflow_steps")
    op.drop_index("ix_workflow_runs_meeting", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_queue", table_name="workflow_runs")
    op.drop_table("workflow_runs")
