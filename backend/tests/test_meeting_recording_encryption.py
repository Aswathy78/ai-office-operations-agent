import base64
import secrets

import pytest

from app.meeting.recording_encryption import (
    MAGIC,
    NONCE_SIZE,
    HEADER_SIZE,
    TAG_SIZE,
    FORMAT_VERSION,
    RecordingDecryptionError,
    RecordingEncryptionConfigurationError,
    decrypt_recording_file,
    encrypt_recording_file,
)


@pytest.fixture()
def test_key(monkeypatch):
    key = secrets.token_bytes(32)
    monkeypatch.setenv("RECORDING_ENCRYPTION_KEY", base64.b64encode(key).decode("ascii"))
    return key


def test_encrypts_and_decrypts_using_versioned_authenticated_format(tmp_path, test_key):
    del test_key
    plaintext = b"known recording content"
    source_path = tmp_path / "plaintext.tmp"
    encrypted_path = tmp_path / "recording.aorc"
    source_path.write_bytes(plaintext)

    nonce = encrypt_recording_file(source_path, encrypted_path)
    encrypted = encrypted_path.read_bytes()

    assert len(nonce) == NONCE_SIZE
    assert encrypted[:len(MAGIC)] == MAGIC
    assert encrypted[len(MAGIC)] == FORMAT_VERSION
    assert encrypted[len(MAGIC) + 1:HEADER_SIZE] == nonce
    assert len(encrypted) >= HEADER_SIZE + TAG_SIZE
    assert encrypted != plaintext
    assert decrypt_recording_file(encrypted_path) == plaintext


def test_each_encryption_operation_uses_a_fresh_nonce(tmp_path, test_key):
    del test_key
    source_path = tmp_path / "plaintext.tmp"
    source_path.write_bytes(b"same recording bytes")
    first_path = tmp_path / "first.aorc"
    second_path = tmp_path / "second.aorc"

    first_nonce = encrypt_recording_file(source_path, first_path)
    second_nonce = encrypt_recording_file(source_path, second_path)

    assert first_nonce != second_nonce
    assert first_path.read_bytes()[len(MAGIC) + 1:HEADER_SIZE] == first_nonce
    assert second_path.read_bytes()[len(MAGIC) + 1:HEADER_SIZE] == second_nonce


def test_tampered_ciphertext_fails_authentication(tmp_path, test_key):
    del test_key
    source_path = tmp_path / "plaintext.tmp"
    encrypted_path = tmp_path / "recording.aorc"
    source_path.write_bytes(b"authenticated recording")
    encrypt_recording_file(source_path, encrypted_path)
    encrypted = bytearray(encrypted_path.read_bytes())
    encrypted[HEADER_SIZE] ^= 0x01
    encrypted_path.write_bytes(encrypted)

    with pytest.raises(RecordingDecryptionError, match="authentication failed"):
        decrypt_recording_file(encrypted_path)


def test_missing_key_has_clear_configuration_error(tmp_path, monkeypatch):
    monkeypatch.delenv("RECORDING_ENCRYPTION_KEY", raising=False)
    source_path = tmp_path / "plaintext.tmp"
    encrypted_path = tmp_path / "recording.aorc"
    source_path.write_bytes(b"recording")

    with pytest.raises(RecordingEncryptionConfigurationError, match="must be configured"):
        encrypt_recording_file(source_path, encrypted_path)


def test_invalid_base64_key_has_clear_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDING_ENCRYPTION_KEY", "not-base64!")
    source_path = tmp_path / "plaintext.tmp"
    encrypted_path = tmp_path / "recording.aorc"
    source_path.write_bytes(b"recording")

    with pytest.raises(RecordingEncryptionConfigurationError, match="valid base64"):
        encrypt_recording_file(source_path, encrypted_path)


def test_wrong_key_fails_authentication(tmp_path, monkeypatch, test_key):
    del test_key
    source_path = tmp_path / "plaintext.tmp"
    encrypted_path = tmp_path / "recording.aorc"
    source_path.write_bytes(b"recording encrypted with key one")
    encrypt_recording_file(source_path, encrypted_path)
    monkeypatch.setenv(
        "RECORDING_ENCRYPTION_KEY",
        base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
    )

    with pytest.raises(RecordingDecryptionError, match="authentication failed"):
        decrypt_recording_file(encrypted_path)


def test_decoded_key_must_be_exactly_32_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RECORDING_ENCRYPTION_KEY",
        base64.b64encode(secrets.token_bytes(31)).decode("ascii"),
    )
    source_path = tmp_path / "plaintext.tmp"
    encrypted_path = tmp_path / "recording.aorc"
    source_path.write_bytes(b"recording")

    with pytest.raises(RecordingEncryptionConfigurationError, match="exactly 32 bytes"):
        encrypt_recording_file(source_path, encrypted_path)


def test_truncated_and_unknown_version_files_are_rejected(tmp_path, monkeypatch, test_key):
    del test_key
    path = tmp_path / "recording.aorc"
    path.write_bytes(b"AORC")
    with pytest.raises(RecordingDecryptionError, match="truncated or malformed"):
        decrypt_recording_file(path)

    path.write_bytes(MAGIC + bytes((FORMAT_VERSION + 1,)) + b"\0" * (NONCE_SIZE + TAG_SIZE))
    with pytest.raises(RecordingDecryptionError, match="unsupported format version"):
        decrypt_recording_file(path)
