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
import traceback
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.documents.extraction import ExtractionError, extract_text
from app.documents.models import (
    DOC_STATUS_FAILED,
    DOC_STATUS_PROCESSING,
    DOC_STATUS_READY,
    DOC_STATUS_UPLOADED,
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
    """Process a single document synchronously (also used in tests)."""
    # Transition to processing inside the same session.
    doc.status = DOC_STATUS_PROCESSING
    doc.processing_started_at = datetime.now(timezone.utc)
    doc.updated_at = datetime.now(timezone.utc)
    db.commit()

    try:
        data = read_document_file(doc.stored_filename)
        result = extract_text(data, doc.mime_type, filename=doc.original_filename)

        doc.status = DOC_STATUS_READY
        doc.extracted_text = result.text
        doc.page_count = result.page_count
        doc.processing_completed_at = datetime.now(timezone.utc)
        doc.updated_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "Document %s processed: %d chars, %s pages, ocr=%s",
            doc.id,
            len(result.text),
            result.page_count,
            result.ocr_used,
        )

    except (ExtractionError, FileNotFoundError) as exc:
        reason = _safe_failure_reason(exc)
        logger.warning("Document %s failed: %s", doc.id, reason)
        doc.status = DOC_STATUS_FAILED
        doc.failure_reason = reason
        doc.processing_completed_at = datetime.now(timezone.utc)
        doc.updated_at = datetime.now(timezone.utc)
        db.commit()

    except Exception as exc:
        # Unexpected errors: log full traceback but store only a safe reason.
        logger.exception("Unexpected error processing document %s", doc.id)
        reason = f"Internal processing error: {type(exc).__name__}"
        doc.status = DOC_STATUS_FAILED
        doc.failure_reason = reason
        doc.processing_completed_at = datetime.now(timezone.utc)
        doc.updated_at = datetime.now(timezone.utc)
        try:
            db.commit()
        except Exception:
            db.rollback()


def _worker_loop() -> None:
    logger.info("Document processor started.")
    while not _stop_event.is_set():
        try:
            with SessionLocal() as db:
                # Pick one document at a time to keep transactions short.
                doc = (
                    db.query(Document)
                    .filter(Document.status == DOC_STATUS_UPLOADED)
                    .with_for_update(skip_locked=True)
                    .first()
                )
                if doc is not None:
                    process_one_document(db, doc)
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
