"""Text extraction and OCR pipeline for uploaded documents — Story 7.1.

Supported formats:
  PDF   — pypdf; falls back to pytesseract OCR when no usable text is detected.
  DOCX  — python-docx paragraph extraction.
  PPTX  — python-pptx slide/shape text extraction.
  XLSX  — openpyxl cell value extraction.
  Images (PNG/JPG/TIFF/BMP/GIF) — Pillow + pytesseract OCR.

Every extraction function returns an `ExtractionResult` with the plain-text
corpus and an optional page count.  All failures are caught and raised as
`ExtractionError` so the processor can record a safe, readable failure_reason.

OCR dependency note
-------------------
pytesseract requires the Tesseract binary to be installed separately:
  Windows: choco install tesseract   (or download from UB Mannheim)
  Linux:   apt install tesseract-ocr
  macOS:   brew install tesseract
Set the `TESSERACT_CMD` environment variable if Tesseract is not on PATH.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from typing import IO

logger = logging.getLogger(__name__)

# Minimum character count before we consider PDF text "usable".
_PDF_MIN_CHARS = 50


class ExtractionError(Exception):
    """A document could not be parsed or OCR-processed."""


@dataclass
class ExtractionResult:
    text: str
    page_count: int | None = None
    ocr_used: bool = False
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _configure_tesseract() -> None:
    cmd = os.environ.get("TESSERACT_CMD", "")
    if cmd:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = cmd


def _ocr_image_bytes(image_bytes: bytes, *, hint: str = "image") -> str:
    """Run OCR on raw image bytes; raises ExtractionError on failure."""
    _configure_tesseract()
    try:
        from PIL import Image
        import pytesseract
    except ImportError as exc:
        raise ExtractionError(
            "OCR libraries (Pillow / pytesseract) are not installed."
        ) from exc

    try:
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image)
        return text
    except pytesseract.pytesseract.TesseractNotFoundError:
        raise ExtractionError(
            "Tesseract is not installed or not found on PATH. "
            "Install it and set TESSERACT_CMD if needed."
        )
    except Exception as exc:
        raise ExtractionError(f"OCR failed for {hint}: {exc}") from exc


# ---------------------------------------------------------------------------
# Format-specific extractors
# ---------------------------------------------------------------------------


def extract_pdf(data: bytes) -> ExtractionResult:
    """Extract text from a PDF.  Falls back to per-page OCR for scanned PDFs."""
    try:
        import pypdf
    except ImportError as exc:
        raise ExtractionError("pypdf is not installed.") from exc

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"Could not open PDF: {exc}") from exc

    page_count = len(reader.pages)
    if page_count == 0:
        raise ExtractionError("PDF contains no pages.")

    pages_text: list[str] = []
    ocr_used = False

    for i, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception:
            raw = ""

        if len(raw.strip()) >= _PDF_MIN_CHARS:
            pages_text.append(f"[Page {i}]\n{raw.strip()}")
        else:
            # Scanned page — attempt OCR via rasterization.
            try:
                import pypdf.generic
                # Extract images embedded in the page for OCR.
                ocr_fragments: list[str] = []
                if hasattr(page, "images") and page.images:
                    for img_obj in page.images:
                        try:
                            ocr_text = _ocr_image_bytes(img_obj.data, hint=f"PDF page {i} image")
                            if ocr_text.strip():
                                ocr_fragments.append(ocr_text.strip())
                        except ExtractionError as e:
                            logger.warning("OCR failed for PDF page %d image: %s", i, e)
                if ocr_fragments:
                    ocr_used = True
                    pages_text.append(f"[Page {i} — OCR]\n" + "\n".join(ocr_fragments))
                else:
                    # Page has no embedded images either — treat as blank.
                    logger.debug("PDF page %d has no usable text or images.", i)
            except Exception as exc:
                logger.warning("Could not attempt OCR for PDF page %d: %s", i, exc)

    full_text = "\n\n".join(pages_text)
    if not full_text.strip():
        raise ExtractionError(
            "PDF contains no extractable text and OCR produced no output. "
            "The file may be empty, corrupt, or a scanned document without embedded images."
        )

    return ExtractionResult(text=full_text, page_count=page_count, ocr_used=ocr_used)


def extract_docx(data: bytes) -> ExtractionResult:
    """Extract text from a DOCX file using python-docx."""
    try:
        from docx import Document as DocxDocument
    except ImportError as exc:
        raise ExtractionError("python-docx is not installed.") from exc

    try:
        doc = DocxDocument(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"Could not open DOCX: {exc}") from exc

    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    if not paragraphs:
        raise ExtractionError("DOCX contains no readable text.")

    return ExtractionResult(text="\n".join(paragraphs))


def extract_pptx(data: bytes) -> ExtractionResult:
    """Extract text from a PPTX file preserving slide numbers."""
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise ExtractionError("python-pptx is not installed.") from exc

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"Could not open PPTX: {exc}") from exc

    slide_count = len(prs.slides)
    if slide_count == 0:
        raise ExtractionError("PPTX contains no slides.")

    slides_text: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        texts.append(line)
        if texts:
            slides_text.append(f"[Slide {i}]\n" + "\n".join(texts))

    if not slides_text:
        raise ExtractionError("PPTX contains no readable text in any slide.")

    return ExtractionResult(text="\n\n".join(slides_text), page_count=slide_count)


def extract_xlsx(data: bytes) -> ExtractionResult:
    """Extract cell values from an XLSX workbook preserving sheet names."""
    try:
        import openpyxl
    except ImportError as exc:
        raise ExtractionError("openpyxl is not installed.") from exc

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractionError(f"Could not open XLSX: {exc}") from exc

    sheet_count = len(wb.sheetnames)
    if sheet_count == 0:
        raise ExtractionError("XLSX contains no sheets.")

    sheets_text: list[str] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows: list[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None and str(c).strip()]
            if cells:
                rows.append("\t".join(cells))
        if rows:
            sheets_text.append(f"[Sheet: {sheet_name}]\n" + "\n".join(rows))

    wb.close()

    if not sheets_text:
        raise ExtractionError("XLSX contains no readable cell values.")

    return ExtractionResult(text="\n\n".join(sheets_text), page_count=sheet_count)


def extract_image(data: bytes, *, filename: str = "image") -> ExtractionResult:
    """OCR-extract text from a supported image file."""
    ocr_text = _ocr_image_bytes(data, hint=filename)
    if not ocr_text.strip():
        raise ExtractionError(
            f"OCR produced no output for image '{filename}'. "
            "The image may be blank or contain no recognisable text."
        )
    return ExtractionResult(text=ocr_text.strip(), ocr_used=True)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_MIME_EXTRACTORS = {
    "application/pdf": extract_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_docx,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": extract_pptx,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": extract_xlsx,
}

_IMAGE_MIMES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/tiff",
        "image/bmp",
        "image/gif",
    }
)


def extract_text(data: bytes, mime_type: str, *, filename: str = "") -> ExtractionResult:
    """Dispatch text extraction by *mime_type*.

    Raises `ExtractionError` for unsupported formats, empty documents, corrupt
    files, missing OCR dependencies, or OCR engine failures.
    """
    if mime_type in _MIME_EXTRACTORS:
        return _MIME_EXTRACTORS[mime_type](data)
    if mime_type in _IMAGE_MIMES:
        return extract_image(data, filename=filename or mime_type)
    raise ExtractionError(f"Unsupported document format: {mime_type}")
