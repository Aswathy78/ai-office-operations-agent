import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.meeting.models import (
    Base,
    Meeting,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_FAILED,
    TRANSCRIPT_STATUS_PENDING,
    TRANSCRIPT_STATUS_PROCESSING,
)
from app.meeting.recording_encryption import encrypt_recording_file
from app.meeting.service import get_meeting_transcript, update_meeting_transcript
from app.meeting.transcription_worker import (
    claim_pending_transcript,
    process_claimed_transcript,
)


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


@pytest.fixture(autouse=True)
def recording_encryption_key(monkeypatch):
    key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    monkeypatch.setenv("RECORDING_ENCRYPTION_KEY", key)


def _create_completed_recording(
    db_session: Session,
    storage_root,
    media: bytes = b"english meeting audio",
) -> tuple[MeetingRecording, MeetingTranscript]:
    meeting_id = str(uuid.uuid4())
    recording_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="transcription-organizer",
        title="Transcription Test",
        start_at=now,
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(
        id=recording_id,
        meeting_id=meeting_id,
        status=RECORDING_STATUS_COMPLETED,
        completed_at=now,
    )
    meeting_directory = storage_root / meeting_id
    meeting_directory.mkdir(parents=True, exist_ok=True)
    encrypted_path = meeting_directory / f"{recording_id}.webm"
    plaintext_source = storage_root / f"{recording_id}.source.webm"
    plaintext_source.write_bytes(media)
    encrypt_recording_file(plaintext_source, encrypted_path)
    plaintext_source.unlink()
    recording.storage_uri = f"{meeting_id}/{recording_id}.webm"
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording_id,
        status=TRANSCRIPT_STATUS_PENDING,
        language="en",
    )
    db_session.add_all([meeting, recording, transcript])
    db_session.commit()
    return recording, transcript


def test_transcript_is_unique_per_recording(db_session: Session, tmp_path):
    recording, transcript = _create_completed_recording(db_session, tmp_path)
    duplicate = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status=TRANSCRIPT_STATUS_PENDING,
        language="en",
    )
    db_session.add(duplicate)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()
    assert db_session.query(MeetingTranscript).filter_by(recording_id=recording.id).count() == 1
    assert db_session.get(MeetingTranscript, transcript.id) is not None


def test_get_meeting_transcript_returns_latest_transcript(
    db_session: Session,
    tmp_path,
):
    recording, transcript = _create_completed_recording(db_session, tmp_path)
    second_recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=recording.meeting_id,
        status=RECORDING_STATUS_COMPLETED,
        created_at=transcript.created_at + timedelta(seconds=1),
    )
    latest_transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=second_recording.id,
        status=TRANSCRIPT_STATUS_PENDING,
        language="en",
        created_at=transcript.created_at + timedelta(seconds=1),
    )
    db_session.add_all([second_recording, latest_transcript])
    db_session.commit()

    assert get_meeting_transcript(db_session, recording.meeting_id) == latest_transcript
    assert get_meeting_transcript(db_session, "missing-meeting") is None


def test_update_meeting_transcript_changes_only_segment_text(
    db_session: Session,
    tmp_path,
):
    recording, transcript = _create_completed_recording(db_session, tmp_path)
    original_segments = [
        {
            "start": 0.25,
            "end": 1.5,
            "text": "Hello there.",
            "speaker": "Alice",
            "flagged_for_correction": True,
        },
        {
            "start": 1.5,
            "end": 2.75,
            "text": "How are you?",
            "speaker": "Bob",
            "flagged_for_correction": False,
        },
    ]
    transcript.segments = original_segments
    db_session.commit()

    updated = update_meeting_transcript(
        db_session,
        recording.meeting_id,
        [{"text": "Hi."}, {"text": "Doing well."}],
        "test-editor",
    )

    assert updated is not None
    assert updated.segments == [
        {
            "start": 0.25,
            "end": 1.5,
            "text": "Hi.",
            "speaker": "Alice",
            "flagged_for_correction": True,
        },
        {
            "start": 1.5,
            "end": 2.75,
            "text": "Doing well.",
            "speaker": "Bob",
            "flagged_for_correction": False,
        },
    ]
    assert updated.transcript_text == "Hi. Doing well."
    assert (
        update_meeting_transcript(db_session, "missing-meeting", [], "test-editor")
        is None
    )


