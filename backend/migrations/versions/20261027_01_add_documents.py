"""Add documents and document_acl_entries tables — Story 7.1 / FR-37.

* `documents`: uploaded company-knowledge documents with processing status
  and extracted text corpus ready for Story 7.2 RAG chunking.
* `document_acl_entries`: per-document employee access-control list.

Revision ID: 20261027_01
Revises: 20261024_01
Create Date: 2026-10-27
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261027_01"
down_revision: str | None = "20261024_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("documents"):
        op.create_table(
            "documents",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("original_filename", sa.String(length=512), nullable=False),
            sa.Column("stored_filename", sa.String(length=512), nullable=False),
            sa.Column("file_size", sa.BigInteger(), nullable=False),
            sa.Column("mime_type", sa.String(length=128), nullable=False),
            sa.Column("owner_id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("extracted_text", sa.Text(), nullable=True),
            sa.Column("page_count", sa.Integer(), nullable=True),
            sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("processing_completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('uploaded', 'processing', 'ready', 'failed')",
                name="ck_documents_status",
            ),
            sa.ForeignKeyConstraint(
                ["owner_id"],
                ["employees.id"],
                ondelete="RESTRICT",
                name="fk_documents_owner_id",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("stored_filename", name="uq_documents_stored_filename"),
        )
        op.create_index("ix_documents_owner_status", "documents", ["owner_id", "status"])
        op.create_index("ix_documents_status_created", "documents", ["status", "created_at"])

    if not inspector.has_table("document_acl_entries"):
        op.create_table(
            "document_acl_entries",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("employee_id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["document_id"],
                ["documents.id"],
                ondelete="CASCADE",
                name="fk_document_acl_entries_document_id",
            ),
            sa.ForeignKeyConstraint(
                ["employee_id"],
                ["employees.id"],
                ondelete="CASCADE",
                name="fk_document_acl_entries_employee_id",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "document_id",
                "employee_id",
                name="uq_document_acl_entries_doc_employee",
            ),
        )
        op.create_index(
            "ix_document_acl_entries_employee",
            "document_acl_entries",
            ["employee_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_document_acl_entries_employee", table_name="document_acl_entries")
    op.drop_table("document_acl_entries")
    op.drop_index("ix_documents_status_created", table_name="documents")
    op.drop_index("ix_documents_owner_status", table_name="documents")
    op.drop_table("documents")
