from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.meeting.models import (
    ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingAttendanceLog,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    SUMMARY_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
)
from main import app


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


def _create_meeting_setup(db: Session, organizer_id: str = "organizer-test-id") -> str:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id=organizer_id,
        title="Engineering All-Hands",
        start_at=now - timedelta(hours=1),
        end_at=now,
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
    )
    summary = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript.id,
        status=SUMMARY_STATUS_COMPLETED,
        overview="All-hands discussion on Q3 roadmap.",
        decisions=["Adopt unified telemetry", "Migrate pipeline by EOM"],
    )
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        transcript_id=transcript.id,
        status="completed",
    )
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Alex River",
        email="alex@example.com",
        is_active=True,
    )
    action_item = MeetingActionItem(
        id=str(uuid.uuid4()),
        extraction_id=extraction.id,
        task_description="Set up telemetry dashboard",
        owner_name="Alex",
        confirmed_employee_id=employee.id,
        deadline_at=now + timedelta(days=5),
        deadline_text="in 5 days",
        priority="high",
        confidence=0.92,
        source_segment_index=0,
        source_start_seconds=12.0,
        source_end_seconds=22.0,
        review_status=ACTION_ITEM_REVIEW_STATUS_CONFIRMED,
    )
    attendee = MeetingAttendanceLog(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        participant_name="Alex River",
        participant_email="alex@example.com",
        joined_at=now - timedelta(minutes=55),
    )
    db.add_all(
        [
            meeting,
            recording,
            transcript,
            summary,
            extraction,
            employee,
            action_item,
            attendee,
        ]
    )
    db.commit()
    return meeting_id


def test_generate_meeting_minutes_as_organizer(client: TestClient, db_session: Session):
    organizer_id = "test-org-1"
    meeting_id = _create_meeting_setup(db_session, organizer_id=organizer_id)

    response = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/generate?organizer_id={organizer_id}"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["meeting_id"] == meeting_id
    assert data["status"] == "pending_approval"
    assert data["policy"] == "manual_approval"
    assert data["content_summary"] == "All-hands discussion on Q3 roadmap."
    assert data["content_decisions"] == [
        "Adopt unified telemetry",
        "Migrate pipeline by EOM",
    ]
    assert len(data["content_action_items"]) == 1
    assert data["content_action_items"][0]["owner_name"] == "Alex River"
    assert data["content_action_items"][0]["owner_email"] == "alex@example.com"
    assert len(data["recipients"]) == 1
    assert data["recipients"][0]["recipient_email"] == "alex@example.com"
    assert data["recipients"][0]["delivery_status"] == "pending"


def test_get_meeting_minutes_endpoint(client: TestClient, db_session: Session):
    organizer_id = "test-org-2"
    meeting_id = _create_meeting_setup(db_session, organizer_id=organizer_id)

    # Before generation -> 404
    get_before = client.get(
        f"/api/v1/meetings/{meeting_id}/minutes?organizer_id={organizer_id}"
    )
    assert get_before.status_code == 404
    assert "not found" in get_before.json()["detail"].lower()

    # Generate
    client.post(f"/api/v1/meetings/{meeting_id}/minutes/generate?organizer_id={organizer_id}")

    # After generation -> 200
    get_after = client.get(
        f"/api/v1/meetings/{meeting_id}/minutes?organizer_id={organizer_id}"
    )
    assert get_after.status_code == 200
    assert get_after.json()["meeting_id"] == meeting_id
    assert len(get_after.json()["recipients"]) == 1


def test_approve_meeting_minutes_endpoint(client: TestClient, db_session: Session):
    organizer_id = "test-org-3"
    meeting_id = _create_meeting_setup(db_session, organizer_id=organizer_id)

    # Generate first
    client.post(f"/api/v1/meetings/{meeting_id}/minutes/generate?organizer_id={organizer_id}")

    # Approve
    approve_res = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/approve?organizer_id={organizer_id}"
    )
    assert approve_res.status_code == 200
    approved_data = approve_res.json()
    assert approved_data["status"] == "approved"
    assert approved_data["approved_by"] == organizer_id
    assert approved_data["approved_at"] is not None


def test_unauthorized_user_cannot_generate_access_or_approve_minutes(
    client: TestClient, db_session: Session
):
    organizer_id = "real-organizer"
    meeting_id = _create_meeting_setup(db_session, organizer_id=organizer_id)

    unauthorized_id = "imposter-user"

    # Unauthorized generate
    gen_res = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/generate?organizer_id={unauthorized_id}"
    )
    assert gen_res.status_code == 403

    # Generate as real organizer first
    client.post(f"/api/v1/meetings/{meeting_id}/minutes/generate?organizer_id={organizer_id}")

    # Unauthorized get
    get_res = client.get(
        f"/api/v1/meetings/{meeting_id}/minutes?organizer_id={unauthorized_id}"
    )
    assert get_res.status_code == 403

    # Unauthorized approve
    app_res = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/approve?organizer_id={unauthorized_id}"
    )
    assert app_res.status_code == 403


def test_authenticated_header_identity_validation(client: TestClient, db_session: Session):
    organizer_id = "header-organizer"
    meeting_id = _create_meeting_setup(db_session, organizer_id=organizer_id)

    # Using valid X-User-Id header matching organizer
    gen_header_res = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/generate",
        headers={"X-User-Id": organizer_id},
    )
    assert gen_header_res.status_code == 200

    # Using invalid X-User-Id header
    gen_invalid_header = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/generate",
        headers={"X-User-Id": "someone-else"},
    )
    assert gen_invalid_header.status_code == 403

    # Conflicting query param and header
    gen_conflict = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/generate?organizer_id={organizer_id}",
        headers={"X-User-Id": "someone-else"},
    )
    assert gen_conflict.status_code == 403


def test_nonexistent_meeting_returns_404(client: TestClient):
    fake_id = str(uuid.uuid4())
    res = client.post(f"/api/v1/meetings/{fake_id}/minutes/generate?organizer_id=demo-organizer")
    assert res.status_code == 404


def test_update_meeting_minutes_as_organizer(client: TestClient, db_session: Session):
    organizer_id = "test-org-edit"
    meeting_id = _create_meeting_setup(db_session, organizer_id=organizer_id)

    # First generate minutes
    client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/generate",
        headers={"X-User-Id": organizer_id},
    )

    # Edit minutes content
    update_payload = {
        "content_summary": "Updated executive summary for sprint planning.",
        "content_decisions": ["Deploy v2 on Monday", "Hold sprint retro on Friday"],
        "content_action_items": [
            {
                "task_description": "Review PRs",
                "owner_name": "Marcus",
                "deadline_text": "by EOD",
                "priority": "high",
            }
        ],
    }

    res = client.patch(
        f"/api/v1/meetings/{meeting_id}/minutes",
        headers={"X-User-Id": organizer_id},
        json=update_payload,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["content_summary"] == "Updated executive summary for sprint planning."
    assert data["content_decisions"] == ["Deploy v2 on Monday", "Hold sprint retro on Friday"]
    assert len(data["content_action_items"]) == 1
    assert data["content_action_items"][0]["task_description"] == "Review PRs"


def test_refresh_meeting_minutes_endpoint(client: TestClient, db_session: Session):
    organizer_id = "test-org-refresh"
    meeting_id = _create_meeting_setup(db_session, organizer_id=organizer_id)

    # Call refresh directly
    res = client.post(
        f"/api/v1/meetings/{meeting_id}/minutes/refresh",
        headers={"X-User-Id": organizer_id},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "pending_approval"
    assert data["content_summary"] is not None
