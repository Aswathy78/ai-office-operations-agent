from datetime import datetime, timedelta, timezone
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
    MeetingRecordingConsent,
    RECORDING_CONSENT_STATUS_CONSENTED,
    RECORDING_CONSENT_STATUS_DECLINED,
    RECORDING_STATUS_PROCESSING,
    RECORDING_STATUS_RECORDING,
)

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


def _create_meeting(db_session: Session) -> tuple[Meeting, list[MeetingAttendanceLog]]:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id=ORGANIZER_ID,
        title="Recording API Test",
        start_at=now - timedelta(minutes=5),
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="active",
    )
    attendees = [
        MeetingAttendanceLog(
            id=str(uuid.uuid4()),
            meeting_id=meeting_id,
            participant_name="Internal Participant",
            user_id="participant-user",
        ),
        MeetingAttendanceLog(
            id=str(uuid.uuid4()),
            meeting_id=meeting_id,
            participant_name="External Guest",
            participant_email="guest@example.com",
            is_external_guest=True,
        ),
    ]
    db_session.add_all([meeting, *attendees])
    db_session.commit()
    return meeting, attendees


def _consent(client: TestClient, meeting_id: str, attendance_id: str, consent_status: str):
    return client.post(
        f"/api/v1/meetings/{meeting_id}/recording/consent",
        json={"attendance_id": attendance_id, "consent_status": consent_status},
    )


def _start_recording(client: TestClient, meeting_id: str):
    return client.post(
        f"/api/v1/meetings/{meeting_id}/recording/start?organizer_id={ORGANIZER_ID}"
    )


def test_consent_is_persisted_and_repeated_requests_do_not_duplicate(
    client: TestClient,
    db_session: Session,
):
    meeting, attendees = _create_meeting(db_session)
    attendance = attendees[0]

    first = _consent(
        client,
        meeting.id,
        attendance.id,
        RECORDING_CONSENT_STATUS_CONSENTED,
    )
    repeated = _consent(
        client,
        meeting.id,
        attendance.id,
        RECORDING_CONSENT_STATUS_CONSENTED,
    )

    assert first.status_code == repeated.status_code == 200
    assert first.json()["id"] == repeated.json()["id"]
    assert first.json()["consented_at"] == repeated.json()["consented_at"]
    assert db_session.query(MeetingRecordingConsent).filter_by(
        meeting_id=meeting.id,
        attendance_id=attendance.id,
    ).count() == 1


def test_recording_cannot_start_until_every_active_attendee_consents(
    client: TestClient,
    db_session: Session,
):
    meeting, attendees = _create_meeting(db_session)
    _consent(client, meeting.id, attendees[0].id, RECORDING_CONSENT_STATUS_CONSENTED)

    response = _start_recording(client, meeting.id)

    assert response.status_code == 409
    assert "all active participants" in response.json()["detail"].lower()
    assert db_session.query(MeetingRecording).filter_by(meeting_id=meeting.id).count() == 0


def test_recording_starts_after_all_internal_and_external_attendees_consent(
    client: TestClient,
    db_session: Session,
):
    meeting, attendees = _create_meeting(db_session)
    for attendance in attendees:
        response = _consent(
            client,
            meeting.id,
            attendance.id,
            RECORDING_CONSENT_STATUS_CONSENTED,
        )
        assert response.status_code == 200

    response = _start_recording(client, meeting.id)

    assert response.status_code == 200
    assert response.json()["status"] == RECORDING_STATUS_RECORDING
    assert response.json()["started_at"] is not None
    assert response.json()["completed_at"] is None


def test_repeated_start_reuses_the_active_recording(
    client: TestClient,
    db_session: Session,
):
    meeting, attendees = _create_meeting(db_session)
    for attendance in attendees:
        _consent(client, meeting.id, attendance.id, RECORDING_CONSENT_STATUS_CONSENTED)

    first = _start_recording(client, meeting.id)
    repeated = _start_recording(client, meeting.id)

    assert first.status_code == repeated.status_code == 200
    assert first.json()["id"] == repeated.json()["id"]
    assert first.json()["started_at"] == repeated.json()["started_at"]
    assert db_session.query(MeetingRecording).filter_by(meeting_id=meeting.id).count() == 1


