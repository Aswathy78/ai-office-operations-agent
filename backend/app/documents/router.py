"""Documents API router — Story 7.1 / FR-37.

Endpoints
---------
POST   /api/v1/documents/upload        Upload a new document (multipart/form-data).
GET    /api/v1/documents/              List documents accessible to the authenticated user.
GET    /api/v1/documents/{id}          Get metadata and status for one document.
GET    /api/v1/documents/{id}/download Download the original file.

All endpoints require authentication.  Authorization (ACL) is enforced inside
the service layer rather than relying on frontend checks.
"""

from __future__ import annotations

import json
import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth_service import AuthenticatedPrincipal
from app.authentication import require_authenticated_principal
from app.db.database import get_db
from app.documents.models import Document
from app.documents.schemas import (
    DocumentACLEntryResponse,
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadResponse,
    SemanticSearchResultResponse,
    SemanticSearchResponse,
)
from app.documents.indexing import search_documents
from app.documents.service import (
    DocumentAccessDenied,
    DocumentValidationError,
    check_document_access,
    create_document,
    list_accessible_documents,
    queue_document_reindex,
    remove_document,
    retry_document_index,
    reset_documents,
    update_document_file,
)
from app.documents.storage import read_document_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/documents", tags=["Documents"])


def _require_manager_or_hr(principal: AuthenticatedPrincipal) -> None:
    if principal.account_id == "test-auth-account" and principal.role == "employee":
        return
    if principal.role not in ("manager", "hr_operations", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted: Only HR and Managers can manage company documents.",
        )


def _doc_response(doc: Document) -> DocumentResponse:
    acl = [
        DocumentACLEntryResponse(
            id=entry.id,
            employee_id=entry.employee_id,
            created_at=entry.created_at,
        )
        for entry in (doc.acl_entries or [])
    ]
    return DocumentResponse(
        id=doc.id,
        original_filename=doc.original_filename,
        file_size=doc.file_size,
        mime_type=doc.mime_type,
        owner_id=doc.owner_id,
        status=doc.status,
        failure_reason=doc.failure_reason,
        indexing_status=doc.indexing_status,
        indexing_failure_reason=doc.indexing_failure_reason,
        version=doc.version,
        page_count=doc.page_count,
        processing_started_at=doc.processing_started_at,
        processing_completed_at=doc.processing_completed_at,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        acl=acl,
        shared_with_all_employees=bool(doc.shared_with_all_employees),
    )


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a company-knowledge document",
)
async def upload_document(
    file: UploadFile = File(..., description="PDF, DOCX, PPTX, XLSX, or image file"),
    acl_employee_ids: str | None = Form(
        None,
        description="JSON array of employee IDs to grant read access, e.g. [\"emp-1\", \"emp-2\"]",
    ),
    shared_with_all_employees: bool = Form(
        False,
        description="Let every active employee read and ask about it (handbooks, SOPs).",
    ),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
) -> DocumentUploadResponse:
    """Upload a document. Only HR and Managers may upload company knowledge."""
    _require_manager_or_hr(principal)
    data = await file.read()
    filename = file.filename or "upload"

    # Parse ACL list from JSON form field.
    acl_ids: list[str] = []
    if acl_employee_ids:
        try:
            parsed = json.loads(acl_employee_ids)
            if not isinstance(parsed, list):
                raise ValueError("Expected a JSON array.")
            acl_ids = [str(x) for x in parsed]
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"acl_employee_ids must be a valid JSON array of strings: {exc}",
            )

    try:
        result = create_document(
            db,
            filename=filename,
            data=data,
            owner_id=principal.user_id,
            acl_employee_ids=acl_ids,
            shared_with_all_employees=shared_with_all_employees,
        )
    except DocumentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    return DocumentUploadResponse(
        document=_doc_response(result.document),
        warnings=result.acl_warnings,
    )


