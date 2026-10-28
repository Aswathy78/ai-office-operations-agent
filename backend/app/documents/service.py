"""Document upload business logic — Story 7.1 / FR-37.

Responsibilities:
- Validate file extension, MIME type (magic bytes), and size.
- Persist the Document record with status=uploaded.
- Persist ACL entries (validated against the employees table).
- Enforce that the authenticated user is always the owner.
- Provide access-control checks reused by the router.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import filetype
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.documents.models import (
    ALLOWED_EXTENSIONS,
    DOC_STATUS_UPLOADED,
    Document,
    DocumentACLEntry,
)
from app.documents.storage import (
    delete_document_file,
    generate_stored_filename,
    max_upload_bytes,
    write_document_file,
)
from app.meeting.models import Employee

logger = logging.getLogger(__name__)


class DocumentValidationError(ValueError):
    """The uploaded file or metadata did not pass validation."""


class DocumentAccessDenied(PermissionError):
    """The requesting user may not access the document."""


# MIME types we accept, mapped from the extension.
_EXTENSION_MIME_MAP: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


def _detect_mime(data: bytes) -> str | None:
    """Return the detected MIME type or None if unrecognised."""
    kind = filetype.guess(data[:4096])
    if kind is None:
        return None
    return kind.mime


def validate_upload(
    filename: str,
    data: bytes,
) -> str:
    """Validate extension, size, and magic bytes.  Return the canonical MIME type.

    Raises `DocumentValidationError` with a descriptive message on failure.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise DocumentValidationError(
            f"Unsupported file type '{suffix}'. "
            f"Allowed types: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
        )

    max_bytes = max_upload_bytes()
    if len(data) > max_bytes:
        raise DocumentValidationError(
            f"File size {len(data):,} bytes exceeds the maximum allowed "
            f"size of {max_bytes // (1024 * 1024)} MB."
        )

    if len(data) == 0:
        raise DocumentValidationError("Uploaded file is empty.")

    # Magic-byte check — must align with the declared extension.
    expected_mime = _EXTENSION_MIME_MAP[suffix]
    detected_mime = _detect_mime(data)

    # filetype cannot detect all Office XML formats — treat None as "trust extension".
    # For known binary formats (PNG, JPEG, PDF, GIF, BMP, TIFF) always verify.
    _VERIFIABLE_MIMES = {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/bmp",
        "image/tiff",
        "application/pdf",
    }
    if expected_mime in _VERIFIABLE_MIMES:
        if detected_mime != expected_mime:
            detected_label = detected_mime or "unrecognised format"
            raise DocumentValidationError(
                f"File content does not match the declared extension '{suffix}'. "
                f"Detected type: {detected_label}."
            )

    return expected_mime


@dataclass
class UploadResult:
    document: Document
    acl_warnings: list[str] = field(default_factory=list)


