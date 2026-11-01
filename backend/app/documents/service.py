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
from sqlalchemy import and_, inspect, or_, select, update
from sqlalchemy.orm import Session

from app.documents.models import (
    ALLOWED_EXTENSIONS,
    DOC_STATUS_READY,
    DOC_STATUS_UPLOADED,
    INDEX_STATUS_FAILED,
    INDEX_STATUS_PENDING,
    INDEX_STATUS_READY,
    DocumentChunk,
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
    shared_with_all_employees: bool = False,
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
        shared_with_all_employees=shared_with_all_employees,
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
    has_access = (
        db.query(Document.id)
        .filter(
            Document.id == document.id,
            _document_access_filter(actor_id),
        )
        .first()
    )
    if has_access is None:
        raise DocumentAccessDenied(
            "You do not have permission to access this document."
        )


def _document_access_filter(actor_id: str):
    """Shared SQL rule for every document read, search and answer.

    Access needs an existing, active employee (Story 7.4: an unknown or disabled
    identity matches nothing) who is the owner, is on the document's ACL, or the
    document is shared with all employees (Story 7.3).
    """
    acl_document_ids = select(DocumentACLEntry.document_id).where(
        DocumentACLEntry.employee_id == actor_id
    )
    actor_is_active = (
        select(Employee.id)
        .where(Employee.id == actor_id, Employee.is_active.is_(True))
        .exists()
    )
    return and_(
        actor_is_active,
        or_(
            Document.owner_id == actor_id,
            Document.id.in_(acl_document_ids),
            Document.shared_with_all_employees.is_(True),
        ),
    )


def accessible_document_versions(
    db: Session,
    actor_id: str,
    document_ids: list[str] | set[str],
) -> dict[str, int]:
    """Current version of each listed document the actor may read right now (Story 7.4).

    Used to re-check retrieved or cached context at answer and send time, so a
    revoked grant, a disabled account or a deleted document is noticed. Any
    error is treated as "no access" (fail closed).
    """
    ids = {document_id for document_id in document_ids if document_id}
    if not ids or not actor_id:
        return {}
    try:
        rows = (
            db.query(Document.id, Document.version)
            .filter(Document.id.in_(ids), _document_access_filter(actor_id))
            .all()
        )
    except Exception:  # noqa: BLE001 - cannot verify: deny
        logger.exception("Document access re-check failed; denying access.")
        return {}
    return {document_id: version for document_id, version in rows}


def list_accessible_documents(
    db: Session,
    actor_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[Document]:
    """Return all documents the actor owns or has ACL access to."""
    return (
        db.query(Document)
        .filter(_document_access_filter(actor_id))
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
    shared_with_all_employees: bool | None = None,
) -> UploadResult:
    """Replace a document and let the background worker re-extract and index it."""
    mime_type = validate_upload(filename, data)

    previous_filename = document.stored_filename
    new_stored_filename = generate_stored_filename(filename)
    write_document_file(new_stored_filename, data)

    warnings: list[str] = []
    try:
        now = datetime.now(timezone.utc)
        # Retire prior chunks in the same transaction as the version change.
        db.query(DocumentChunk).filter(
            DocumentChunk.document_id == document.id,
            DocumentChunk.soft_deleted_at.is_(None),
        ).update(
            {DocumentChunk.soft_deleted_at: now},
            synchronize_session=False,
        )
        document.original_filename = filename
        document.stored_filename = new_stored_filename
        document.file_size = len(data)
        document.mime_type = mime_type
        document.status = DOC_STATUS_UPLOADED
        document.failure_reason = None
        document.indexing_status = INDEX_STATUS_PENDING
        document.indexing_failure_reason = None
        document.extracted_text = None
        document.page_count = None
        document.version += 1
        if shared_with_all_employees is not None:
            document.shared_with_all_employees = shared_with_all_employees
        document.processing_started_at = None
        document.processing_completed_at = None
        document.updated_at = now

        if acl_employee_ids is not None:
            warnings.extend(_replace_acl_entries(db, document, acl_employee_ids))
        db.commit()
        db.refresh(document)
    except Exception:
        db.rollback()
        delete_document_file(new_stored_filename)
        raise

    delete_document_file(previous_filename)

    return UploadResult(document=document, acl_warnings=warnings)


def _replace_acl_entries(
    db: Session, document: Document, acl_employee_ids: list[str]
) -> list[str]:
    """Replace the document's access list; the owner is implicit. Does not commit."""
    warnings: list[str] = []
    db.query(DocumentACLEntry).filter(
        DocumentACLEntry.document_id == document.id
    ).delete()
    seen: set[str] = set()
    for emp_id in acl_employee_ids:
        if emp_id == document.owner_id or emp_id in seen:
            continue
        seen.add(emp_id)
        emp = db.get(Employee, emp_id)
        if emp is None or not emp.is_active:
            warnings.append(
                f"Employee '{emp_id}' was not found or is inactive and was skipped."
            )
            continue
        db.add(
            DocumentACLEntry(
                id=str(uuid.uuid4()),
                document_id=document.id,
                employee_id=emp_id,
                created_at=datetime.now(timezone.utc),
            )
        )
    return warnings


def update_document_access(
    db: Session,
    document: Document,
    *,
    acl_employee_ids: list[str] | None = None,
    shared_with_all_employees: bool | None = None,
) -> UploadResult:
    """Change who can read a document without replacing its file.

    The file, version and index stay as they are. The access list stored on the
    document's search chunks is rewritten in place, so retrieval follows the new
    list immediately and nothing has to be re-embedded.
    """
    warnings: list[str] = []
    try:
        if acl_employee_ids is not None:
            warnings.extend(_replace_acl_entries(db, document, acl_employee_ids))
            db.flush()
            allowed = {document.owner_id}
            allowed.update(
                row[0]
                for row in db.query(DocumentACLEntry.employee_id)
                .filter(DocumentACLEntry.document_id == document.id)
                .all()
            )
            db.query(DocumentChunk).filter(
                DocumentChunk.document_id == document.id
            ).update(
                {DocumentChunk.acl_employee_ids: sorted(allowed)},
                synchronize_session=False,
            )
        if shared_with_all_employees is not None:
            document.shared_with_all_employees = shared_with_all_employees
        document.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(document)
    except Exception:
        db.rollback()
        raise
    return UploadResult(document=document, acl_warnings=warnings)


def retry_document_index(db: Session, document: Document) -> bool:
    """Queue a failed semantic index again without changing extracted source text."""
    if (
        document.status != DOC_STATUS_READY
        or document.indexing_status != INDEX_STATUS_FAILED
        or not (
            document.extracted_text and document.extracted_text.strip()
        )
    ):
        return False
    document.indexing_status = INDEX_STATUS_PENDING
    document.indexing_failure_reason = None
    document.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(document)
    return True


def queue_document_reindex(db: Session, document: Document) -> bool:
    """Queue an already extracted document for a safe same-version rebuild."""
    if (
        document.status != DOC_STATUS_READY
        or not (document.extracted_text and document.extracted_text.strip())
        or document.indexing_status not in (INDEX_STATUS_READY, INDEX_STATUS_FAILED)
    ):
        return False

    now = datetime.now(timezone.utc)
    result = db.execute(
        update(Document)
        .where(
            Document.id == document.id,
            Document.version == document.version,
            Document.status == DOC_STATUS_READY,
            Document.indexing_status.in_((INDEX_STATUS_READY, INDEX_STATUS_FAILED)),
        )
        .values(
            indexing_status=INDEX_STATUS_PENDING,
            indexing_failure_reason=None,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    db.refresh(document)
    return True


def remove_document(db: Session, document: Document) -> None:
    """Delete a document and all indexed versions; safe to retry after commit."""
    state = inspect(document)
    identity = state.identity
    document_id = identity[0] if identity else state.dict.get("id")
    if document_id is None:
        raise ValueError("Cannot delete a document without a database identity.")

    current_document = db.get(Document, document_id)
    stored_filename = (
        current_document.stored_filename if current_document is not None else None
    )
    db.query(DocumentChunk).filter(
        DocumentChunk.document_id == document_id
    ).delete(synchronize_session=False)
    if current_document is not None:
        db.delete(current_document)
    db.commit()
    if stored_filename is not None:
        try:
            delete_document_file(stored_filename)
        except OSError:
            logger.exception(
                "Failed to remove stored file for deleted document %s.",
                document_id,
            )


def reset_documents(db: Session) -> int:
    """Delete all documents and their stored files for a clean start over."""
    docs = db.query(Document).all()
    count = len(docs)
    for doc in docs:
        delete_document_file(doc.stored_filename)
        db.query(DocumentChunk).filter(
            DocumentChunk.document_id == doc.id
        ).delete(synchronize_session=False)
        db.delete(doc)
    db.commit()
    return count