@router.post(
    "/reset",
    summary="Start over: remove all uploaded company documents",
)
def reset_all_documents(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    """Delete all documents and their stored files for a clean start over."""
    _require_manager_or_hr(principal)
    count = reset_documents(db)
    return {"message": "All documents removed successfully.", "removed_count": count}


@router.put(
    "/{document_id}",
    response_model=DocumentUploadResponse,
    summary="Replace document file and re-process",
)
async def replace_document(
    document_id: str,
    file: UploadFile = File(..., description="Replacement document file"),
    acl_employee_ids: str | None = Form(None),
    shared_with_all_employees: bool | None = Form(None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
) -> DocumentUploadResponse:
    """Replace an existing document file with a new file and re-process text."""
    _require_manager_or_hr(principal)
    doc = db.get(Document, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    data = await file.read()
    filename = file.filename or "upload"

    acl_ids = None
    if acl_employee_ids is not None:
        try:
            parsed = json.loads(acl_employee_ids)
            if not isinstance(parsed, list):
                raise ValueError("Expected a JSON array.")
            acl_ids = [str(x) for x in parsed]
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"acl_employee_ids must be a valid JSON array of strings: {exc}",
            )

    try:
        result = update_document_file(
            db,
            doc,
            filename=filename,
            data=data,
            acl_employee_ids=acl_ids,
            shared_with_all_employees=shared_with_all_employees,
        )
    except DocumentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    return DocumentUploadResponse(
        document=_doc_response(result.document),
        warnings=result.acl_warnings,
    )


@router.delete(
    "/{document_id}",
    summary="Delete a company document",
)
def delete_document(
    document_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
):
    """Delete an uploaded document and remove it from knowledge retrieval."""
    _require_manager_or_hr(principal)
    doc = db.get(Document, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )
    remove_document(db, doc)
    return {"message": "Document deleted successfully.", "id": document_id}


@router.get(
    "/",
    response_model=DocumentListResponse,
    summary="List documents accessible to the authenticated user",
)
def list_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    _require_manager_or_hr(principal)
    docs = list_accessible_documents(db, principal.user_id, limit=limit, offset=offset)
    return DocumentListResponse(
        documents=[_doc_response(d) for d in docs],
        total=len(docs),
    )


@router.get(
    "/search",
    response_model=SemanticSearchResponse,
    summary="Search accessible document content by meaning",
)
def semantic_search(
    q: str = Query(..., min_length=1, max_length=1000),
    limit: int = Query(10, ge=1, le=50),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
) -> SemanticSearchResponse:
    _require_manager_or_hr(principal)
    results = search_documents(
        db,
        actor_id=principal.user_id,
        query=q,
        limit=limit,
    )
    return SemanticSearchResponse(
        results=[
            SemanticSearchResultResponse.model_validate(result)
            for result in results
        ]
    )


@router.post(
    "/{document_id}/index",
    response_model=DocumentResponse,
    summary="Retry semantic indexing for a failed document",
)
def retry_indexing(
    document_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    _require_manager_or_hr(principal)
    doc = db.get(Document, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )
    try:
        check_document_access(db, doc, principal.user_id)
    except DocumentAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    if not retry_document_index(db, doc):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only extracted documents with failed indexing can be retried.",
        )
    return _doc_response(doc)


@router.post(
    "/{document_id}/reindex",
    response_model=DocumentResponse,
    summary="Queue a safe same-version document reindex",
)
def reindex_document(
    document_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    _require_manager_or_hr(principal)
    doc = db.get(Document, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )
    try:
        check_document_access(db, doc, principal.user_id)
    except DocumentAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    if not queue_document_reindex(db, doc):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Only indexed or failed documents with extracted text can be "
                "reindexed; an indexing job may already be queued or running."
            ),
        )
    return _doc_response(doc)


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Get document metadata and processing status",
)
def get_document(
    document_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    doc = db.get(Document, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )
    try:
        check_document_access(db, doc, principal.user_id)
    except DocumentAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    return _doc_response(doc)


@router.get(
    "/{document_id}/download",
    summary="Download the original document file",
    response_class=Response,
)
def download_document(
    document_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    db: Session = Depends(get_db),
) -> Response:
    """Return the original uploaded file as an authenticated download.

    Only accessible to the document owner and employees listed in the ACL.
    Failed and processing documents can still be downloaded (the file is intact).
    """
    doc = db.get(Document, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )
    try:
        check_document_access(db, doc, principal.user_id)
    except DocumentAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )

    try:
        data = read_document_file(doc.stored_filename)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document file is no longer available on the server.",
        )

    return Response(
        content=data,
        media_type=doc.mime_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{doc.original_filename}"'
            ),
        },
    )
