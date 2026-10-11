import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sys
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import main as main_module
from main import app
from app.db.database import get_db
from app.meeting.models import (
    Base,
    Meeting,
    MeetingAttendanceLog,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    RECORDING_STATUS_FAILED,
    RECORDING_STATUS_PROCESSING,
    RECORDING_STATUS_RECORDING,
    TRANSCRIPT_STATUS_PENDING,
)
from app.meeting import service as meeting_service
from app.meeting.recording_encryption import decrypt_recording_file
from app.meeting.transcription_worker import (
    claim_pending_transcript,
    create_whisper_model,
    process_claimed_transcript,
)

ORGANIZER_ID = "recording-organizer"


def test_api_startup_loads_backend_dotenv_before_database_initialization(monkeypatch):
    events = []
    expected_env = Path(main_module.__file__).resolve().parent / ".env"

    monkeypatch.setattr(
        main_module,
        "load_dotenv",
        lambda *, dotenv_path, override: events.append(
            ("load_dotenv", Path(dotenv_path), override)
        ),
    )
    monkeypatch.setattr(
        main_module,
        "create_tables",
        lambda: events.append(("create_tables",)),
    )

    main_module.startup()

    assert events == [
        ("load_dotenv", expected_env, False),
        ("create_tables",),
    ]


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
    assert result["status"] == RECORDING_STATUS_COMPLETED
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
    assert saved_recording.status == RECORDING_STATUS_COMPLETED
    assert saved_recording.completed_at is not None
    assert saved_recording.error_message is None
    transcripts = db_session.query(MeetingTranscript).filter_by(recording_id=recording.id).all()
    assert len(transcripts) == 1
    assert transcripts[0].status == TRANSCRIPT_STATUS_PENDING
    assert transcripts[0].language == "en"


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
    assert result["status"] == RECORDING_STATUS_COMPLETED
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
    recording = _create_processing_recording(db_session, meeting.id)

    response = _upload(client, meeting.id, ORGANIZER_ID, b"")

    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()
    assert list(recording_storage.rglob("*.webm")) == []
    db_session.expire_all()
    failed_recording = db_session.get(MeetingRecording, recording.id)
    assert failed_recording.status == RECORDING_STATUS_FAILED
    assert "empty" in failed_recording.error_message.lower()


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


