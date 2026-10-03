"""AES-256-GCM file encryption for persisted meeting recordings.

Format version 1 is: ``AORC`` magic (4 bytes), version (1 byte),
12-byte nonce, ciphertext, then the 16-byte GCM authentication tag. The
magic/version/nonce header is authenticated as additional authenticated data.
"""

import base64
import binascii
import os
from pathlib import Path
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY_ENVIRONMENT_VARIABLE = "RECORDING_ENCRYPTION_KEY"
MAGIC = b"AORC"
FORMAT_VERSION = 1
NONCE_SIZE = 12
TAG_SIZE = 16
HEADER_SIZE = len(MAGIC) + 1 + NONCE_SIZE
CHUNK_SIZE = 1024 * 1024


class RecordingEncryptionConfigurationError(RuntimeError):
    """Raised when the recording encryption key is missing or invalid."""


class RecordingDecryptionError(ValueError):
    """Raised when an encrypted recording is malformed or fails authentication."""


def _get_encryption_key() -> bytes:
    encoded_key = os.environ.get(KEY_ENVIRONMENT_VARIABLE)
    if not encoded_key:
        raise RecordingEncryptionConfigurationError(
            f"{KEY_ENVIRONMENT_VARIABLE} must be configured to use encrypted recording storage."
        )

    try:
        key = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError):
        raise RecordingEncryptionConfigurationError(
            f"{KEY_ENVIRONMENT_VARIABLE} must be valid base64 encoding of a 32-byte key."
        ) from None

    if len(key) != 32:
        raise RecordingEncryptionConfigurationError(
            f"{KEY_ENVIRONMENT_VARIABLE} must decode to exactly 32 bytes for AES-256-GCM."
        )
    return key


def encrypt_recording_file(source_path: str | Path, encrypted_path: str | Path) -> bytes:
    """Encrypt a plaintext temporary file to a separate destination file.

    Returns the nonce used. The caller is responsible for atomically promoting
    the encrypted destination after encryption succeeds.
    """
    key = _get_encryption_key()
    nonce = secrets.token_bytes(NONCE_SIZE)
    header = MAGIC + bytes((FORMAT_VERSION,)) + nonce
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)

    with Path(source_path).open("rb") as source, Path(encrypted_path).open("wb") as destination:
        destination.write(header)
        while chunk := source.read(CHUNK_SIZE):
            destination.write(encryptor.update(chunk))
        destination.write(encryptor.finalize())
        destination.write(encryptor.tag)
        destination.flush()
        os.fsync(destination.fileno())
    return nonce


def decrypt_recording_file(encrypted_path: str | Path) -> bytes:
    """Decrypt an encrypted recording into memory for trusted internal use."""
    encrypted = Path(encrypted_path).read_bytes()
    if len(encrypted) < HEADER_SIZE + TAG_SIZE:
        raise RecordingDecryptionError("Encrypted recording is truncated or malformed.")

    header = encrypted[:HEADER_SIZE]
    if header[:len(MAGIC)] != MAGIC:
        raise RecordingDecryptionError("Encrypted recording has an unrecognized file header.")
    version = header[len(MAGIC)]
    if version != FORMAT_VERSION:
        raise RecordingDecryptionError("Encrypted recording uses an unsupported format version.")

    key = _get_encryption_key()
    nonce_start = len(MAGIC) + 1
    nonce = header[nonce_start:HEADER_SIZE]
    tag = encrypted[-TAG_SIZE:]
    ciphertext = encrypted[HEADER_SIZE:-TAG_SIZE]

    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    decryptor.authenticate_additional_data(header)
    try:
        return decryptor.update(ciphertext) + decryptor.finalize()
    except InvalidTag:
        raise RecordingDecryptionError(
            "Encrypted recording authentication failed; the file or key may be invalid."
        ) from None
