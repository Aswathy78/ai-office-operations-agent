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
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    SUMMARY_STATUS_COMPLETED,
    SUMMARY_STATUS_FAILED,
    SUMMARY_STATUS_PENDING,
    SUMMARY_STATUS_PROCESSING,
    TRANSCRIPT_STATUS_COMPLETED,
)

ORGANIZER_ID = "summary-api-organizer"


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


def _create_meeting_and_summary(
    db_session: Session,
    status: str,
) -> tuple[Meeting, MeetingSummary]:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id=ORGANIZER_ID,
        title="Summary API test",
        start_at=now,
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        status=RECORDING_STATUS_COMPLETED,
    )
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status=TRANSCRIPT_STATUS_COMPLETED,
        language="en",
        transcript_text="Persisted source transcript.",
    )
    summary = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript.id,
        status=status,
        overview="The team reviewed the pilot.",
        decisions=["Run the pilot."],
        discussion_topics=["Pilot timing"],
        open_questions=["Who owns rollout?"],
        error_message="Summary generation failed." if status == SUMMARY_STATUS_FAILED else None,
    )
    db_session.add_all([meeting, recording, transcript, summary])
    db_session.commit()
    return meeting, summary


@pytest.mark.parametrize(
    "summary_status",
    [
        SUMMARY_STATUS_PENDING,
        SUMMARY_STATUS_PROCESSING,
        SUMMARY_STATUS_COMPLETED,
        SUMMARY_STATUS_FAILED,
    ],
)
def test_get_summary_returns_persisted_summary_status_and_sections(
    client: TestClient,
    db_session: Session,
    summary_status: str,
):
    meeting, summary = _create_meeting_and_summary(db_session, summary_status)
    response = client.get(
        f"/api/v1/meetings/{meeting.id}/summary?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == summary.id
    assert body["meeting_id"] == meeting.id
    assert body["transcript_id"] == summary.transcript_id
    assert body["status"] == summary_status
    if summary_status == SUMMARY_STATUS_COMPLETED:
        assert body["overview"] == "The team reviewed the pilot."
        assert body["decisions"] == ["Run the pilot."]
        assert body["discussion_topics"] == ["Pilot timing"]
        assert body["open_questions"] == ["Who owns rollout?"]
    if summary_status == SUMMARY_STATUS_FAILED:
        assert body["error_message"] == "Summary generation failed."


def test_get_summary_returns_404_when_meeting_or_summary_is_missing(
    client: TestClient,
    db_session: Session,
):
    missing_meeting = client.get(
        f"/api/v1/meetings/{uuid.uuid4()}/summary?organizer_id={ORGANIZER_ID}"
    )
    assert missing_meeting.status_code == 404
    assert missing_meeting.json()["detail"] == "Meeting not found"

    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db_session.add(
        Meeting(
            id=meeting_id,
            organizer_id=ORGANIZER_ID,
            title="Meeting without summary",
            start_at=now,
            end_at=now + timedelta(hours=1),
            join_url=f"http://localhost/meetings/{meeting_id}",
            status="completed",
        )
    )
    db_session.commit()

    missing_summary = client.get(
        f"/api/v1/meetings/{meeting_id}/summary?organizer_id={ORGANIZER_ID}"
    )
    assert missing_summary.status_code == 404
    assert missing_summary.json()["detail"] == "Meeting summary not found"


def test_get_summary_requires_meeting_organizer(
    client: TestClient,
    db_session: Session,
):
    meeting, _summary = _create_meeting_and_summary(
        db_session,
        SUMMARY_STATUS_COMPLETED,
    )

    response = client.get(
        f"/api/v1/meetings/{meeting.id}/summary?organizer_id=someone-else"
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Only the meeting organizer can access the summary"
    )


def test_get_summary_only_reads_persisted_status_without_generation(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    meeting, summary = _create_meeting_and_summary(
        db_session,
        SUMMARY_STATUS_PENDING,
    )

    def fail_if_generation_called(*args, **kwargs):
        pytest.fail("GET summary must not generate a summary.")

    monkeypatch.setattr(
        "app.meeting.summary_service.generate_meeting_summary",
        fail_if_generation_called,
    )

    response = client.get(
        f"/api/v1/meetings/{meeting.id}/summary?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 200
    assert response.json()["status"] == SUMMARY_STATUS_PENDING
    assert db_session.get(MeetingSummary, summary.id).status == SUMMARY_STATUS_PENDING
