import base64
from datetime import datetime, timedelta, timezone
import secrets
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.db.database import get_db
from app.meeting.models import (
    Base,
    Meeting,
    MeetingAttendanceLog,
    MeetingRecording,
    RECORDING_STATUS_PROCESSING,
    RECORDING_STATUS_RECORDING,
)
from app.meeting import service as meeting_service
from app.meeting.recording_encryption import decrypt_recording_file

ORGANIZER_ID = "recording-organizer"


@pytest.fixture()
def db_session():
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
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def recording_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting_service, "RECORDING_STORAGE_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def recording_encryption_key(monkeypatch):
    test_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    monkeypatch.setenv("RECORDING_ENCRYPTION_KEY", test_key)


def _create_meeting(db_session: Session) -> Meeting:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id=ORGANIZER_ID,
        title="Recording Upload Test",
        start_at=now - timedelta(minutes=5),
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="active",
    )
    db_session.add(meeting)
    db_session.commit()
    return meeting


def _create_processing_recording(db_session: Session, meeting_id: str) -> MeetingRecording:
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        status=RECORDING_STATUS_PROCESSING,
    )
    db_session.add(recording)
    db_session.commit()
    return recording


def _upload(client: TestClient, meeting_id: str, organizer_id: str, content: bytes):
    return client.post(
        f"/api/v1/meetings/{meeting_id}/recording/upload?organizer_id={organizer_id}",
        files={"file": ("recording.webm", content, "audio/webm;codecs=opus")},
    )


def test_successful_upload_persists_file_uri_and_metadata(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    recording = _create_processing_recording(db_session, meeting.id)
    media = b"completed webm recording"

    response = _upload(client, meeting.id, ORGANIZER_ID, media)

    assert response.status_code == 200
    result = response.json()
    assert result["recording_id"] == recording.id
    assert result["status"] == RECORDING_STATUS_PROCESSING
    assert result["filename"] == f"{recording.id}.webm"
    assert result["content_type"] == "audio/webm"
    assert result["size_bytes"] == len(media)
    stored_file = recording_storage / result["storage_uri"]
    assert stored_file.is_file()
    assert stored_file.read_bytes() != media
    assert decrypt_recording_file(stored_file) == media

    db_session.expire_all()
    saved_recording = db_session.get(MeetingRecording, recording.id)
    assert saved_recording.storage_uri == result["storage_uri"]


def test_successful_audio_video_webm_upload(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    recording = _create_processing_recording(db_session, meeting.id)
    media = b"completed audio and video webm recording"

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/recording/upload?organizer_id={ORGANIZER_ID}",
        files={"file": ("recording.webm", media, "video/webm;codecs=vp8,opus")},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["recording_id"] == recording.id
    assert result["content_type"] == "video/webm"
    assert result["filename"] == f"{recording.id}.webm"
    stored_file = recording_storage / result["storage_uri"]
    assert stored_file.is_file()
    assert stored_file.read_bytes() != media
    assert decrypt_recording_file(stored_file) == media


def test_empty_upload_is_rejected(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    _create_processing_recording(db_session, meeting.id)

    response = _upload(client, meeting.id, ORGANIZER_ID, b"")

    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()
    assert list(recording_storage.rglob("*.webm")) == []


def test_upload_without_processing_recording_is_rejected(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)

    response = _upload(client, meeting.id, ORGANIZER_ID, b"media")

    assert response.status_code == 409
    assert list(recording_storage.rglob("*.webm")) == []


def test_upload_requires_organizer_authorization(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    _create_processing_recording(db_session, meeting.id)

    response = _upload(client, meeting.id, "not-the-organizer", b"media")

    assert response.status_code == 403
    assert list(recording_storage.rglob("*.webm")) == []


def test_upload_rejects_recording_not_in_processing_state(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=RECORDING_STATUS_RECORDING,
    )
    db_session.add(recording)
    db_session.commit()

    response = _upload(client, meeting.id, ORGANIZER_ID, b"media")

    assert response.status_code == 409
    assert list(recording_storage.rglob("*.webm")) == []


def test_repeat_upload_replaces_the_deterministic_file(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    recording = _create_processing_recording(db_session, meeting.id)

    first = _upload(client, meeting.id, ORGANIZER_ID, b"first completed blob")
    second = _upload(client, meeting.id, ORGANIZER_ID, b"replacement completed blob")

    assert first.status_code == second.status_code == 200
    assert first.json()["storage_uri"] == second.json()["storage_uri"]
    files = list(recording_storage.rglob("*.webm"))
    assert files == [recording_storage / second.json()["storage_uri"]]
    assert files[0].read_bytes() != b"replacement completed blob"
    assert decrypt_recording_file(files[0]) == b"replacement completed blob"


def test_encryption_failure_removes_plaintext_temporary_file(
    client: TestClient,
    db_session: Session,
    recording_storage,
    monkeypatch,
):
    meeting = _create_meeting(db_session)
    _create_processing_recording(db_session, meeting.id)

    def fail_encryption(_source, _destination):
        raise OSError("forced encryption failure")

    monkeypatch.setattr(meeting_service, "encrypt_recording_file", fail_encryption)
    response = _upload(client, meeting.id, ORGANIZER_ID, b"sensitive plaintext")

    assert response.status_code == 500
    assert list(recording_storage.rglob("*.plaintext.tmp")) == []
    assert list(recording_storage.rglob("*.encrypted.tmp")) == []
    assert list(recording_storage.rglob("*.webm")) == []


def test_missing_encryption_key_rejects_upload_and_cleans_temporary_files(
    client: TestClient,
    db_session: Session,
    recording_storage,
    monkeypatch,
):
    meeting = _create_meeting(db_session)
    _create_processing_recording(db_session, meeting.id)
    monkeypatch.delenv("RECORDING_ENCRYPTION_KEY", raising=False)

    response = _upload(client, meeting.id, ORGANIZER_ID, b"sensitive plaintext")

    assert response.status_code == 503
    assert "RECORDING_ENCRYPTION_KEY" in response.json()["detail"]
    assert list(recording_storage.rglob("*.plaintext.tmp")) == []
    assert list(recording_storage.rglob("*.encrypted.tmp")) == []
    assert list(recording_storage.rglob("*.webm")) == []


def test_upload_rejects_unsafe_filename_and_non_audio_content_type(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    _create_processing_recording(db_session, meeting.id)

    unsafe_filename = client.post(
        f"/api/v1/meetings/{meeting.id}/recording/upload?organizer_id={ORGANIZER_ID}",
        files={"file": ("..\\outside.webm", b"media", "audio/webm")},
    )
    invalid_type = client.post(
        f"/api/v1/meetings/{meeting.id}/recording/upload?organizer_id={ORGANIZER_ID}",
        files={"file": ("recording.txt", b"not audio", "text/plain")},
    )

    assert unsafe_filename.status_code == 400
    assert invalid_type.status_code == 415
    assert list(recording_storage.rglob("*.*")) == []
