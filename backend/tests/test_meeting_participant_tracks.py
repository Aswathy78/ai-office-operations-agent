"""Tests for FR-8 Stage 2: participant-track recording and speaker identification.

Covers:
    1.  Register two participant tracks
    2.  Registration authorization
    3.  Explicit participant-track upload
    4.  First recording by ID completes; second stays processing
    5.  Wrong recording ID -> 404
    6.  Recording from another meeting -> 404
    7.  Organizer (connection_id=NULL) recording rejected at participant-track endpoint
    8.  Speaker metadata: known participant
    9.  Unknown speaker (participant_name=None)
    10. Existing /recording/upload endpoint regression
    11. Delete meeting removes participant-track recordings
"""

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
from types import SimpleNamespace
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
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    RECORDING_STATUS_PROCESSING,
    TRANSCRIPT_STATUS_PENDING,
)
from app.meeting import service as meeting_service
from app.meeting.recording_encryption import encrypt_recording_file, decrypt_recording_file
from app.meeting.transcription_worker import (
    claim_pending_transcript,
    process_claimed_transcript,
)

ORGANIZER_ID = "fr8-organizer"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_meeting(db_session: Session, organizer_id: str = ORGANIZER_ID) -> Meeting:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id=organizer_id,
        title="FR-8 Test Meeting",
        start_at=now - timedelta(minutes=30),
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="active",
    )
    db_session.add(meeting)
    db_session.commit()
    return meeting


def _register_tracks(client, meeting_id, tracks, organizer_id=ORGANIZER_ID):
    return client.post(
        f"/api/v1/meetings/{meeting_id}/recording/tracks?organizer_id={organizer_id}",
        json={"tracks": tracks},
    )


def _upload_track(client, meeting_id, recording_id, content=b"audio data", organizer_id=ORGANIZER_ID):
    return client.post(
        f"/api/v1/meetings/{meeting_id}/recording/{recording_id}/upload?organizer_id={organizer_id}",
        files={"file": ("track.webm", content, "audio/webm")},
    )


def _create_completed_recording_with_transcript(
    db_session: Session,
    storage_root: Path,
    meeting_id: str,
    participant_name: str | None = None,
    connection_id: str | None = None,
    media: bytes = b"participant audio",
) -> tuple[MeetingRecording, MeetingTranscript]:
    recording_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    recording = MeetingRecording(
        id=recording_id,
        meeting_id=meeting_id,
        status=RECORDING_STATUS_COMPLETED,
        completed_at=now,
        participant_name=participant_name,
        connection_id=connection_id,
    )
    meeting_dir = storage_root / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    plaintext_source = storage_root / f"{recording_id}.source.webm"
    plaintext_source.write_bytes(media)
    encrypted_path = meeting_dir / f"{recording_id}.webm"
    encrypt_recording_file(plaintext_source, encrypted_path)
    plaintext_source.unlink()
    recording.storage_uri = f"{meeting_id}/{recording_id}.webm"
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording_id,
        status=TRANSCRIPT_STATUS_PENDING,
        language="en",
    )
    db_session.add_all([recording, transcript])
    db_session.commit()
    return recording, transcript


class FakeWhisperModel:
    def __init__(self, segments=None):
        self._segments = segments or [
            SimpleNamespace(start=0.0, end=1.0, text=" Hello."),
            SimpleNamespace(start=1.0, end=2.0, text=" World."),
        ]

    def transcribe(self, audio_path, *, language, word_timestamps):
        return iter(self._segments), SimpleNamespace(language="en")


# ---------------------------------------------------------------------------
# 1. Register two participant tracks
# ---------------------------------------------------------------------------


def test_register_two_participant_tracks_creates_processing_recordings(
    client: TestClient,
    db_session: Session,
):
    meeting = _create_meeting(db_session)
    tracks_payload = [
        {"connection_id": "conn-alice", "participant_name": "Alice", "attendance_id": "att-alice"},
        {"connection_id": "conn-bob", "participant_name": "Bob"},
    ]

    response = _register_tracks(client, meeting.id, tracks_payload)

    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result) == 2

    names = {r["participant_name"] for r in result}
    conn_ids = {r["connection_id"] for r in result}
    assert names == {"Alice", "Bob"}
    assert conn_ids == {"conn-alice", "conn-bob"}
    for r in result:
        assert r["status"] == RECORDING_STATUS_PROCESSING
        assert r["meeting_id"] == meeting.id
        assert r["recording_id"]

    db_rows = db_session.query(MeetingRecording).filter_by(meeting_id=meeting.id).all()
    assert len(db_rows) == 2
    for row in db_rows:
        assert row.status == RECORDING_STATUS_PROCESSING
        assert row.connection_id in {"conn-alice", "conn-bob"}
        assert row.participant_name in {"Alice", "Bob"}
        assert row.retention_expires_at is not None
        # No transcript created at registration time
        assert db_session.query(MeetingTranscript).filter_by(recording_id=row.id).count() == 0


