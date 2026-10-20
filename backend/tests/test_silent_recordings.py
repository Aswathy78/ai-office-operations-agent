"""A recording with no sound must fail clearly, not become an invented transcript."""
import base64
from datetime import datetime, timedelta, timezone
import io
import math
import secrets
import struct
import uuid
import wave

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.meeting import transcription_worker
from app.meeting.models import (
    Base,
    Meeting,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_FAILED,
    TRANSCRIPT_STATUS_PENDING,
)
from app.meeting.recording_encryption import encrypt_recording_file
from app.meeting.transcription_worker import (
    claim_pending_transcript,
    process_claimed_transcript,
    recording_is_silent,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("RECORDING_ENCRYPTION_KEY", base64.b64encode(secrets.token_bytes(32)).decode("ascii"))


def _wav(seconds: float = 2.0, amplitude: float = 0.0) -> bytes:
    """16 kHz mono PCM: silence (amplitude 0) or a 440 Hz tone at speech-like level."""
    rate = 16000
    frames = b"".join(
        struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(int(seconds * rate))
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return buffer.getvalue()


def _recording(db: Session, storage_root, media: bytes) -> MeetingTranscript:
    meeting_id, recording_id = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db.add(Meeting(id=meeting_id, organizer_id="organizer", title="Silent test", start_at=now,
                   end_at=now + timedelta(hours=1), join_url=f"http://localhost/meetings/{meeting_id}",
                   status="completed"))
    relative = f"{meeting_id}/{recording_id}.webm"
    path = storage_root / relative
    path.parent.mkdir(parents=True)
    plaintext = path.with_suffix(".plain")
    plaintext.write_bytes(media)
    encrypt_recording_file(plaintext, path)
    plaintext.unlink()
    db.add(MeetingRecording(id=recording_id, meeting_id=meeting_id, status=RECORDING_STATUS_COMPLETED,
                            storage_uri=relative, completed_at=now))
    transcript = MeetingTranscript(id=str(uuid.uuid4()), recording_id=recording_id,
                                   status=TRANSCRIPT_STATUS_PENDING)
    db.add(transcript)
    db.commit()
    return transcript


class RecordingModel:
    """Fake Whisper that would 'hallucinate' text the way the real one does on silence."""

    def __init__(self):
        self.called = False

    def transcribe(self, audio_path, *, language, word_timestamps):
        self.called = True
        segment = type("Segment", (), {"start": 0.0, "end": 1.0, "text": "You", "words": []})()
        return iter([segment]), None


def test_silence_detection(tmp_path):
    silent, tone, junk = tmp_path / "silent.wav", tmp_path / "tone.wav", tmp_path / "junk.webm"
    silent.write_bytes(_wav(amplitude=0.0))
    tone.write_bytes(_wav(amplitude=0.06))  # about -27 dBFS, like the working recordings
    junk.write_bytes(b"not audio at all")

    assert recording_is_silent(silent) is True
    assert recording_is_silent(tone) is False
    assert recording_is_silent(junk) is False  # undecodable: left for Whisper to report


def test_silent_recording_fails_clearly_without_summary_or_action_items(db_session, tmp_path, monkeypatch):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    transcript = _recording(db_session, storage_root, _wav(seconds=3, amplitude=0.0))
    summaries, extractions = [], []
    monkeypatch.setattr(transcription_worker, "generate_meeting_summary", lambda db, tid: summaries.append(tid))
    monkeypatch.setattr(transcription_worker, "generate_meeting_action_items", lambda db, tid: extractions.append(tid))
    assert claim_pending_transcript(db_session) == transcript.id
    model = RecordingModel()

    assert not process_claimed_transcript(db_session, transcript.id, model, storage_root=storage_root,
                                          temporary_directory=tmp_path)

    db_session.expire_all()
    failed = db_session.get(MeetingTranscript, transcript.id)
    assert failed.status == TRANSCRIPT_STATUS_FAILED
    assert "contains no sound" in failed.error_message
    assert "microphone" in failed.error_message
    assert failed.transcript_text is None  # no invented "You"
    assert model.called is False
    assert summaries == [] and extractions == []
    assert db_session.query(MeetingSummary).count() == 0
    assert db_session.query(MeetingActionItemExtraction).count() == 0


def test_audible_recording_is_still_transcribed(db_session, tmp_path, monkeypatch):
    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    transcript = _recording(db_session, storage_root, _wav(seconds=2, amplitude=0.06))
    monkeypatch.setattr(transcription_worker, "generate_meeting_summary", lambda db, tid: None)
    monkeypatch.setattr(transcription_worker, "generate_meeting_action_items", lambda db, tid: None)
    assert claim_pending_transcript(db_session) == transcript.id
    model = RecordingModel()

    assert process_claimed_transcript(db_session, transcript.id, model, storage_root=storage_root,
                                      temporary_directory=tmp_path)

    db_session.expire_all()
    assert db_session.get(MeetingTranscript, transcript.id).status == TRANSCRIPT_STATUS_COMPLETED
    assert model.called is True