def test_worker_claims_pending_transcript_only_once(db_session: Session, tmp_path):
    _, transcript = _create_completed_recording(db_session, tmp_path)

    assert claim_pending_transcript(db_session) == transcript.id
    assert claim_pending_transcript(db_session) is None
    db_session.expire_all()
    assert db_session.get(MeetingTranscript, transcript.id).status == TRANSCRIPT_STATUS_PROCESSING


def test_worker_stores_english_timestamped_segments_and_cleans_plaintext(
    db_session: Session,
    tmp_path,
):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    temporary_directory = tmp_path / "plaintext"
    temporary_directory.mkdir()
    recording, transcript = _create_completed_recording(
        db_session,
        storage_root,
        media=b"decoded english audio",
    )
    claimed_id = claim_pending_transcript(db_session)
    assert claimed_id == transcript.id

    class FakeWhisperModel:
        language = None
        input_path = None

        def transcribe(self, audio_path, *, language):
            self.language = language
            self.input_path = audio_path
            assert Path(audio_path).read_bytes() == b"decoded english audio"
            return iter(
                [
                    SimpleNamespace(start=0.25, end=1.5, text=" Hello there."),
                    SimpleNamespace(start=1.5, end=2.75, text=" How are you?"),
                ]
            ), SimpleNamespace(language="en")

    model = FakeWhisperModel()
    assert process_claimed_transcript(
        db_session,
        transcript.id,
        model,
        storage_root=storage_root,
        temporary_directory=temporary_directory,
    )

    db_session.expire_all()
    completed = db_session.get(MeetingTranscript, transcript.id)
    assert completed.status == TRANSCRIPT_STATUS_COMPLETED
    assert completed.language == "en"
    assert completed.transcript_text == "Hello there. How are you?"
    assert completed.segments == [
        {"start": 0.25, "end": 1.5, "text": "Hello there.", "speaker": "Unknown Speaker", "flagged_for_correction": True},
        {"start": 1.5, "end": 2.75, "text": "How are you?", "speaker": "Unknown Speaker", "flagged_for_correction": True},
    ]
    assert model.language == "en"
    assert not Path(model.input_path).exists()
    assert list(temporary_directory.iterdir()) == []
    assert db_session.get(MeetingRecording, recording.id).status == RECORDING_STATUS_COMPLETED


def test_decryption_failure_is_persisted_and_plaintext_is_removed(
    db_session: Session,
    tmp_path,
):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    temporary_directory = tmp_path / "plaintext"
    temporary_directory.mkdir()
    recording, transcript = _create_completed_recording(db_session, storage_root)
    encrypted_path = storage_root / recording.storage_uri
    encrypted_path.write_bytes(b"not an encrypted recording")
    assert claim_pending_transcript(db_session) == transcript.id

    assert not process_claimed_transcript(
        db_session,
        transcript.id,
        model=object(),
        storage_root=storage_root,
        temporary_directory=temporary_directory,
    )

    db_session.expire_all()
    failed = db_session.get(MeetingTranscript, transcript.id)
    assert failed.status == TRANSCRIPT_STATUS_FAILED
    assert failed.error_message
    assert failed.completed_at is not None
    assert list(temporary_directory.iterdir()) == []


def test_inference_failure_is_persisted_and_plaintext_is_removed(
    db_session: Session,
    tmp_path,
):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    temporary_directory = tmp_path / "plaintext"
    temporary_directory.mkdir()
    _, transcript = _create_completed_recording(db_session, storage_root)
    assert claim_pending_transcript(db_session) == transcript.id

    class FailingWhisperModel:
        input_path = None

        def transcribe(self, audio_path, *, language):
            self.input_path = audio_path
            raise RuntimeError("inference unavailable")

    model = FailingWhisperModel()
    assert not process_claimed_transcript(
        db_session,
        transcript.id,
        model,
        storage_root=storage_root,
        temporary_directory=temporary_directory,
    )

    db_session.expire_all()
    failed = db_session.get(MeetingTranscript, transcript.id)
    assert failed.status == TRANSCRIPT_STATUS_FAILED
    assert "inference unavailable" in failed.error_message
    assert failed.completed_at is not None
    assert not Path(model.input_path).exists()
    assert list(temporary_directory.iterdir()) == []