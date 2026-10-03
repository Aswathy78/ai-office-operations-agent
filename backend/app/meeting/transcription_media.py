"""Temporary plaintext access for the meeting transcription worker."""

import os
from pathlib import Path
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.meeting.recording_encryption import (
    FORMAT_VERSION,
    HEADER_SIZE,
    MAGIC,
    NONCE_SIZE,
    TAG_SIZE,
    RecordingDecryptionError,
    _get_encryption_key,
)

CHUNK_SIZE = 1024 * 1024


def decrypt_recording_to_temporary_file(
    encrypted_path: str | Path,
    *,
    temporary_directory: str | Path | None = None,
) -> Path:
    """Stream an authenticated recording into a private temporary file.

    The returned plaintext path belongs to the caller and must be unlinked
    after the transcription library has finished using it.
    """
    source_path = Path(encrypted_path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="meeting-stt-",
        suffix=source_path.suffix,
        dir=temporary_directory,
    )
    plaintext_path = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "wb") as destination:
            with source_path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                encrypted_size = source.tell()
                if encrypted_size < HEADER_SIZE + TAG_SIZE:
                    raise RecordingDecryptionError(
                        "Encrypted recording is truncated or malformed."
                    )

                source.seek(0)
                header = source.read(HEADER_SIZE)
                if len(header) != HEADER_SIZE or header[:len(MAGIC)] != MAGIC:
                    raise RecordingDecryptionError(
                        "Encrypted recording has an unrecognized file header."
                    )
                if header[len(MAGIC)] != FORMAT_VERSION:
                    raise RecordingDecryptionError(
                        "Encrypted recording uses an unsupported format version."
                    )

                nonce_start = len(MAGIC) + 1
                nonce = header[nonce_start:nonce_start + NONCE_SIZE]
                source.seek(-TAG_SIZE, os.SEEK_END)
                tag = source.read(TAG_SIZE)
                ciphertext_size = encrypted_size - HEADER_SIZE - TAG_SIZE
                source.seek(HEADER_SIZE)

                decryptor = Cipher(
                    algorithms.AES(_get_encryption_key()),
                    modes.GCM(nonce, tag),
                ).decryptor()
                decryptor.authenticate_additional_data(header)

                remaining = ciphertext_size
                while remaining:
                    chunk = source.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise RecordingDecryptionError(
                            "Encrypted recording is truncated or malformed."
                        )
                    destination.write(decryptor.update(chunk))
                    remaining -= len(chunk)

                try:
                    destination.write(decryptor.finalize())
                except InvalidTag:
                    raise RecordingDecryptionError(
                        "Encrypted recording authentication failed; the file or key may be invalid."
                    ) from None

                destination.flush()
                os.fsync(destination.fileno())
        return plaintext_path
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        plaintext_path.unlink(missing_ok=True)
        raise