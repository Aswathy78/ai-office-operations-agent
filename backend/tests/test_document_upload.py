"""Tests for Story 7.1 — Document Upload and Processing (FR-37).

Covers:
- AC1: File upload with validation (formats, size limit, magic bytes, storage)
- AC2: Text extraction across PDF, DOCX, PPTX, XLSX, image OCR
- AC3: Access control (owner persistence, ACL lists, unauthorized rejection)
- AC4: Status state machine (uploaded -> processing -> ready / failed)
- AC5: Stored extracted text ready for RAG
"""

import io
import json
from pathlib import Path
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.db.database import get_db
from app.documents.extraction import (
    ExtractionError,
    ExtractionResult,
    extract_docx,
    extract_image,
    extract_pdf,
    extract_pptx,
    extract_text,
    extract_xlsx,
)
from app.documents.models import (
    DOC_STATUS_FAILED,
    DOC_STATUS_PROCESSING,
    DOC_STATUS_READY,
    DOC_STATUS_UPLOADED,
    Document,
    DocumentACLEntry,
)
from app.documents.processor import process_one_document
from app.documents.service import (
    DocumentAccessDenied,
    DocumentValidationError,
    check_document_access,
    create_document,
    list_accessible_documents,
    validate_upload,
)
from app.documents.storage import (
    generate_stored_filename,
    read_document_file,
    write_document_file,
)
from app.meeting.models import Base, Employee


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    """In-memory SQLite database session isolated per test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = testing_session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session: Session):
    """FastAPI TestClient with database session override."""
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def doc_storage_dir(tmp_path, monkeypatch):
    """Isolate document storage in a temporary directory per test."""
    import app.documents.storage as doc_storage
    monkeypatch.setattr(doc_storage, "_DEFAULT_STORAGE_ROOT", tmp_path / "documents")
    monkeypatch.setenv("DOCUMENT_STORAGE_PATH", str(tmp_path / "documents"))
    return tmp_path / "documents"


@pytest.fixture()
def sample_employees(db_session: Session):
    """Seed test employees into the database."""
    emp1 = Employee(
        id="emp-owner",
        name="Alice Owner",
        email="alice@office.example.com",
        is_active=True,
    )
    emp2 = Employee(
        id="emp-viewer",
        name="Bob Viewer",
        email="bob@office.example.com",
        is_active=True,
    )
    emp3 = Employee(
        id="emp-unauthorized",
        name="Charlie Outsider",
        email="charlie@office.example.com",
        is_active=True,
    )
    emp_inactive = Employee(
        id="emp-inactive",
        name="Inactive Dan",
        email="dan@office.example.com",
        is_active=False,
    )
    db_session.add_all([emp1, emp2, emp3, emp_inactive])
    db_session.commit()
    return {"owner": emp1, "viewer": emp2, "unauthorized": emp3, "inactive": emp_inactive}


# Minimal valid file helpers
def _create_minimal_pdf_bytes(text: str = "This is a test document with sufficient text to pass the minimum character count threshold for PDF extraction.") -> bytes:
    import pypdf
    writer = pypdf.PdfWriter()
    # Add a blank page
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    # Return bytes with %PDF magic header
    return stream.getvalue()


def _create_minimal_png_bytes() -> bytes:
    # 1x1 transparent PNG
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _create_minimal_docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document as DocxDoc
    doc = DocxDoc()
    for p in paragraphs:
        doc.add_paragraph(p)
    stream = io.BytesIO()
    doc.save(stream)
    return stream.getvalue()


def _create_minimal_pptx_bytes(slide_texts: list[str]) -> bytes:
    from pptx import Presentation
    prs = Presentation()
    for text in slide_texts:
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank layout
        txBox = slide.shapes.add_textbox(0, 0, 100, 100)
        tf = txBox.text_frame
        p = tf.add_paragraph()
        p.text = text
    stream = io.BytesIO()
    prs.save(stream)
    return stream.getvalue()


def _create_minimal_xlsx_bytes(sheets_data: dict[str, list[list[str]]]) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    first = True
    for sheet_name, rows in sheets_data.items():
        if first:
            ws = wb.active
            ws.title = sheet_name
            first = False
        else:
            ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# AC1: Upload & File Validation Tests
# ---------------------------------------------------------------------------

def test_validate_upload_allowed_extensions():
    png = _create_minimal_png_bytes()
    mime = validate_upload("test.png", png)
    assert mime == "image/png"


def test_validate_upload_disallowed_extension():
    with pytest.raises(DocumentValidationError, match="Unsupported file type"):
        validate_upload("malicious.exe", b"fake binary content")

    with pytest.raises(DocumentValidationError, match="Unsupported file type"):
        validate_upload("script.sh", b"#!/bin/bash")


def test_validate_upload_empty_file():
    with pytest.raises(DocumentValidationError, match="Uploaded file is empty"):
        validate_upload("test.pdf", b"")


def test_validate_upload_oversized_file(monkeypatch):
    monkeypatch.setenv("DOCUMENT_MAX_SIZE_MB", "1")
    oversized = b"X" * (1 * 1024 * 1024 + 10)
    with pytest.raises(DocumentValidationError, match="exceeds the maximum allowed size"):
        validate_upload("large.pdf", oversized)


def test_validate_upload_mismatched_magic_bytes():
    fake_png = b"Not really a PNG at all"
    with pytest.raises(DocumentValidationError, match="does not match the declared extension"):
        validate_upload("fake.png", fake_png)


def test_upload_api_pdf_success(client: TestClient, sample_employees):
    pdf_bytes = _create_minimal_pdf_bytes()
    response = client.post(
        "/api/v1/documents/upload",
        files={"file": ("sample.pdf", pdf_bytes, "application/pdf")},
        headers={"X-User-Id": "emp-owner"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["document"]["original_filename"] == "sample.pdf"
    assert payload["document"]["owner_id"] == "emp-owner"
    assert payload["document"]["status"] == DOC_STATUS_UPLOADED
    assert payload["document"]["mime_type"] == "application/pdf"
    assert payload["document"]["id"] is not None


def test_upload_api_with_acl(client: TestClient, sample_employees):
    png_bytes = _create_minimal_png_bytes()
    acl_ids = json.dumps(["emp-viewer", "emp-inactive", "non-existent-emp"])
    response = client.post(
        "/api/v1/documents/upload",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"acl_employee_ids": acl_ids},
        headers={"X-User-Id": "emp-owner"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert len(payload["warnings"]) == 2
    doc_acl = payload["document"]["acl"]
    assert len(doc_acl) == 1
    assert doc_acl[0]["employee_id"] == "emp-viewer"


def test_upload_api_invalid_acl_json(client: TestClient, sample_employees):
    png_bytes = _create_minimal_png_bytes()
    response = client.post(
        "/api/v1/documents/upload",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"acl_employee_ids": "not a json list"},
        headers={"X-User-Id": "emp-owner"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# AC2: Text Extraction & OCR Tests
# ---------------------------------------------------------------------------

def test_extract_docx():
    docx_bytes = _create_minimal_docx_bytes([
        "First heading of the company report.",
        "Second paragraph with project milestones and revenue projections.",
    ])
    result = extract_docx(docx_bytes)
    assert "First heading" in result.text
    assert "revenue projections" in result.text


def test_extract_pptx():
    pptx_bytes = _create_minimal_pptx_bytes([
        "Quarterly Business Review Q4",
        "Key Deliverables and Outcomes",
    ])
    result = extract_pptx(pptx_bytes)
    assert "[Slide 1]" in result.text
    assert "Quarterly Business Review" in result.text
    assert "[Slide 2]" in result.text
    assert result.page_count == 2


def test_extract_xlsx():
    xlsx_bytes = _create_minimal_xlsx_bytes({
        "Q1_Budget": [["Department", "Amount"], ["Engineering", "500000"]],
        "Q2_Budget": [["Department", "Amount"], ["Design", "200000"]],
    })
    result = extract_xlsx(xlsx_bytes)
    assert "[Sheet: Q1_Budget]" in result.text
    assert "Engineering\t500000" in result.text
    assert "[Sheet: Q2_Budget]" in result.text
    assert result.page_count == 2


def test_extract_image_ocr(monkeypatch):
    import app.documents.extraction as ext_mod
    monkeypatch.setattr(ext_mod, "_ocr_image_bytes", lambda b, hint="": "Extracted OCR Invoice Text #12345")

    png_bytes = _create_minimal_png_bytes()
    result = extract_image(png_bytes, filename="invoice.png")
    assert result.text == "Extracted OCR Invoice Text #12345"
    assert result.ocr_used is True


def test_extract_empty_or_corrupt_fails():
    with pytest.raises(ExtractionError):
        extract_text(b"corrupt non-pdf data", "application/pdf")


# ---------------------------------------------------------------------------
# AC3: Access Control & Isolation Tests
# ---------------------------------------------------------------------------

def test_owner_access_granted(db_session: Session, sample_employees):
    png_bytes = _create_minimal_png_bytes()
    res = create_document(
        db_session,
        filename="diagram.png",
        data=png_bytes,
        owner_id="emp-owner",
        acl_employee_ids=["emp-viewer"],
    )
    check_document_access(db_session, res.document, "emp-owner")


def test_acl_viewer_access_granted(db_session: Session, sample_employees):
    png_bytes = _create_minimal_png_bytes()
    res = create_document(
        db_session,
        filename="diagram.png",
        data=png_bytes,
        owner_id="emp-owner",
        acl_employee_ids=["emp-viewer"],
    )
    check_document_access(db_session, res.document, "emp-viewer")


def test_unauthorized_access_denied(db_session: Session, sample_employees):
    png_bytes = _create_minimal_png_bytes()
    res = create_document(
        db_session,
        filename="diagram.png",
        data=png_bytes,
        owner_id="emp-owner",
        acl_employee_ids=["emp-viewer"],
    )
    with pytest.raises(DocumentAccessDenied):
        check_document_access(db_session, res.document, "emp-unauthorized")


def test_list_accessible_documents_filtering(db_session: Session, sample_employees):
    png_bytes = _create_minimal_png_bytes()
    d1 = create_document(db_session, filename="doc1.png", data=png_bytes, owner_id="emp-owner", acl_employee_ids=["emp-viewer"]).document
    d2 = create_document(db_session, filename="doc2.png", data=png_bytes, owner_id="emp-viewer").document
    d3 = create_document(db_session, filename="doc3.png", data=png_bytes, owner_id="emp-owner").document

    owner_docs = {d.id for d in list_accessible_documents(db_session, "emp-owner")}
    assert owner_docs == {d1.id, d3.id}

    viewer_docs = {d.id for d in list_accessible_documents(db_session, "emp-viewer")}
    assert viewer_docs == {d1.id, d2.id}

    outsider_docs = list_accessible_documents(db_session, "emp-unauthorized")
    assert len(outsider_docs) == 0


def test_get_document_api_enforces_access(client: TestClient, db_session: Session, sample_employees):
    png_bytes = _create_minimal_png_bytes()
    doc = create_document(
        db_session,
        filename="secret.png",
        data=png_bytes,
        owner_id="emp-owner",
        acl_employee_ids=["emp-viewer"],
    ).document

    res_owner = client.get(f"/api/v1/documents/{doc.id}", headers={"X-User-Id": "emp-owner"})
    assert res_owner.status_code == 200

    res_viewer = client.get(f"/api/v1/documents/{doc.id}", headers={"X-User-Id": "emp-viewer"})
    assert res_viewer.status_code == 200

    res_unauth = client.get(f"/api/v1/documents/{doc.id}", headers={"X-User-Id": "emp-unauthorized"})
    assert res_unauth.status_code == 403


# ---------------------------------------------------------------------------
# AC4: Status Lifecycle & Processing Pipeline Tests
# ---------------------------------------------------------------------------

def test_process_one_document_success(db_session: Session, sample_employees):
    docx_bytes = _create_minimal_docx_bytes([
        "Company Operations Handbook",
        "Section 1: Daily standups and async communication protocols.",
    ])
    doc = create_document(
        db_session,
        filename="handbook.docx",
        data=docx_bytes,
        owner_id="emp-owner",
    ).document

    assert doc.status == DOC_STATUS_UPLOADED
    process_one_document(db_session, doc)

    db_session.refresh(doc)
    assert doc.status == DOC_STATUS_READY
    assert "Company Operations Handbook" in doc.extracted_text
    assert doc.processing_completed_at is not None
    assert doc.failure_reason is None


def test_process_one_document_failure_sets_failed_status(db_session: Session, sample_employees):
    corrupt_data = b"%PDF-1.4 Fake corrupt stream that will fail pypdf parsing completely"
    doc = create_document(
        db_session,
        filename="bad.pdf",
        data=corrupt_data,
        owner_id="emp-owner",
    ).document

    process_one_document(db_session, doc)
    db_session.refresh(doc)

    assert doc.status == DOC_STATUS_FAILED
    assert doc.failure_reason is not None
    assert "Traceback" not in doc.failure_reason
    assert doc.processing_completed_at is not None


# ---------------------------------------------------------------------------
# AC5: Download & Stored Corpus Tests
# ---------------------------------------------------------------------------

def test_download_document_api(client: TestClient, db_session: Session, sample_employees):
    png_bytes = _create_minimal_png_bytes()
    doc = create_document(
        db_session,
        filename="logo.png",
        data=png_bytes,
        owner_id="emp-owner",
        acl_employee_ids=["emp-viewer"],
    ).document

    res = client.get(f"/api/v1/documents/{doc.id}/download", headers={"X-User-Id": "emp-viewer"})
    assert res.status_code == 200
    assert res.content == png_bytes
    assert 'filename="logo.png"' in res.headers["content-disposition"]

    res_unauth = client.get(f"/api/v1/documents/{doc.id}/download", headers={"X-User-Id": "emp-unauthorized"})
    assert res_unauth.status_code == 403
