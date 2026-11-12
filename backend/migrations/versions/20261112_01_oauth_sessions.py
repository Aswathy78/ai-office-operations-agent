"""OAuth sign-in, login sessions, rotating refresh tokens and an auth audit trail (Story 10.1).

Revision ID: 20261112_01
Revises: 20261109_02
Create Date: 2026-11-12
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261112_01"
down_revision: str | None = "20261109_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # The API creates missing tables from the models at start-up, so every step checks first.
    # The auth tables are created by the API at start-up, not by an earlier migration, so on a
    # database the API has not started on yet there is nothing to alter: create_all adds the
    # column along with the table.
    if inspector.has_table("auth_sessions") and "login_session_id" not in {
        c["name"] for c in inspector.get_columns("auth_sessions")
    }:
        with op.batch_alter_table("auth_sessions") as batch:
            batch.add_column(sa.Column("login_session_id", sa.String(length=36), nullable=True))

    if not inspector.has_table("auth_login_sessions"):
        op.create_table(
            "auth_login_sessions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=20), nullable=False),
            sa.Column("ip_address", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=300), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_by", sa.String(length=36), nullable=True),
            sa.Column("revoked_reason", sa.String(length=40), nullable=True),
            sa.ForeignKeyConstraint(["account_id"], ["auth_accounts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_auth_login_sessions_account", "auth_login_sessions", ["account_id", "revoked_at"])
        op.create_index("ix_auth_login_sessions_active", "auth_login_sessions", ["revoked_at", "last_active_at"])

    if not inspector.has_table("auth_refresh_tokens"):
        op.create_table(
            "auth_refresh_tokens",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["session_id"], ["auth_login_sessions.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_auth_refresh_tokens_hash"),
        )
        op.create_index("ix_auth_refresh_tokens_session", "auth_refresh_tokens", ["session_id"])

    if not inspector.has_table("auth_identities"):
        op.create_table(
            "auth_identities",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=20), nullable=False),
            sa.Column("subject", sa.String(length=255), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["account_id"], ["auth_accounts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "subject", name="uq_auth_identities_provider_subject"),
            sa.UniqueConstraint("account_id", "provider", name="uq_auth_identities_account_provider"),
        )

    if not inspector.has_table("oauth_login_states"):
        op.create_table(
            "oauth_login_states",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("state_hash", sa.String(length=64), nullable=False),
            sa.Column("provider", sa.String(length=20), nullable=False),
            sa.Column("nonce", sa.String(length=64), nullable=False),
            sa.Column("code_verifier", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("exchange_hash", sa.String(length=64), nullable=True),
            sa.Column("exchange_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("exchanged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("session_id", sa.String(length=36), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("state_hash", name="uq_oauth_login_states_state"),
        )
        op.create_index("ix_oauth_login_states_exchange", "oauth_login_states", ["exchange_hash"])

    if not inspector.has_table("auth_audit_events"):
        op.create_table(
            "auth_audit_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("event", sa.String(length=40), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("actor_id", sa.String(length=36), nullable=True),
            sa.Column("provider", sa.String(length=20), nullable=True),
            sa.Column("session_id", sa.String(length=36), nullable=True),
            sa.Column("ip_address", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=300), nullable=True),
            sa.Column("detail", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_auth_audit_events_created", "auth_audit_events", ["created_at"])


def downgrade() -> None:
    for table in (
        "auth_audit_events",
        "oauth_login_states",
        "auth_identities",
        "auth_refresh_tokens",
        "auth_login_sessions",
    ):
        op.drop_table(table)
    if sa.inspect(op.get_bind()).has_table("auth_sessions"):
        with op.batch_alter_table("auth_sessions") as batch:
            batch.drop_column("login_session_id")
