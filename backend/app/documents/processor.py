"""Background document-processing worker — Story 7.1.

Architecture
------------
A single daemon thread picks up documents with status `uploaded`, transitions
them to `processing`, runs the extraction pipeline, and sets the final status
to `ready` or `failed`.

The worker is disabled when `DOCUMENT_PROCESSOR_ENABLED=false` (set
automatically by tests via conftest).

Idempotency
-----------
The status is checked inside a transaction before setting `processing`, so a
document that was already picked up by another worker instance (or a retry) is
skipped.  Failed documents remain in `failed` status; re-processing requires
an explicit reset (not implemented in Story 7.1).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.documents.extraction import ExtractionError, extract_text
from app.documents.indexing import index_document
from app.documents.models import (
    DOC_STATUS_FAILED,
    DOC_STATUS_PROCESSING,
    DOC_STATUS_READY,
    DOC_STATUS_UPLOADED,
    INDEX_STATUS_FAILED,
    INDEX_STATUS_PENDING,
    Document,
)
from app.documents.storage import read_document_file

logger = logging.getLogger(__name__)

_POLL_INTERVAL = int(os.environ.get("DOCUMENT_PROCESSOR_INTERVAL_SECONDS", "5"))
_MAX_SAFE_REASON_LENGTH = 400

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _safe_failure_reason(exc: Exception) -> str:
    """Return a short, non-sensitive failure reason from an exception."""
    reason = str(exc)
    if len(reason) > _MAX_SAFE_REASON_LENGTH:
        reason = reason[:_MAX_SAFE_REASON_LENGTH] + "…"
    return reason


def process_one_document(db: Session, doc: Document) -> None:
    """Claim, extract, and enqueue vector indexing for one document."""
    now = datetime.now(timezone.utc)
    claim = db.execute(
        update(Document)
        .where(
            Document.id == doc.id,
            Document.status == DOC_STATUS_UPLOADED,
        )
        .values(
            status=DOC_STATUS_PROCESSING,
            processing_started_at=now,
            updated_at=now,
        )
    )
    if claim.rowcount != 1:
        db.rollback()
        return
    db.commit()
    db.refresh(doc)

    try:
        data = read_document_file(doc.stored_filename)
        result = extract_text(data, doc.mime_type, filename=doc.original_filename)
        completed_at = datetime.now(timezone.utc)
        db.execute(
            update(Document)
            .where(
                Document.id == doc.id,
                Document.status == DOC_STATUS_PROCESSING,
            )
            .values(
                status=DOC_STATUS_READY,
                extracted_text=result.text,
                page_count=result.page_count,
                failure_reason=None,
                indexing_status=INDEX_STATUS_PENDING,
                indexing_failure_reason=None,
                processing_completed_at=completed_at,
                updated_at=completed_at,
            )
        )
        db.commit()
        db.refresh(doc)
        logger.info(
            "Document %s processed: %d chars, %s pages, ocr=%s",
            doc.id,
            len(result.text),
            result.page_count,
            result.ocr_used,
        )
        index_document(db, doc.id)

    except (ExtractionError, FileNotFoundError) as exc:
        reason = _safe_failure_reason(exc)
        logger.warning("Document %s failed: %s", doc.id, reason)
        failed_at = datetime.now(timezone.utc)
        db.execute(
            update(Document)
            .where(
                Document.id == doc.id,
                Document.status == DOC_STATUS_PROCESSING,
            )
            .values(
                status=DOC_STATUS_FAILED,
                failure_reason=reason,
                indexing_status=INDEX_STATUS_FAILED,
                indexing_failure_reason="Text extraction failed; indexing is unavailable.",
                processing_completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        db.commit()

    except Exception as exc:
        # Unexpected errors: log full traceback but store only a safe reason.
        logger.exception("Unexpected error processing document %s", doc.id)
        reason = f"Internal processing error: {type(exc).__name__}"
        try:
            failed_at = datetime.now(timezone.utc)
            db.execute(
                update(Document)
                .where(
                    Document.id == doc.id,
                    Document.status == DOC_STATUS_PROCESSING,
                )
                .values(
                    status=DOC_STATUS_FAILED,
                    failure_reason=reason,
                    indexing_status=INDEX_STATUS_FAILED,
                    indexing_failure_reason="Text extraction failed; indexing is unavailable.",
                    processing_completed_at=failed_at,
                    updated_at=failed_at,
                )
            )
            db.commit()
        except Exception:
            db.rollback()


def _process_pending_index_documents(db: Session) -> None:
    """Attempt every queued index job once, in creation order."""
    pending_document_ids = (
        db.query(Document.id)
        .filter(
            Document.status == DOC_STATUS_READY,
            Document.indexing_status == INDEX_STATUS_PENDING,
        )
        .order_by(Document.created_at, Document.id)
        .all()
    )
    for (document_id,) in pending_document_ids:
        try:
            index_document(db, document_id)
        except Exception:
            db.rollback()
            logger.exception(
                "Document worker failed to index queued document %s; continuing.",
                document_id,
            )


def _worker_loop() -> None:
    logger.info("Document processor started.")
    while not _stop_event.is_set():
        try:
            with SessionLocal() as db:
                uploaded_doc = (
                    db.query(Document)
                    .filter(Document.status == DOC_STATUS_UPLOADED)
                    .order_by(Document.created_at, Document.id)
                    .first()
                )
                if uploaded_doc is not None:
                    process_one_document(db, uploaded_doc)
                _process_pending_index_documents(db)
        except Exception:
            logger.exception("Document worker encountered an error during polling.")

        _stop_event.wait(timeout=_POLL_INTERVAL)
    logger.info("Document processor stopped.")


def start_document_processor() -> bool:
    """Start the background processing thread.  Returns True if started."""
    if os.environ.get("DOCUMENT_PROCESSOR_ENABLED", "true").strip().lower() == "false":
        logger.info("Document processor disabled via DOCUMENT_PROCESSOR_ENABLED=false.")
        return False

    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return True

    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name="document-processor",
        daemon=True,
    )
    _worker_thread.start()
    return True


def stop_document_processor() -> None:
    """Signal the background thread to stop and wait for it."""
    _stop_event.set()
    global _worker_thread
    if _worker_thread is not None:
        _worker_thread.join(timeout=10)
        _worker_thread = None
