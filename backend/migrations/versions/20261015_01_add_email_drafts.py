"""Add e-mail drafts created from chat (Story 4.3).

Revision ID: 20261015_01
Revises: 20261014_03
Create Date: 2026-10-15
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261015_01"
down_revision: str | None = "20261014_03"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("email_drafts"):
        return

    op.create_table(
        "email_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_by_id", sa.String(length=100), nullable=False),
        sa.Column("created_by_name", sa.String(length=255), nullable=False),
        sa.Column("created_by_email", sa.String(length=255), nullable=True),
        sa.Column("created_by_role", sa.String(length=30), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("template_key", sa.String(length=50), nullable=True),
        sa.Column("generation_note", sa.Text(), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("recipient_groups", sa.JSON(), nullable=False),
        sa.Column("unresolved_recipients", sa.JSON(), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("approval_reasons", sa.JSON(), nullable=False),
        sa.Column("sensitive_findings", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="draft"),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("delivery_results", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'rejected', "
            "'sent', 'partially_sent', 'failed', 'discarded')",
            name="ck_email_drafts_status",
        ),
        sa.CheckConstraint(
            "source IN ('template', 'llm')",
            name="ck_email_drafts_source",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_drafts_created_by_id", "email_drafts", ["created_by_id"])
    op.create_index("ix_email_drafts_status", "email_drafts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_email_drafts_status", table_name="email_drafts")
    op.drop_index("ix_email_drafts_created_by_id", table_name="email_drafts")
    op.drop_table("email_drafts")
