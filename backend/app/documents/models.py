"""Document and DocumentACLEntry SQLAlchemy models — Story 7.1 / FR-37.

All document models share the single declarative `Base` defined in
`app.meeting.models` so they participate in the same metadata used by Alembic
and `create_tables()`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.meeting.models import Base

# ---------------------------------------------------------------------------
# Processing status constants
# ---------------------------------------------------------------------------

DOC_STATUS_UPLOADED = "uploaded"
DOC_STATUS_PROCESSING = "processing"
DOC_STATUS_READY = "ready"
DOC_STATUS_FAILED = "failed"
DOC_STATUSES = (
    DOC_STATUS_UPLOADED,
    DOC_STATUS_PROCESSING,
    DOC_STATUS_READY,
    DOC_STATUS_FAILED,
)

INDEX_STATUS_PENDING = "pending"
INDEX_STATUS_PROCESSING = "processing"
INDEX_STATUS_READY = "ready"
INDEX_STATUS_FAILED = "failed"
INDEX_STATUSES = (
    INDEX_STATUS_PENDING,
    INDEX_STATUS_PROCESSING,
    INDEX_STATUS_READY,
    INDEX_STATUS_FAILED,
)

# Allowed file extensions (lower-case, with leading dot).
ALLOWED_EXTENSIONS = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif"}
)

# Maximum upload size in bytes (default 50 MB; overridable via DOCUMENT_MAX_SIZE_MB).
DEFAULT_MAX_SIZE_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Document(Base):
    """One uploaded company-knowledge document (Story 7.1 / FR-37).

    The `extracted_text` field stores the full plain-text corpus ready for
    the Story 7.2 chunker.  `page_count` is set when the extractor can
    determine it (PDFs, PPTX, XLSX); otherwise None.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in DOC_STATUSES)})",
            name="ck_documents_status",
        ),
        Index("ix_documents_owner_status", "owner_id", "status"),
        Index("ix_documents_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Original name supplied by the uploader; kept for display only.
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # UUID-based name stored on disk (prevents path-traversal and enumeration).
    stored_filename: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # The employee who uploaded the document; always the authenticated user.
    owner_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=DOC_STATUS_UPLOADED,
    )
    # Human-readable failure reason (never a raw Python traceback).
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexing_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=INDEX_STATUS_PENDING
    )
    indexing_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Story 7.3: company-wide documents (handbooks, SOPs) every active employee may
    # read and ask about, without listing each person in the ACL.
    shared_with_all_employees: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # Plain-text corpus produced by the extraction pipeline.
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Number of pages/slides/sheets; None for image files.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    owner: Mapped["app.meeting.models.Employee"] = relationship(
        "Employee",
        foreign_keys=[owner_id],
        lazy="select",
    )
    acl_entries: Mapped[list["DocumentACLEntry"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class DocumentACLEntry(Base):
    """One employee granted read access to a document.

    The owner always has implicit access; this table stores additional
    employees explicitly authorized by the uploader.
    """

    __tablename__ = "document_acl_entries"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "employee_id",
            name="uq_document_acl_entries_doc_employee",
        ),
        Index("ix_document_acl_entries_employee", "employee_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    document: Mapped["Document"] = relationship(back_populates="acl_entries")
    employee: Mapped["app.meeting.models.Employee"] = relationship(
        "Employee",
        foreign_keys=[employee_id],
        lazy="select",
    )


class DocumentChunk(Base):
    """Versioned document passage and local vector embedding for semantic search."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "document_version",
            "chunk_index",
            name="uq_document_chunks_version_index",
        ),
        Index(
            "ix_document_chunks_version",
            "document_id",
            "document_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    acl_employee_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    document: Mapped[Document] = relationship(back_populates="chunks")
