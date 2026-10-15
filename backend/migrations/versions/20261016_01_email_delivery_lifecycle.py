"""E-mail delivery lifecycle: Draft, Approval, Queued, Sending, Sent, Failed, Retry (FR-22).

Revision ID: 20261016_01
Revises: 20261015_02
Create Date: 2026-10-16

Existing statuses are mapped onto the seven lifecycle statuses:
pending_approval -> approval; approved / rejected -> draft (decision kept in
approval_decision); partially_sent -> failed; discarded -> draft (discarded_at set).
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261016_01"
down_revision: str | None = "20261015_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

NEW_STATUSES = "('draft', 'approval', 'queued', 'sending', 'sent', 'failed', 'retry')"
OLD_STATUSES = (
    "('draft', 'pending_approval', 'approved', 'rejected', "
    "'sent', 'partially_sent', 'failed', 'discarded')"
)
NEW_COLUMNS = (
    sa.Column("approval_decision", sa.String(length=20), nullable=True),
    sa.Column("acknowledged_sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_error", sa.Text(), nullable=True),
    sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("discarded_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("email_drafts")}
    checks = {check["name"] for check in inspector.get_check_constraints("email_drafts")}

    with op.batch_alter_table("email_drafts", recreate="always") as batch:
        if "ck_email_drafts_status" in checks:
            batch.drop_constraint("ck_email_drafts_status", type_="check")
        for column in NEW_COLUMNS:
            if column.name not in columns:
                batch.add_column(column.copy())

    op.execute(
        "UPDATE email_drafts SET status = 'approval' WHERE status = 'pending_approval'"
    )
    op.execute(
        "UPDATE email_drafts SET approval_decision = status, status = 'draft' "
        "WHERE status IN ('approved', 'rejected')"
    )
    op.execute("UPDATE email_drafts SET status = 'failed' WHERE status = 'partially_sent'")
    op.execute(
        "UPDATE email_drafts SET status = 'draft', discarded_at = updated_at "
        "WHERE status = 'discarded'"
    )
    op.execute("UPDATE email_drafts SET status_changed_at = updated_at WHERE status_changed_at IS NULL")

    with op.batch_alter_table("email_drafts", recreate="always") as batch:
        batch.create_check_constraint("ck_email_drafts_status", f"status IN {NEW_STATUSES}")
        batch.create_index(
            "ix_email_drafts_status_next_attempt", ["status", "next_attempt_at"]
        )

    if not inspector.has_table("email_draft_events"):
        op.create_table(
            "email_draft_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("draft_id", sa.String(length=36), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("from_status", sa.String(length=30), nullable=True),
            sa.Column("to_status", sa.String(length=30), nullable=False),
            sa.Column("actor_id", sa.String(length=100), nullable=False),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["draft_id"], ["email_drafts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("draft_id", "sequence", name="uq_email_draft_events_draft_sequence"),
        )
        op.create_index(
            "ix_email_draft_events_draft_created",
            "email_draft_events",
            ["draft_id", "created_at"],
        )
    elif "sequence" not in {c["name"] for c in inspector.get_columns("email_draft_events")}:
        # Created by the API's create_tables() before `sequence` existed: number the
        # rows per e-mail in time order, then enforce uniqueness.
        with op.batch_alter_table("email_draft_events") as batch:
            batch.add_column(sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"))
        op.execute(
            "UPDATE email_draft_events SET sequence = ("
            "SELECT COUNT(*) FROM email_draft_events earlier "
            "WHERE earlier.draft_id = email_draft_events.draft_id "
            "AND (earlier.created_at < email_draft_events.created_at "
            "OR (earlier.created_at = email_draft_events.created_at "
            "AND earlier.id <= email_draft_events.id)))"
        )
        with op.batch_alter_table("email_draft_events") as batch:
            batch.create_unique_constraint(
                "uq_email_draft_events_draft_sequence", ["draft_id", "sequence"]
            )

    # Give existing e-mails a starting point in their history. Runs even when the
    # table already exists (the API's create_tables() may have created it empty).
    op.execute(
        "INSERT INTO email_draft_events "
        "(id, draft_id, sequence, from_status, to_status, actor_id, detail, created_at) "
        "SELECT lower(hex(randomblob(16))), d.id, 1, NULL, d.status, 'system', "
        "'Status recorded when delivery tracking was introduced.', d.updated_at "
        "FROM email_drafts d WHERE NOT EXISTS "
        "(SELECT 1 FROM email_draft_events e WHERE e.draft_id = d.id)"
    )


def downgrade() -> None:
    op.drop_index("ix_email_draft_events_draft_created", table_name="email_draft_events")
    op.drop_table("email_draft_events")

    with op.batch_alter_table("email_drafts", recreate="always") as batch:
        batch.drop_index("ix_email_drafts_status_next_attempt")
        batch.drop_constraint("ck_email_drafts_status", type_="check")

    op.execute("UPDATE email_drafts SET status = 'discarded' WHERE discarded_at IS NOT NULL")
    op.execute("UPDATE email_drafts SET status = 'pending_approval' WHERE status = 'approval'")
    op.execute(
        "UPDATE email_drafts SET status = approval_decision "
        "WHERE status = 'draft' AND approval_decision IN ('approved', 'rejected')"
    )
    op.execute(
        "UPDATE email_drafts SET status = 'failed' "
        "WHERE status IN ('queued', 'sending', 'retry')"
    )

    with op.batch_alter_table("email_drafts", recreate="always") as batch:
        for column in reversed(NEW_COLUMNS):
            batch.drop_column(column.name)
        batch.create_check_constraint("ck_email_drafts_status", f"status IN {OLD_STATUSES}")
