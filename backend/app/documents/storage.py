"""Secure local file-storage service for uploaded documents — Story 7.1.

Files are stored under a configurable base directory (defaulting to
`backend/storage/documents/`).  Each file is given a UUID-based name so
the original filename is never exposed in the filesystem or URL.  The
directory is NOT served as a static directory by FastAPI; files can only be
read through the authenticated download endpoint.
"""

from __future__ import annotations

import os
from pathlib import Path
import uuid

_DEFAULT_STORAGE_ROOT = Path(__file__).resolve().parents[2] / "storage" / "documents"


def _storage_root() -> Path:
    """Return the configured document storage directory, creating it if absent."""
    root = Path(
        os.environ.get("DOCUMENT_STORAGE_PATH", str(_DEFAULT_STORAGE_ROOT))
    ).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def max_upload_bytes() -> int:
    """Return the maximum allowed upload size in bytes."""
    try:
        mb = int(os.environ.get("DOCUMENT_MAX_SIZE_MB", "50"))
        if mb < 1 or mb > 2048:
            raise ValueError
    except ValueError:
        mb = 50
    return mb * 1024 * 1024


def generate_stored_filename(original_filename: str) -> str:
    """Return a UUID-based filename preserving only the extension."""
    suffix = Path(original_filename).suffix.lower()
    return f"{uuid.uuid4()}{suffix}"


def write_document_file(stored_filename: str, data: bytes) -> Path:
    """Write *data* atomically under the storage root using *stored_filename*.

    Uses a temporary file + rename for atomicity, preventing a partially-written
    file from appearing as a complete upload.
    """
    root = _storage_root()
    dest = root / stored_filename
    tmp_path = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp_path.write_bytes(data)
        tmp_path.replace(dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return dest


def read_document_file(stored_filename: str) -> bytes:
    """Read and return the bytes of a stored document."""
    path = _storage_root() / stored_filename
    if not path.is_file():
        raise FileNotFoundError(f"Document file not found: {stored_filename}")
    return path.read_bytes()


def delete_document_file(stored_filename: str) -> None:
    """Delete a stored document file (best-effort; does not raise if missing)."""
    path = _storage_root() / stored_filename
    path.unlink(missing_ok=True)


def document_file_path(stored_filename: str) -> Path:
    """Return the absolute path to a stored document without reading it."""
    return _storage_root() / stored_filename
