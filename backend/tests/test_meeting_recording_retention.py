from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.meeting.models import (
    Base,
    Meeting,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_RETENTION_DAYS,
    RECORDING_STATUS_CANCELLED,
    RECORDING_STATUS_COMPLETED,
    RECORDING_STATUS_FAILED,
    RECORDING_STATUS_PROCESSING,
    RECORDING_STATUS_RECORDING,
    TRANSCRIPT_STATUS_COMPLETED,
)
from app.meeting import recording_retention
from app.meeting.recording_retention import enforce_recording_retention


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def retention_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(recording_retention, "RECORDING_STORAGE_DIR", tmp_path)
    return tmp_path


def _create_meeting(db_session: Session) -> Meeting:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="retention-organizer",
        title="Retention Test Meeting",
        start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    db_session.add(meeting)
    db_session.flush()
    return meeting


def _add_recording(
    db_session: Session,
    meeting: Meeting,
    recording_id: str,
    *,
    status: str = RECORDING_STATUS_COMPLETED,
    expires_at: datetime,
    storage_uri: str | None = None,
) -> MeetingRecording:
    recording = MeetingRecording(
        id=recording_id,
        meeting_id=meeting.id,
        status=status,
        retention_expires_at=expires_at,
        storage_uri=storage_uri,
    )
    db_session.add(recording)
    db_session.commit()
    return recording


def _recording_uri(meeting_id: str, recording_id: str) -> str:
    return f"{meeting_id}/{recording_id}.webm"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stored_file(root: Path, uri: str, contents: bytes = b"encrypted recording bytes") -> Path:
    file_path = root / uri
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(contents)
    return file_path


def test_new_recording_default_expiry_uses_90_day_retention(db_session: Session):
    meeting = _create_meeting(db_session)
    before_create = datetime.now(timezone.utc)
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=RECORDING_STATUS_COMPLETED,
    )
    db_session.add(recording)
    db_session.commit()
    db_session.refresh(recording)

    actual_duration = recording.retention_expires_at - recording.created_at
    assert abs(actual_duration - timedelta(days=RECORDING_RETENTION_DAYS)) < timedelta(seconds=1)
    assert _as_utc(recording.created_at) >= before_create - timedelta(seconds=1)


def test_expired_completed_recording_file_and_row_are_deleted(
    db_session: Session,
    retention_storage: Path,
):
    meeting = _create_meeting(db_session)
    recording_id = str(uuid.uuid4())
    uri = _recording_uri(meeting.id, recording_id)
    stored_file = _stored_file(retention_storage, uri)
    recording = _add_recording(
        db_session,
        meeting,
        recording_id,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        storage_uri=uri,
    )
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status=TRANSCRIPT_STATUS_COMPLETED,
        language="en",
        transcript_text="Expired recording transcript",
    )
    transcript_id = transcript.id
    db_session.add(transcript)
    db_session.commit()

    report = enforce_recording_retention(db_session, storage_root=retention_storage)

    assert report.deleted_recording_ids == [recording.id]
    assert report.failures == []
    assert not stored_file.exists()
    assert db_session.get(MeetingRecording, recording.id) is None
    assert db_session.get(MeetingTranscript, transcript_id) is None


def test_unexpired_recording_is_preserved(db_session: Session, retention_storage: Path):
    meeting = _create_meeting(db_session)
    recording_id = str(uuid.uuid4())
    uri = _recording_uri(meeting.id, recording_id)
    stored_file = _stored_file(retention_storage, uri)
    recording = _add_recording(
        db_session,
        meeting,
        recording_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        storage_uri=uri,
    )

    report = enforce_recording_retention(db_session, storage_root=retention_storage)

    assert recording.id in report.skipped_recording_ids
    assert report.deleted_recording_ids == []
    assert stored_file.is_file()
    assert db_session.get(MeetingRecording, recording.id) is not None


def test_missing_file_cleanup_is_safe_and_idempotent(
    db_session: Session,
    retention_storage: Path,
):
    meeting = _create_meeting(db_session)
    recording_id = str(uuid.uuid4())
    uri = _recording_uri(meeting.id, recording_id)
    recording = _add_recording(
        db_session,
        meeting,
        recording_id,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        storage_uri=uri,
    )

    first_report = enforce_recording_retention(db_session, storage_root=retention_storage)
    second_report = enforce_recording_retention(db_session, storage_root=retention_storage)

    assert first_report.deleted_recording_ids == [recording.id]
    assert first_report.failures == []
    assert second_report.deleted_recording_ids == []
    assert second_report.failures == []
    assert db_session.get(MeetingRecording, recording.id) is None