def test_stop_moves_active_recording_to_processing_idempotently(
    client: TestClient,
    db_session: Session,
):
    meeting, attendees = _create_meeting(db_session)
    for attendance in attendees:
        _consent(client, meeting.id, attendance.id, RECORDING_CONSENT_STATUS_CONSENTED)
    started = _start_recording(client, meeting.id)
    assert started.status_code == 200

    stop_url = f"/api/v1/meetings/{meeting.id}/recording/stop?organizer_id={ORGANIZER_ID}"
    first = client.post(stop_url)
    repeated = client.post(stop_url)

    assert first.status_code == repeated.status_code == 200
    assert first.json()["status"] == RECORDING_STATUS_PROCESSING
    assert first.json()["completed_at"] is None
    assert repeated.json()["id"] == first.json()["id"]
    assert repeated.json()["status"] == RECORDING_STATUS_PROCESSING
    assert db_session.get(MeetingRecording, first.json()["id"]).status == RECORDING_STATUS_PROCESSING


def test_start_is_rejected_while_latest_recording_is_processing(
    client: TestClient,
    db_session: Session,
):
    meeting, attendees = _create_meeting(db_session)
    for attendance in attendees:
        _consent(client, meeting.id, attendance.id, RECORDING_CONSENT_STATUS_CONSENTED)
    started = _start_recording(client, meeting.id)
    client.post(
        f"/api/v1/meetings/{meeting.id}/recording/stop?organizer_id={ORGANIZER_ID}"
    )

    response = _start_recording(client, meeting.id)

    assert started.status_code == 200
    assert response.status_code == 409
    assert "processing" in response.json()["detail"].lower()


def test_stop_without_an_active_recording_is_rejected(
    client: TestClient,
    db_session: Session,
):
    meeting, _ = _create_meeting(db_session)

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/recording/stop?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 409


def test_declined_consent_does_not_satisfy_start_requirement(
    client: TestClient,
    db_session: Session,
):
    meeting, attendees = _create_meeting(db_session)
    _consent(client, meeting.id, attendees[0].id, RECORDING_CONSENT_STATUS_CONSENTED)
    declined = _consent(
        client,
        meeting.id,
        attendees[1].id,
        RECORDING_CONSENT_STATUS_DECLINED,
    )

    response = _start_recording(client, meeting.id)

    assert declined.status_code == 200
    assert declined.json()["consent_status"] == RECORDING_CONSENT_STATUS_DECLINED
    assert response.status_code == 409


def test_recording_read_requires_organizer_and_returns_state_without_media(
    client: TestClient,
    db_session: Session,
):
    meeting, attendees = _create_meeting(db_session)
    for attendance in attendees:
        _consent(client, meeting.id, attendance.id, RECORDING_CONSENT_STATUS_CONSENTED)
    started = _start_recording(client, meeting.id)

    forbidden = client.get(
        f"/api/v1/meetings/{meeting.id}/recording?organizer_id=someone-else"
    )
    state = client.get(
        f"/api/v1/meetings/{meeting.id}/recording?organizer_id={ORGANIZER_ID}"
    )

    assert forbidden.status_code == 403
    assert state.status_code == 200
    assert state.json()["id"] == started.json()["id"]
    assert state.json()["storage_uri"] is None
    assert "media" not in state.json()


def test_deleting_meeting_removes_recording_and_consent_rows(
    client: TestClient,
    db_session: Session,
):
    meeting, attendees = _create_meeting(db_session)
    meeting.status = "completed"
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status="completed",
    )
    consent = MeetingRecordingConsent(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        attendance_id=attendees[0].id,
        consent_status=RECORDING_CONSENT_STATUS_CONSENTED,
        consented_at=datetime.now(timezone.utc),
    )
    db_session.add_all([recording, consent])
    db_session.commit()

    response = client.delete(
        f"/api/v1/meetings/{meeting.id}?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 204
    assert db_session.query(MeetingRecording).filter_by(meeting_id=meeting.id).count() == 0
    assert db_session.query(MeetingRecordingConsent).filter_by(meeting_id=meeting.id).count() == 0