def test_completed_recording_rejects_repeat_upload(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    recording = _create_processing_recording(db_session, meeting.id)

    first = _upload(client, meeting.id, ORGANIZER_ID, b"first completed blob")
    second = _upload(client, meeting.id, ORGANIZER_ID, b"replacement completed blob")

    assert first.status_code == 200
    assert first.json()["status"] == RECORDING_STATUS_COMPLETED
    assert second.status_code == 409
    files = list(recording_storage.rglob("*.webm"))
    assert files == [recording_storage / first.json()["storage_uri"]]
    assert decrypt_recording_file(files[0]) == b"first completed blob"
    db_session.expire_all()
    saved_recording = db_session.get(MeetingRecording, recording.id)
    assert saved_recording.status == RECORDING_STATUS_COMPLETED
    assert db_session.query(MeetingTranscript).filter_by(recording_id=recording.id).count() == 1


def test_upload_to_transcription_happy_path(
    client: TestClient,
    db_session: Session,
    recording_storage,
    tmp_path,
    monkeypatch,
):
    meeting = _create_meeting(db_session)
    recording = _create_processing_recording(db_session, meeting.id)
    media = b"completed english meeting audio"

    response = _upload(client, meeting.id, ORGANIZER_ID, media)

    assert response.status_code == 200
    assert response.json()["status"] == RECORDING_STATUS_COMPLETED
    db_session.expire_all()
    saved_recording = db_session.get(MeetingRecording, recording.id)
    assert saved_recording.status == RECORDING_STATUS_COMPLETED
    assert saved_recording.storage_uri is not None

    transcripts = db_session.query(MeetingTranscript).filter_by(recording_id=recording.id).all()
    assert len(transcripts) == 1
    transcript_id = transcripts[0].id
    assert transcripts[0].status == TRANSCRIPT_STATUS_PENDING

    duplicate_upload = _upload(client, meeting.id, ORGANIZER_ID, media)
    assert duplicate_upload.status_code == 409
    assert db_session.query(MeetingTranscript).filter_by(recording_id=recording.id).count() == 1

    assert claim_pending_transcript(db_session) == transcript_id
    assert claim_pending_transcript(db_session) is None
    db_session.expire_all()
    assert db_session.get(MeetingTranscript, transcript_id).status == "processing"

    constructor_calls = []

    class FakeWhisperModel:
        def __init__(self, model_name, **options):
            constructor_calls.append((model_name, options))
            self.input_path = None
            self.language = None

        def transcribe(self, audio_path, *, language, word_timestamps):
            self.input_path = audio_path
            self.language = language
            assert Path(audio_path).read_bytes() == media
            return iter(
                [
                    SimpleNamespace(start=0.4, end=1.8, text=" Welcome everyone."),
                    SimpleNamespace(start=2.1, end=3.6, text=" Let's begin."),
                ]
            ), SimpleNamespace(language="en")

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    model = create_whisper_model()
    assert constructor_calls == [
        ("small", {"device": "cuda", "compute_type": "float16"})
    ]

    temporary_directory = tmp_path / "decrypted-audio"
    temporary_directory.mkdir()
    assert process_claimed_transcript(
        db_session,
        transcript_id,
        model,
        storage_root=recording_storage,
        temporary_directory=temporary_directory,
    )

    db_session.expire_all()
    completed_transcript = db_session.get(MeetingTranscript, transcript_id)
    assert completed_transcript.status == "completed"
    assert completed_transcript.language == "en"
    assert completed_transcript.transcript_text == "Welcome everyone. Let's begin."
    assert completed_transcript.segments == [
        {"start": 0.4, "end": 1.8, "text": "Welcome everyone.", "speaker": "Unknown Speaker", "flagged_for_correction": True},
        {"start": 2.1, "end": 3.6, "text": "Let's begin.", "speaker": "Unknown Speaker", "flagged_for_correction": True},
    ]
    assert model.language == "en"
    assert model.input_path is not None
    assert not Path(model.input_path).exists()
    assert list(temporary_directory.iterdir()) == []


def test_encryption_failure_removes_plaintext_temporary_file(
    client: TestClient,
    db_session: Session,
    recording_storage,
    monkeypatch,
):
    meeting = _create_meeting(db_session)
    recording = _create_processing_recording(db_session, meeting.id)

    def fail_encryption(_source, _destination):
        raise OSError("forced encryption failure")

    monkeypatch.setattr(meeting_service, "encrypt_recording_file", fail_encryption)
    response = _upload(client, meeting.id, ORGANIZER_ID, b"sensitive plaintext")

    assert response.status_code == 500
    assert list(recording_storage.rglob("*.plaintext.tmp")) == []
    assert list(recording_storage.rglob("*.encrypted.tmp")) == []
    assert list(recording_storage.rglob("*.webm")) == []
    db_session.expire_all()
    failed_recording = db_session.get(MeetingRecording, recording.id)
    assert failed_recording.status == RECORDING_STATUS_FAILED
    assert "forced encryption failure" in failed_recording.error_message


def test_missing_encryption_key_rejects_upload_and_cleans_temporary_files(
    client: TestClient,
    db_session: Session,
    recording_storage,
    monkeypatch,
):
    meeting = _create_meeting(db_session)
    recording = _create_processing_recording(db_session, meeting.id)
    monkeypatch.delenv("RECORDING_ENCRYPTION_KEY", raising=False)

    response = _upload(client, meeting.id, ORGANIZER_ID, b"sensitive plaintext")

    assert response.status_code == 503
    assert "RECORDING_ENCRYPTION_KEY" in response.json()["detail"]
    assert list(recording_storage.rglob("*.plaintext.tmp")) == []
    assert list(recording_storage.rglob("*.encrypted.tmp")) == []
    assert list(recording_storage.rglob("*.webm")) == []
    db_session.expire_all()
    failed_recording = db_session.get(MeetingRecording, recording.id)
    assert failed_recording.status == RECORDING_STATUS_FAILED
    assert "RECORDING_ENCRYPTION_KEY" in failed_recording.error_message


def test_filesystem_storage_failure_persists_failed_state(
    client: TestClient,
    db_session: Session,
    recording_storage,
    monkeypatch,
):
    meeting = _create_meeting(db_session)
    recording = _create_processing_recording(db_session, meeting.id)

    def fail_replace(_source, _destination):
        raise OSError("forced storage failure")

    monkeypatch.setattr(meeting_service.os, "replace", fail_replace)
    response = _upload(client, meeting.id, ORGANIZER_ID, b"recording")

    assert response.status_code == 500
    assert list(recording_storage.rglob("*.webm")) == []
    db_session.expire_all()
    failed_recording = db_session.get(MeetingRecording, recording.id)
    assert failed_recording.status == RECORDING_STATUS_FAILED
    assert "forced storage failure" in failed_recording.error_message


def test_metadata_persistence_failure_persists_failed_state(
    client: TestClient,
    db_session: Session,
    recording_storage,
    monkeypatch,
):
    meeting = _create_meeting(db_session)
    recording = _create_processing_recording(db_session, meeting.id)
    commit = db_session.commit
    should_fail = True

    def fail_first_commit():
        nonlocal should_fail
        if should_fail:
            should_fail = False
            raise RuntimeError("forced metadata persistence failure")
        commit()

    monkeypatch.setattr(db_session, "commit", fail_first_commit)
    response = _upload(client, meeting.id, ORGANIZER_ID, b"recording")

    assert response.status_code == 500
    assert "Unable to persist recording metadata" in response.json()["detail"]
    assert list(recording_storage.rglob("*.webm")) == []
    db_session.expire_all()
    failed_recording = db_session.get(MeetingRecording, recording.id)
    assert failed_recording.status == RECORDING_STATUS_FAILED
    assert "Unable to persist recording metadata" in failed_recording.error_message


def test_upload_rejects_unsafe_filename_and_non_audio_content_type(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    unsafe_recording = _create_processing_recording(db_session, meeting.id)

    unsafe_filename = client.post(
        f"/api/v1/meetings/{meeting.id}/recording/upload?organizer_id={ORGANIZER_ID}",
        files={"file": ("..\\outside.webm", b"media", "audio/webm")},
    )

    db_session.expire_all()
    failed_unsafe_recording = db_session.get(MeetingRecording, unsafe_recording.id)
    assert failed_unsafe_recording.status == RECORDING_STATUS_FAILED

    invalid_type_meeting = _create_meeting(db_session)
    invalid_type_recording = _create_processing_recording(db_session, invalid_type_meeting.id)
    invalid_type = client.post(
        f"/api/v1/meetings/{invalid_type_meeting.id}/recording/upload?organizer_id={ORGANIZER_ID}",
        files={"file": ("recording.txt", b"not audio", "text/plain")},
    )

    assert unsafe_filename.status_code == 400
    assert invalid_type.status_code == 415
    db_session.expire_all()
    failed_invalid_type_recording = db_session.get(MeetingRecording, invalid_type_recording.id)
    assert failed_invalid_type_recording.status == RECORDING_STATUS_FAILED
    assert list(recording_storage.rglob("*.*")) == []