# ---------------------------------------------------------------------------
# 2. Registration authorization
# ---------------------------------------------------------------------------


def test_register_tracks_requires_organizer(client: TestClient, db_session: Session):
    meeting = _create_meeting(db_session)
    response = _register_tracks(
        client,
        meeting.id,
        [{"connection_id": "conn-x", "participant_name": "X"}],
        organizer_id="not-the-organizer",
    )
    assert response.status_code == 403
    assert db_session.query(MeetingRecording).filter_by(meeting_id=meeting.id).count() == 0


def test_register_tracks_returns_404_for_unknown_meeting(client: TestClient, db_session: Session):
    response = _register_tracks(
        client,
        str(uuid.uuid4()),
        [{"connection_id": "conn-x", "participant_name": "X"}],
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 3. Explicit participant-track upload
# ---------------------------------------------------------------------------


def test_upload_participant_track_by_recording_id(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    reg = _register_tracks(client, meeting.id, [{"connection_id": "conn-alice", "participant_name": "Alice"}])
    assert reg.status_code == 200
    recording_id = reg.json()[0]["recording_id"]

    media = b"alice participant audio data"
    upload = _upload_track(client, meeting.id, recording_id, content=media)

    assert upload.status_code == 200, upload.text
    result = upload.json()
    assert result["recording_id"] == recording_id
    assert result["status"] == RECORDING_STATUS_COMPLETED
    assert result["storage_uri"]

    stored_file = recording_storage / result["storage_uri"]
    assert stored_file.is_file()
    assert stored_file.read_bytes() != media
    assert decrypt_recording_file(stored_file) == media

    db_session.expire_all()
    saved = db_session.get(MeetingRecording, recording_id)
    assert saved.status == RECORDING_STATUS_COMPLETED
    assert saved.completed_at is not None

    transcripts = db_session.query(MeetingTranscript).filter_by(recording_id=recording_id).all()
    assert len(transcripts) == 1
    assert transcripts[0].status == TRANSCRIPT_STATUS_PENDING


# ---------------------------------------------------------------------------
# 4. Uploading first recording leaves second unchanged (no _latest_meeting_recording())
# ---------------------------------------------------------------------------


def test_upload_first_recording_id_does_not_affect_second(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    reg = _register_tracks(
        client,
        meeting.id,
        [
            {"connection_id": "conn-alice", "participant_name": "Alice"},
            {"connection_id": "conn-bob", "participant_name": "Bob"},
        ],
    )
    assert reg.status_code == 200
    items = reg.json()
    alice_id = next(r["recording_id"] for r in items if r["participant_name"] == "Alice")
    bob_id = next(r["recording_id"] for r in items if r["participant_name"] == "Bob")

    upload = _upload_track(client, meeting.id, alice_id, content=b"alice audio")
    assert upload.status_code == 200
    assert upload.json()["recording_id"] == alice_id

    db_session.expire_all()
    alice_rec = db_session.get(MeetingRecording, alice_id)
    bob_rec = db_session.get(MeetingRecording, bob_id)
    assert alice_rec.status == RECORDING_STATUS_COMPLETED
    assert bob_rec.status == RECORDING_STATUS_PROCESSING  # unchanged

    alice_transcripts = db_session.query(MeetingTranscript).filter_by(recording_id=alice_id).all()
    bob_transcripts = db_session.query(MeetingTranscript).filter_by(recording_id=bob_id).all()
    assert len(alice_transcripts) == 1
    assert len(bob_transcripts) == 0


# ---------------------------------------------------------------------------
# 5. Wrong recording ID -> 404
# ---------------------------------------------------------------------------


def test_upload_with_nonexistent_recording_id_returns_404(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    response = _upload_track(client, meeting.id, str(uuid.uuid4()), content=b"audio")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 6. Recording from another meeting -> 404
# ---------------------------------------------------------------------------


def test_upload_recording_cross_meeting_returns_404(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting_a = _create_meeting(db_session)
    meeting_b = _create_meeting(db_session)

    reg_a = _register_tracks(client, meeting_a.id, [{"connection_id": "conn-a", "participant_name": "Alice"}])
    assert reg_a.status_code == 200
    recording_id_a = reg_a.json()[0]["recording_id"]

    # Try to upload using meeting_b URL with recording_id from meeting_a
    response = _upload_track(client, meeting_b.id, recording_id_a, content=b"audio")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 7. Organizer recording (connection_id=NULL) rejected at participant-track endpoint
# ---------------------------------------------------------------------------


def test_organizer_recording_rejected_at_participant_track_endpoint(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    # Simulate the existing full-meeting organizer recording (no connection_id)
    org_recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=RECORDING_STATUS_PROCESSING,
        # connection_id intentionally omitted (NULL)
    )
    db_session.add(org_recording)
    db_session.commit()

    response = _upload_track(client, meeting.id, org_recording.id, content=b"audio")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 8. Speaker metadata: known participant -> speaker=name, flagged=False
# ---------------------------------------------------------------------------


def test_transcription_worker_labels_known_speaker(db_session: Session, tmp_path):
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id, organizer_id="org",
        title="Speaker Test", start_at=now, end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}", status="completed",
    )
    db_session.add(meeting)
    db_session.flush()

    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    recording, transcript = _create_completed_recording_with_transcript(
        db_session, storage_root, meeting_id,
        participant_name="Alice", connection_id="conn-alice",
    )

    claimed_id = claim_pending_transcript(db_session)
    assert claimed_id == transcript.id

    tmp_dec = tmp_path / "dec"
    tmp_dec.mkdir()
    success = process_claimed_transcript(
        db_session, transcript.id, FakeWhisperModel(),
        storage_root=storage_root, temporary_directory=tmp_dec,
    )
    assert success

    db_session.expire_all()
    completed = db_session.get(MeetingTranscript, transcript.id)
    assert completed.status == "completed"
    for seg in completed.segments:
        assert seg["speaker"] == "Alice"
        assert seg["flagged_for_correction"] is False


# ---------------------------------------------------------------------------
# 9. Unknown speaker: participant_name=None -> "Unknown Speaker", flagged=True
# ---------------------------------------------------------------------------


def test_transcription_worker_flags_unknown_speaker(db_session: Session, tmp_path):
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id, organizer_id="org",
        title="Unknown Speaker Test", start_at=now, end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}", status="completed",
    )
    db_session.add(meeting)
    db_session.flush()

    storage_root = tmp_path / "recordings"
    storage_root.mkdir()
    recording, transcript = _create_completed_recording_with_transcript(
        db_session, storage_root, meeting_id,
        participant_name=None, connection_id=None,
    )

    claim_pending_transcript(db_session)
    tmp_dec = tmp_path / "dec"
    tmp_dec.mkdir()
    process_claimed_transcript(
        db_session, transcript.id, FakeWhisperModel(),
        storage_root=storage_root, temporary_directory=tmp_dec,
    )

    db_session.expire_all()
    completed = db_session.get(MeetingTranscript, transcript.id)
    for seg in completed.segments:
        assert seg["speaker"] == "Unknown Speaker"
        assert seg["flagged_for_correction"] is True


# ---------------------------------------------------------------------------
# 10. Existing /recording/upload endpoint regression
# ---------------------------------------------------------------------------


def test_existing_organizer_recording_upload_unaffected(
    client: TestClient,
    db_session: Session,
    recording_storage,
):
    meeting = _create_meeting(db_session)
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=RECORDING_STATUS_PROCESSING,
    )
    db_session.add(recording)
    db_session.commit()

    media = b"full meeting organizer recording"
    response = client.post(
        f"/api/v1/meetings/{meeting.id}/recording/upload?organizer_id={ORGANIZER_ID}",
        files={"file": ("recording.webm", media, "audio/webm;codecs=opus")},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["recording_id"] == recording.id
    assert result["status"] == RECORDING_STATUS_COMPLETED

    db_session.expire_all()
    saved = db_session.get(MeetingRecording, recording.id)
    assert saved.status == RECORDING_STATUS_COMPLETED
    assert saved.storage_uri is not None
    assert saved.participant_name is None
    assert saved.connection_id is None

    transcripts = db_session.query(MeetingTranscript).filter_by(recording_id=recording.id).all()
    assert len(transcripts) == 1
    assert transcripts[0].status == TRANSCRIPT_STATUS_PENDING


# ---------------------------------------------------------------------------
# 11. Delete meeting removes all participant-track recordings
# ---------------------------------------------------------------------------


def test_delete_meeting_removes_participant_track_recordings(
    client: TestClient,
    db_session: Session,
):
    meeting = _create_meeting(db_session)
    meeting.status = "completed"
    db_session.commit()

    now = datetime.now(timezone.utc)
    for name, conn in [("Alice", "conn-alice"), ("Bob", "conn-bob")]:
        db_session.add(MeetingRecording(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            status=RECORDING_STATUS_PROCESSING,
            participant_name=name,
            connection_id=conn,
            created_at=now,
            retention_expires_at=now + timedelta(days=90),
        ))
    db_session.add(MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=RECORDING_STATUS_PROCESSING,
        created_at=now,
        retention_expires_at=now + timedelta(days=90),
    ))
    db_session.commit()

    assert db_session.query(MeetingRecording).filter_by(meeting_id=meeting.id).count() == 3

    response = client.delete(f"/api/v1/meetings/{meeting.id}?organizer_id={ORGANIZER_ID}")
    assert response.status_code == 204
    assert db_session.query(MeetingRecording).filter_by(meeting_id=meeting.id).count() == 0
