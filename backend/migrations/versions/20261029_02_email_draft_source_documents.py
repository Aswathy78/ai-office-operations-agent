"""Company documents quoted by an e-mail draft (Story 7.4).

Adds ``email_drafts.source_document_ids``: the documents whose facts an e-mail
quotes, so the sender's access is re-checked when it is sent or retried.

Revision ID: 20261029_02
Revises: 20261029_01
Create Date: 2026-10-29
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261029_02"
down_revision: str | None = "20261029_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("email_drafts")}
    if "source_document_ids" not in columns:
        op.add_column(
            "email_drafts",
            sa.Column(
                "source_document_ids",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("email_drafts") as batch:
        batch.drop_column("source_document_ids")
