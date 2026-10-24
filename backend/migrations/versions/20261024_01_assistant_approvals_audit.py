"""Assistant action approvals and audit events (Story 6.6).

* ``assistant_action_approvals``: medium/high-risk assistant writes paused with
  "Approval Required" until the requester (medium) or an approver (high) decides.
* ``assistant_audit_events``: every permission, conflict, risk and approval
  decision and outcome for assistant tool calls.

Revision ID: 20261024_01
Revises: 20261022_02
Create Date: 2026-10-24
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261024_01"
down_revision: str | None = "20261022_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("assistant_action_approvals"):
        op.create_table(
            "assistant_action_approvals",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("tool_name", sa.String(length=80), nullable=False),
            sa.Column("requester_id", sa.String(length=36), nullable=False),
            sa.Column("requester_role", sa.String(length=30), nullable=False),
            sa.Column("requester_team", sa.String(length=100), nullable=True),
            sa.Column("conversation_id", sa.String(length=36), nullable=True),
            sa.Column("idempotency_key", sa.String(length=200), nullable=False),
            sa.Column("arguments", sa.JSON(), nullable=False),
            sa.Column("arguments_hash", sa.String(length=64), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("risk_level", sa.String(length=10), nullable=False),
            sa.Column("risk_reasons", sa.JSON(), nullable=False),
            sa.Column("approver_kind", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("decided_by", sa.String(length=36), nullable=True),
            sa.Column("decided_by_role", sa.String(length=30), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decision_note", sa.Text(), nullable=True),
            sa.Column("execution_id", sa.String(length=36), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'rejected', 'cancelled', 'expired', 'executed', 'failed')",
                name="ck_assistant_action_approvals_status",
            ),
            sa.CheckConstraint(
                "risk_level IN ('low', 'medium', 'high')",
                name="ck_assistant_action_approvals_risk_level",
            ),
            sa.CheckConstraint(
                "approver_kind IN ('requester', 'approver')",
                name="ck_assistant_action_approvals_approver_kind",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "requester_id", "idempotency_key",
                name="uq_assistant_action_approvals_requester_key",
            ),
        )
        op.create_index(
            "ix_assistant_action_approvals_status_created",
            "assistant_action_approvals",
            ["status", "created_at"],
        )

    if not inspector.has_table("assistant_audit_events"):
        op.create_table(
            "assistant_audit_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("actor_id", sa.String(length=36), nullable=False),
            sa.Column("actor_role", sa.String(length=30), nullable=False),
            sa.Column("tool_name", sa.String(length=80), nullable=False),
            sa.Column("event", sa.String(length=40), nullable=False),
            sa.Column("risk_level", sa.String(length=10), nullable=True),
            sa.Column("approval_id", sa.String(length=36), nullable=True),
            sa.Column("execution_id", sa.String(length=36), nullable=True),
            sa.Column("conversation_id", sa.String(length=36), nullable=True),
            sa.Column("detail", sa.Text(), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_assistant_audit_events_actor_created",
            "assistant_audit_events",
            ["actor_id", "created_at"],
        )
        op.create_index(
            "ix_assistant_audit_events_approval", "assistant_audit_events", ["approval_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_assistant_audit_events_approval", table_name="assistant_audit_events")
    op.drop_index("ix_assistant_audit_events_actor_created", table_name="assistant_audit_events")
    op.drop_table("assistant_audit_events")
    op.drop_index(
        "ix_assistant_action_approvals_status_created",
        table_name="assistant_action_approvals",
    )
    op.drop_table("assistant_action_approvals")
