"""Page / slide / sheet label on document chunks (Story 7.5).

Adds ``document_chunks.page_label`` so an answer can cite the page, slide or
sheet a passage comes from, next to its section heading.

Chunks indexed before this revision have no label. Documents that are already
indexed are queued for the normal re-index so they get one; until the worker
rebuilds them their old chunks keep serving searches (the rebuild swaps the
chunks in one transaction).

Revision ID: 20261031_01
Revises: 20261029_02
Create Date: 2026-10-31
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261031_01"
down_revision: str | None = "20261029_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("document_chunks")}
    if "page_label" not in columns:
        op.add_column(
            "document_chunks",
            sa.Column("page_label", sa.String(length=120), nullable=True),
        )

    bind.execute(
        sa.text(
            "UPDATE documents SET indexing_status = 'pending', "
            "indexing_failure_reason = NULL "
            "WHERE status = 'ready' AND indexing_status = 'ready' "
            "AND extracted_text IS NOT NULL AND TRIM(extracted_text) <> ''"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("document_chunks") as batch:
        batch.drop_column("page_label")