def create_document(
    db: Session,
    *,
    filename: str,
    data: bytes,
    owner_id: str,
    acl_employee_ids: list[str] | None = None,
) -> UploadResult:
    """Persist an uploaded document and return the created record.

    *owner_id* must be the authenticated user's employee id — never accept it
    from the client payload.

    `acl_employee_ids` is an optional list of employee ids that should be
    granted read access.  Each id is validated; invalid ids are skipped with a
    warning rather than aborting the entire upload.
    """
    mime_type = validate_upload(filename, data)

    # Verify the owner exists and is active.
    owner = db.get(Employee, owner_id)
    if owner is None or not owner.is_active:
        raise DocumentValidationError(
            "Your employee account could not be found or is not active."
        )

    stored_filename = generate_stored_filename(filename)
    write_document_file(stored_filename, data)

    doc = Document(
        id=str(uuid.uuid4()),
        original_filename=filename,
        stored_filename=stored_filename,
        file_size=len(data),
        mime_type=mime_type,
        owner_id=owner_id,
        status=DOC_STATUS_UPLOADED,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(doc)
    db.flush()  # get the id before ACL entries

    warnings: list[str] = []
    seen: set[str] = set()

    for emp_id in acl_employee_ids or []:
        if emp_id == owner_id:
            continue  # owner already has implicit access
        if emp_id in seen:
            continue
        seen.add(emp_id)

        emp = db.get(Employee, emp_id)
        if emp is None or not emp.is_active:
            warnings.append(
                f"Employee '{emp_id}' was not found or is inactive and was skipped."
            )
            continue

        entry = DocumentACLEntry(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            employee_id=emp_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(entry)

    db.commit()
    db.refresh(doc)
    return UploadResult(document=doc, acl_warnings=warnings)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def check_document_access(
    db: Session,
    document: Document,
    actor_id: str,
) -> None:
    """Raise `DocumentAccessDenied` if *actor_id* may not access *document*.

    Access is granted if:
    - The actor is the document owner, OR
    - The actor has an ACLentry on the document.
    """
    if document.owner_id == actor_id:
        return

    entry = (
        db.query(DocumentACLEntry)
        .filter(
            DocumentACLEntry.document_id == document.id,
            DocumentACLEntry.employee_id == actor_id,
        )
        .first()
    )
    if entry is None:
        raise DocumentAccessDenied(
            "You do not have permission to access this document."
        )


def list_accessible_documents(
    db: Session,
    actor_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[Document]:
    """Return all documents the actor owns or has ACL access to."""
    acl_doc_ids = select(DocumentACLEntry.document_id).where(
        DocumentACLEntry.employee_id == actor_id
    )
    return (
        db.query(Document)
        .filter(
            or_(
                Document.owner_id == actor_id,
                Document.id.in_(acl_doc_ids),
            )
        )
        .order_by(Document.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def update_document_file(
    db: Session,
    document: Document,
    *,
    filename: str,
    data: bytes,
    acl_employee_ids: list[str] | None = None,
) -> UploadResult:
    """Replace an existing document's file data, reset status, and re-process text."""
    mime_type = validate_upload(filename, data)

    # Delete previous file from disk
    delete_document_file(document.stored_filename)

    new_stored_filename = generate_stored_filename(filename)
    write_document_file(new_stored_filename, data)

    document.original_filename = filename
    document.stored_filename = new_stored_filename
    document.file_size = len(data)
    document.mime_type = mime_type
    document.status = DOC_STATUS_UPLOADED
    document.failure_reason = None
    document.extracted_text = None
    document.page_count = None
    document.processing_started_at = None
    document.processing_completed_at = None
    document.updated_at = datetime.now(timezone.utc)

    warnings: list[str] = []
    if acl_employee_ids is not None:
        db.query(DocumentACLEntry).filter(DocumentACLEntry.document_id == document.id).delete()
        seen: set[str] = set()
        for emp_id in acl_employee_ids:
            if emp_id == document.owner_id or emp_id in seen:
                continue
            seen.add(emp_id)
            emp = db.get(Employee, emp_id)
            if emp is None or not emp.is_active:
                warnings.append(f"Employee '{emp_id}' was not found or is inactive and was skipped.")
                continue
            entry = DocumentACLEntry(
                id=str(uuid.uuid4()),
                document_id=document.id,
                employee_id=emp_id,
                created_at=datetime.now(timezone.utc),
            )
            db.add(entry)

    db.commit()
    db.refresh(document)

    # Automatically re-process the updated file immediately so RAG knowledge is refreshed
    try:
        from app.documents.processor import process_one_document
        process_one_document(db, document)
    except Exception as exc:
        logger.warning("Synchronous re-processing of updated document %s failed: %s", document.id, exc)

    return UploadResult(document=document, acl_warnings=warnings)


def remove_document(db: Session, document: Document) -> None:
    """Delete a document record and its stored file from disk."""
    delete_document_file(document.stored_filename)
    db.delete(document)
    db.commit()


def reset_documents(db: Session) -> int:
    """Delete all documents and their stored files for a clean start over."""
    docs = db.query(Document).all()
    count = len(docs)
    for doc in docs:
        delete_document_file(doc.stored_filename)
        db.delete(doc)
    db.commit()
    return count

