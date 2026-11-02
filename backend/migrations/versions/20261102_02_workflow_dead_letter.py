"""Dead-letter queue and execution history for workflow runs (Story 8.4).

Adds ``workflow_dead_letters`` (runs that ran out of retries, with the details a
human needs and how an administrator resolved them) and ``workflow_events`` (the
execution history of each run), and lets a run be ``cancelled``.

Runs that had already failed before this revision get an open dead-letter entry,
so no failed run is left outside the queue.

Revision ID: 20261102_02
Revises: 20261102_01
Create Date: 2026-11-02
"""

from datetime import datetime, timezone
from typing import Sequence
import uuid

from alembic import op
import sqlalchemy as sa

revision: str = "20261102_02"
down_revision: str | None = "20261102_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

OLD_STATUSES = "('pending', 'processing', 'retrying', 'completed', 'failed')"
NEW_STATUSES = "('pending', 'processing', 'retrying', 'completed', 'failed', 'cancelled')"
CHECK_NAME = "ck_workflow_runs_status"


def _workflow_runs_table(statuses: str) -> sa.Table:
    """workflow_runs as created by 20261102_01, with the given status check.

    Passed to the batch rebuild so Alembic does not have to reflect the table. It
    would otherwise fail on a database where ``meetings`` does not exist yet (the
    app creates that table, not this migration chain).
    """
    metadata = sa.MetaData()
    sa.Table("meetings", metadata, sa.Column("id", sa.String(length=36), primary_key=True))
    return sa.Table(
        "workflow_runs",
        metadata,
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
        sa.CheckConstraint(f"status IN {statuses}", name=CHECK_NAME),
        sa.CheckConstraint("priority >= 0", name="ck_workflow_runs_priority"),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_workflow_runs_idempotency_key"),
        sa.Index("ix_workflow_runs_queue", "status", "next_attempt_at", "priority", "created_at"),
        sa.Index("ix_workflow_runs_meeting", "meeting_id", "created_at"),
    )


def _swap_run_status_check(statuses: str) -> None:
    with op.batch_alter_table(
        "workflow_runs", copy_from=_workflow_runs_table(statuses), recreate="always"
    ):
        pass  # the rebuild itself applies the new check constraint


def upgrade() -> None:
    bind = op.get_bind()
    _swap_run_status_check(NEW_STATUSES)

    inspector = sa.inspect(bind)
    if not inspector.has_table("workflow_dead_letters"):
        op.create_table(
            "workflow_dead_letters",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
            sa.Column("workflow_type", sa.String(length=60), nullable=False),
            sa.Column("meeting_id", sa.String(length=36), nullable=True),
            sa.Column("failed_step_key", sa.String(length=80), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("max_attempts", sa.Integer(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("notified_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_by", sa.String(length=36), nullable=True),
            sa.Column("resolution_note", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "status IN ('open', 'retried', 'resumed', 'cancelled')",
                name="ck_workflow_dead_letters_status",
            ),
            sa.ForeignKeyConstraint(
                ["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_workflow_dead_letters_status", "workflow_dead_letters", ["status", "created_at"]
        )
        op.create_index(
            "ix_workflow_dead_letters_run",
            "workflow_dead_letters",
            ["workflow_run_id", "created_at"],
        )
        op.create_index(
            "uq_workflow_dead_letters_open_run",
            "workflow_dead_letters",
            ["workflow_run_id"],
            unique=True,
            sqlite_where=sa.text("status = 'open'"),
            postgresql_where=sa.text("status = 'open'"),
        )

    if not inspector.has_table("workflow_events"):
        op.create_table(
            "workflow_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("step_key", sa.String(length=80), nullable=True),
            sa.Column("attempt_number", sa.Integer(), nullable=True),
            sa.Column("event_type", sa.String(length=40), nullable=False),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("retry_delay_seconds", sa.Float(), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("actor_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_workflow_events_run", "workflow_events", ["workflow_run_id", "sequence"]
        )

    # Runs that failed before the queue existed still need a human's attention.
    now = datetime.now(timezone.utc)
    failed_runs = bind.execute(
        sa.text(
            "SELECT r.id, r.workflow_type, r.meeting_id, r.last_error, r.attempt_count, "
            "r.max_attempts, r.payload, ("
            "SELECT s.step_key FROM workflow_steps s WHERE s.workflow_run_id = r.id "
            "AND s.status = 'failed' ORDER BY s.step_order LIMIT 1) "
            "FROM workflow_runs r WHERE r.status = 'failed' AND NOT EXISTS ("
            "SELECT 1 FROM workflow_dead_letters d WHERE d.workflow_run_id = r.id)"
        )
    ).fetchall()
    if failed_runs:
        dead_letters = sa.table(
            "workflow_dead_letters",
            sa.column("id"),
            sa.column("workflow_run_id"),
            sa.column("workflow_type"),
            sa.column("meeting_id"),
            sa.column("failed_step_key"),
            sa.column("error_message"),
            sa.column("attempt_count"),
            sa.column("max_attempts"),
            sa.column("payload", sa.JSON()),
            sa.column("status"),
            sa.column("notified_count"),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        import json

        op.bulk_insert(
            dead_letters,
            [
                {
                    "id": str(uuid.uuid4()),
                    "workflow_run_id": row[0],
                    "workflow_type": row[1],
                    "meeting_id": row[2],
                    "failed_step_key": row[7],
                    "error_message": row[3],
                    "attempt_count": row[4],
                    "max_attempts": row[5],
                    "payload": json.loads(row[6]) if isinstance(row[6], str) else (row[6] or {}),
                    "status": "open",
                    "notified_count": 0,
                    "created_at": now,
                }
                for row in failed_runs
            ],
        )


def downgrade() -> None:
    op.drop_table("workflow_events")
    op.drop_table("workflow_dead_letters")
    op.execute("UPDATE workflow_runs SET status = 'failed' WHERE status = 'cancelled'")
    _swap_run_status_check(OLD_STATUSES)
