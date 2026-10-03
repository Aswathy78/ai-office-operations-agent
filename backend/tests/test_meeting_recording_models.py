from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.meeting.models import (
    Base,
    Meeting,
    MeetingAttendanceLog,
    MeetingParticipant,
    MeetingRecording,
    MeetingRecordingConsent,
    RECORDING_CONSENT_STATUS_CONSENTED,
    RECORDING_RETENTION_DAYS,
    RECORDING_STATUSES,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _create_meeting(db_session: Session) -> Meeting:
    meeting_id = str(uuid.uuid4())
    meeting = Meeting(
        id=meeting_id,
        organizer_id="organizer-test",
        title="Recording Model Test",
        start_at=datetime.now(timezone.utc),
        end_at=datetime.now(timezone.utc) + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
    )
    db_session.add(meeting)
    db_session.flush()
    return meeting


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def test_create_tables_includes_recording_and_consent_models(db_session: Session):
    table_names = set(inspect(db_session.get_bind()).get_table_names())

    assert "meeting_recordings" in table_names
    assert "meeting_recording_consents" in table_names


def test_recording_accepts_each_defined_status_and_rejects_unknown_status(
    db_session: Session,
):
    meeting = _create_meeting(db_session)
    recordings = [
        MeetingRecording(id=str(uuid.uuid4()), meeting_id=meeting.id, status=status)
        for status in RECORDING_STATUSES
    ]
    db_session.add_all(recordings)
    db_session.commit()

    assert {recording.status for recording in db_session.query(MeetingRecording).all()} == set(
        RECORDING_STATUSES
    )

    db_session.add(
        MeetingRecording(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            status="unknown",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_recording_stores_storage_uri_and_90_day_retention(db_session: Session):
    meeting = _create_meeting(db_session)
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        storage_uri="s3://private-recordings/recording-123",
    )
    db_session.add(recording)
    db_session.commit()
    db_session.expire_all()

    saved = db_session.get(MeetingRecording, recording.id)
    assert saved is not None
    assert saved.storage_uri == "s3://private-recordings/recording-123"
    assert saved.status == "pending"
    retention = _as_utc(saved.retention_expires_at) - _as_utc(saved.created_at)
    assert abs(retention - timedelta(days=RECORDING_RETENTION_DAYS)) < timedelta(seconds=1)


def test_recording_can_reference_meeting_and_participant(db_session: Session):
    meeting = _create_meeting(db_session)
    participant = MeetingParticipant(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        user_id="participant-test",
        role="participant",
    )
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        participant_id=participant.id,
    )
    db_session.add_all([participant, recording])
    db_session.commit()
    db_session.expire_all()

    saved = db_session.get(MeetingRecording, recording.id)
    assert saved is not None
    assert saved.meeting_id == meeting.id
    assert saved.participant_id == participant.id


def test_consent_persists_status_timestamps_and_attendance_association(
    db_session: Session,
):
    meeting = _create_meeting(db_session)
    attendance = MeetingAttendanceLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        participant_name="Recording Participant",
    )
    consented_at = datetime.now(timezone.utc)
    created_at = consented_at - timedelta(minutes=1)
    consent = MeetingRecordingConsent(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        attendance_id=attendance.id,
        consent_status=RECORDING_CONSENT_STATUS_CONSENTED,
        consented_at=consented_at,
        created_at=created_at,
    )
    db_session.add_all([attendance, consent])
    db_session.commit()
    db_session.expire_all()

    saved = db_session.get(MeetingRecordingConsent, consent.id)
    assert saved is not None
    assert saved.meeting_id == meeting.id
    assert saved.attendance_id == attendance.id
    assert saved.consent_status == RECORDING_CONSENT_STATUS_CONSENTED
    assert _as_utc(saved.consented_at) == consented_at
    assert _as_utc(saved.created_at) == created_at
    assert saved.revoked_at is None