def test_file_deletion_failure_preserves_row_and_does_not_block_other_recordings(
    db_session: Session,
    retention_storage: Path,
    monkeypatch,
):
    meeting = _create_meeting(db_session)
    failure_recording_id = "00000000-0000-0000-0000-000000000001"
    success_recording_id = "00000000-0000-0000-0000-000000000002"
    failure_uri = _recording_uri(meeting.id, failure_recording_id)
    success_uri = _recording_uri(meeting.id, success_recording_id)
    failure_file = _stored_file(retention_storage, failure_uri)
    success_file = _stored_file(retention_storage, success_uri)
    _add_recording(
        db_session,
        meeting,
        failure_recording_id,
        expires_at=datetime.now(timezone.utc) - timedelta(days=2),
        storage_uri=failure_uri,
    )
    _add_recording(
        db_session,
        meeting,
        success_recording_id,
        status=RECORDING_STATUS_FAILED,
        expires_at=datetime.now(timezone.utc) - timedelta(days=2),
        storage_uri=success_uri,
    )

    original_unlink = Path.unlink

    def fail_selected_delete(path: Path, missing_ok: bool = False):
        if path == failure_file:
            raise PermissionError("simulated filesystem refusal")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_selected_delete)

    report = enforce_recording_retention(db_session, storage_root=retention_storage)

    assert report.deleted_recording_ids == [success_recording_id]
    assert len(report.failures) == 1
    assert report.failures[0].recording_id == failure_recording_id
    assert "PermissionError" in report.failures[0].reason
    assert failure_file.is_file()
    assert db_session.get(MeetingRecording, failure_recording_id) is not None
    assert not success_file.exists()
    assert db_session.get(MeetingRecording, success_recording_id) is None


def test_unsafe_storage_uri_cannot_delete_outside_storage(
    db_session: Session,
    retention_storage: Path,
    tmp_path: Path,
):
    meeting = _create_meeting(db_session)
    recording_id = str(uuid.uuid4())
    outside_file = tmp_path / "outside-recording.webm"
    outside_file.write_bytes(b"must remain")
    unsafe_uri = f"../../{outside_file.name}"
    recording = _add_recording(
        db_session,
        meeting,
        recording_id,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        storage_uri=unsafe_uri,
    )

    report = enforce_recording_retention(db_session, storage_root=retention_storage)

    assert report.deleted_recording_ids == []
    assert len(report.failures) == 1
    assert report.failures[0].recording_id == recording.id
    assert outside_file.read_bytes() == b"must remain"
    assert db_session.get(MeetingRecording, recording.id) is not None


def test_expired_recording_and_processing_rows_are_not_deleted(
    db_session: Session,
    retention_storage: Path,
):
    meeting = _create_meeting(db_session)
    expired_at = datetime.now(timezone.utc) - timedelta(days=1)
    active_ids = [
        "00000000-0000-0000-0000-000000000011",
        "00000000-0000-0000-0000-000000000012",
    ]
    active_records = []
    for recording_id, status in zip(
        active_ids,
        (RECORDING_STATUS_RECORDING, RECORDING_STATUS_PROCESSING),
        strict=True,
    ):
        uri = _recording_uri(meeting.id, recording_id)
        _stored_file(retention_storage, uri)
        active_records.append(
            _add_recording(
                db_session,
                meeting,
                recording_id,
                status=status,
                expires_at=expired_at,
                storage_uri=uri,
            )
        )

    report = enforce_recording_retention(db_session, storage_root=retention_storage)

    assert report.deleted_recording_ids == []
    assert report.failures == []
    for recording in active_records:
        assert db_session.get(MeetingRecording, recording.id) is not None
        assert (retention_storage / recording.storage_uri).is_file()


def test_cleanup_can_be_repeated_after_success(db_session: Session, retention_storage: Path):
    meeting = _create_meeting(db_session)
    recording_id = str(uuid.uuid4())
    uri = _recording_uri(meeting.id, recording_id)
    _stored_file(retention_storage, uri)
    _add_recording(
        db_session,
        meeting,
        recording_id,
        status=RECORDING_STATUS_CANCELLED,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        storage_uri=uri,
    )

    first = enforce_recording_retention(db_session, storage_root=retention_storage)
    second = enforce_recording_retention(db_session, storage_root=retention_storage)

    assert first.deleted_recording_ids == [recording_id]
    assert second.deleted_recording_ids == []
    assert first.failures == second.failures == []
