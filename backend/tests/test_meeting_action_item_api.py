from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.meeting.action_item_service import MeetingActionItemExtractionError
from app.db.database import get_db
from app.db.seed_employees import seed_development_employees
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_EXTRACTION_STATUS_FAILED,
    ACTION_ITEM_EXTRACTION_STATUS_PENDING,
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    SUMMARY_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_PENDING,
)

ORGANIZER_ID = "action-item-api-organizer"


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


def _create_meeting_and_transcript(
    db_session: Session,
    *,
    with_transcript: bool = True,
) -> tuple[Meeting, MeetingTranscript | None]:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id=ORGANIZER_ID,
        title="Action Item API Test",
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
    transcript = (
        MeetingTranscript(
            id=str(uuid.uuid4()),
            recording_id=recording.id,
            status=TRANSCRIPT_STATUS_COMPLETED,
            language="en",
            transcript_text="Maya, please send the agenda by Friday.",
            segments=[
                {
                    "start": 12.5,
                    "end": 16.75,
                    "text": "Maya, please send the agenda by Friday.",
                    "speaker": "Organizer",
                    "flagged_for_correction": False,
                }
            ],
            completed_at=now,
        )
        if with_transcript
        else None
    )
    db_session.add_all([meeting, recording])
    if transcript is not None:
        db_session.add(transcript)
    db_session.commit()
    return meeting, transcript


def _create_extraction(
    db_session: Session,
    meeting: Meeting,
    transcript: MeetingTranscript,
    *,
    status: str = ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    with_item: bool = True,
    owner_name: str | None = "Maya",
    employee_match_status: str | None = None,
    ownership_status: str = "unresolved",
) -> MeetingActionItemExtraction:
    extraction = MeetingActionItemExtraction(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        transcript_id=transcript.id,
        status=status,
        completed_at=datetime.now(timezone.utc)
        if status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
        else None,
        error_message="Extraction failed."
        if status == ACTION_ITEM_EXTRACTION_STATUS_FAILED
        else None,
    )
    if with_item:
        extraction.items.append(
            MeetingActionItem(
                id=str(uuid.uuid4()),
                owner_name=owner_name,
                employee_match_status=employee_match_status,
                ownership_status=ownership_status,
                task_description="Send the agenda.",
                deadline_text="by Friday",
                priority="high",
                confidence=0.96,
                source_segment_index=0,
                source_start_seconds=12.5,
                source_end_seconds=16.75,
                source_quote_start_seconds=13.25,
                source_quote_end_seconds=16.5,
                source_quote="Maya, please send the agenda by Friday.",
            )
        )
    db_session.add(extraction)
    db_session.commit()
    db_session.refresh(extraction)
    return extraction


def test_post_extracts_action_items_and_returns_source_fields(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)

    def fake_generate(db: Session, transcript_id: str):
        assert transcript_id == transcript.id
        return _create_extraction(db, meeting, transcript)

    monkeypatch.setattr(
        "app.meeting.router.generate_meeting_action_items",
        fake_generate,
    )
    response = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/extract"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert body["meeting_id"] == meeting.id
    assert body["transcript_id"] == transcript.id
    assert len(body["action_items"]) == 1
    item = body["action_items"][0]
    assert item == {
        "id": item["id"],
        "owner_name": "Maya",
        "proposed_employee_id": None,
        "confirmed_employee_id": None,
        "employee_match_status": None,
        "ownership_status": "unresolved",
        "review_status": "pending_review",
        "reviewed_by": None,
        "reviewed_at": None,
        "is_task_eligible": False,
        "requires_approval": False,
        "task_description": "Send the agenda.",
        "deadline_at": None,
        "deadline_text": "by Friday",
        "priority": "high",
        "confidence": 0.96,
        "meeting_id": meeting.id,
        "transcript_id": transcript.id,
        "source_segment_index": 0,
        "source_start_seconds": 12.5,
        "source_end_seconds": 16.75,
        "source_quote_start_seconds": 13.25,
        "source_quote_end_seconds": 16.5,
        "source_quote": "Maya, please send the agenda by Friday.",
    }


def test_get_returns_persisted_action_items_and_source_timestamps(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript)

    response = client.get(
        f"/api/v1/meetings/{meeting.id}/action-items"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == extraction.id
    assert body["status"] == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    item = body["action_items"][0]
    assert item["meeting_id"] == meeting.id
    assert item["transcript_id"] == transcript.id
    assert item["employee_match_status"] == "no_match"
    assert item["ownership_status"] == "approval_required"
    assert item["proposed_employee_id"] is None
    assert item["confirmed_employee_id"] is None
    assert item["source_start_seconds"] == 12.5
    assert item["source_end_seconds"] == 16.75
    assert item["source_quote_start_seconds"] == 13.25
    assert item["source_quote_end_seconds"] == 16.5


@pytest.mark.parametrize(
    ("owner_name", "email"),
    [
        ("Priya Sharma", "priya.sharma@northstar.example"),
        ("Daniel Brooks", "daniel.brooks@northstar.example"),
    ],
)
def test_get_action_items_backfills_and_returns_legacy_match_metadata(
    client: TestClient,
    db_session: Session,
    owner_name: str,
    email: str,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    assert transcript is not None
    extraction = _create_extraction(
        db_session,
        meeting,
        transcript,
        owner_name=owner_name,
    )
    item = extraction.items[0]
    employee = Employee(
        id=str(uuid.uuid4()),
        name=owner_name,
        email=email,
        is_active=True,
    )
    db_session.add(employee)
    db_session.commit()

    response = client.get(
        f"/api/v1/meetings/{meeting.id}/action-items"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 200
    response_item = response.json()["action_items"][0]
    assert response_item["owner_name"] == owner_name
    assert response_item["employee_match_status"] == "unique_match"
    assert response_item["ownership_status"] == "proposed"
    assert response_item["proposed_employee_id"] == employee.id
    assert response_item["confirmed_employee_id"] is None
    db_session.refresh(item)
    assert item.employee_match_status == "unique_match"
    assert item.ownership_status == "proposed"
    assert item.proposed_employee_id == employee.id
    assert item.confirmed_employee_id is None


def test_get_action_items_backfill_preserves_confirmed_owner_separate_from_match(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    assert transcript is not None
    extraction = _create_extraction(
        db_session,
        meeting,
        transcript,
        owner_name="Priya Sharma",
        ownership_status="confirmed",
    )
    item = extraction.items[0]
    selected_employee = Employee(
        id=str(uuid.uuid4()),
        name="Daniel Brooks",
        email="daniel.brooks@northstar.example",
        is_active=True,
    )
    matched_employee = Employee(
        id=str(uuid.uuid4()),
        name="Priya Sharma",
        email="priya.sharma@northstar.example",
        is_active=True,
    )
    db_session.add_all([selected_employee, matched_employee])
    item.proposed_employee_id = selected_employee.id
    item.confirmed_employee_id = selected_employee.id
    db_session.commit()

    response = client.get(
        f"/api/v1/meetings/{meeting.id}/action-items"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 200
    response_item = response.json()["action_items"][0]
    assert response_item["owner_name"] == "Priya Sharma"
    assert response_item["employee_match_status"] == "unique_match"
    assert response_item["proposed_employee_id"] == matched_employee.id
    assert response_item["confirmed_employee_id"] == selected_employee.id
    assert response_item["ownership_status"] == "confirmed"
    db_session.refresh(item)
    assert item.employee_match_status == "unique_match"
    assert item.proposed_employee_id == matched_employee.id
    assert item.confirmed_employee_id == selected_employee.id
    assert item.ownership_status == "confirmed"


def test_get_active_employees_returns_only_active_employee_fields(
    client: TestClient,
    db_session: Session,
):
    db_session.add(Employee(
        id="test-employee",
        name="Test Employee",
        email="test-employee@test.invalid",
        department="Operations",
        is_active=True,
    ))
    active_employee = Employee(
        id=str(uuid.uuid4()),
        name="Ava Patel",
        email="ava.patel@northstar.example",
        department="Operations",
        is_active=True,
    )
    inactive_employee = Employee(
        id=str(uuid.uuid4()),
        name="Marcus Chen",
        email="marcus.chen@northstar.example",
        department="Operations",
        is_active=False,
    )
    db_session.add_all([active_employee, inactive_employee])
    db_session.commit()

    response = client.get("/api/v1/meetings/employees")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": active_employee.id,
            "name": active_employee.name,
            "email": active_employee.email,
            "team": active_employee.team,
            "department": "Operations",
        },
        {
            "id": "test-employee",
            "name": "Test Employee",
            "email": "test-employee@test.invalid",
            "team": None,
            "department": "Operations",
        }
    ]


def test_development_seed_makes_rahul_available_from_employee_api(
    client: TestClient,
    db_session: Session,
):
    db_session.add(Employee(
        id="test-employee",
        name="Test Employee",
        email="test-employee@test.invalid",
        department="Operations",
        is_active=True,
    ))
    seed_development_employees(db_session)
    rahul = db_session.query(Employee).filter_by(name="Rahul").one()
    rahul.department = "Operations"
    db_session.commit()

    response = client.get("/api/v1/meetings/employees")

    assert response.status_code == 200
    rahul = next(
        employee
        for employee in response.json()
        if employee["email"] == "rahulexample.test@gmail.com"
    )
    assert rahul["name"] == "Rahul"


@pytest.mark.parametrize(
    "match_status",
    ["unique_match", "ambiguous_match", "no_match", "missing_owner"],
)
def test_confirm_action_item_owner_preserves_matching_result_and_original_name(
    client: TestClient,
    db_session: Session,
    match_status: str,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    assert transcript is not None
    extraction = _create_extraction(
        db_session,
        meeting,
        transcript,
        owner_name="Original transcript owner",
        employee_match_status=match_status,
        ownership_status="approval_required",
    )
    item = extraction.items[0]
    item.review_status = "confirmed"
    item.reviewed_by = ORGANIZER_ID
    item.reviewed_at = datetime.now(timezone.utc)
    proposed_employee = None
    if match_status == "unique_match":
        proposed_employee = Employee(
            id=str(uuid.uuid4()),
            name="Original Proposed Employee",
            email="proposed@northstar.example",
            is_active=True,
        )
        item.proposed_employee_id = proposed_employee.id
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Ava Patel",
        email="ava.patel@northstar.example",
        is_active=True,
    )
    db_session.add_all(
        [employee, *([proposed_employee] if proposed_employee is not None else [])]
    )
    db_session.commit()

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/confirm-owner"
        f"?organizer_id={ORGANIZER_ID}",
        json={"employee_id": employee.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["owner_name"] == "Original transcript owner"
    assert body["employee_match_status"] == match_status
    assert body["proposed_employee_id"] == (
        proposed_employee.id if proposed_employee is not None else None
    )
    assert body["confirmed_employee_id"] == employee.id
    assert body["ownership_status"] == "confirmed"
    assert body["review_status"] == "confirmed"
    db_session.refresh(item)
    assert item.owner_name == "Original transcript owner"
    assert item.employee_match_status == match_status
    assert item.proposed_employee_id == (
        proposed_employee.id if proposed_employee is not None else None
    )
    assert item.confirmed_employee_id == employee.id
    assert item.ownership_status == "confirmed"
    assert item.review_status == "confirmed"


def test_confirm_action_item_owner_keeps_missing_owner_name_missing(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    assert transcript is not None
    extraction = _create_extraction(
        db_session,
        meeting,
        transcript,
        owner_name=None,
        employee_match_status="missing_owner",
        ownership_status="unresolved",
    )
    item = extraction.items[0]
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Ava Patel",
        email="ava.patel@northstar.example",
        is_active=True,
    )
    db_session.add(employee)
    db_session.commit()

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/confirm-owner"
        f"?organizer_id={ORGANIZER_ID}",
        json={"employee_id": employee.id},
    )

    assert response.status_code == 200
    assert response.json()["owner_name"] is None
    assert response.json()["employee_match_status"] == "missing_owner"
    assert response.json()["proposed_employee_id"] is None
    assert response.json()["confirmed_employee_id"] == employee.id
    assert response.json()["ownership_status"] == "confirmed"
    db_session.refresh(item)
    assert item.owner_name is None


def test_confirm_action_item_owner_rejects_unknown_employee(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    assert transcript is not None
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/confirm-owner"
        f"?organizer_id={ORGANIZER_ID}",
        json={"employee_id": str(uuid.uuid4())},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Employee not found"
    db_session.refresh(item)
    assert item.confirmed_employee_id is None
    assert item.ownership_status == "unresolved"


def test_confirm_action_item_owner_rejects_inactive_employee(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    assert transcript is not None
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Inactive Employee",
        email="inactive@northstar.example",
        is_active=False,
    )
    db_session.add(employee)
    db_session.commit()

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/confirm-owner"
        f"?organizer_id={ORGANIZER_ID}",
        json={"employee_id": employee.id},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Employee is inactive"
    db_session.refresh(item)
    assert item.confirmed_employee_id is None
    assert item.ownership_status == "unresolved"


def test_confirm_action_item_owner_rejects_item_from_another_meeting(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    other_meeting, other_transcript = _create_meeting_and_transcript(db_session)
    assert transcript is not None
    assert other_transcript is not None
    extraction = _create_extraction(db_session, other_meeting, other_transcript)
    item = extraction.items[0]
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Ava Patel",
        email="ava.patel@northstar.example",
        is_active=True,
    )
    db_session.add(employee)
    db_session.commit()

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/confirm-owner"
        f"?organizer_id={ORGANIZER_ID}",
        json={"employee_id": employee.id},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Action item not found for this meeting"
    db_session.refresh(item)
    assert item.confirmed_employee_id is None
    assert item.ownership_status == "unresolved"


def test_confirm_action_item_owner_rejects_incorrect_organizer(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    assert transcript is not None
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Ava Patel",
        email="ava.patel@northstar.example",
        is_active=True,
    )
    db_session.add(employee)
    db_session.commit()

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/confirm-owner"
        "?organizer_id=someone-else",
        json={"employee_id": employee.id},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Only the meeting organizer can confirm ownership"
    db_session.refresh(item)
    assert item.confirmed_employee_id is None
    assert item.ownership_status == "unresolved"


def test_transcript_api_selects_the_requested_transcript_for_the_meeting(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=RECORDING_STATUS_COMPLETED,
    )
    other_transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status=TRANSCRIPT_STATUS_COMPLETED,
        language="en",
        transcript_text="Different transcript.",
        segments=[
            {
                "start": 21.25,
                "end": 23.0,
                "text": "Different transcript.",
                "speaker": "Speaker B",
                "flagged_for_correction": False,
            }
        ],
        created_at=transcript.created_at + timedelta(seconds=1),
    )
    db_session.add_all([recording, other_transcript])
    db_session.commit()

    response = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript"
        f"?organizer_id={ORGANIZER_ID}&transcript_id={transcript.id}"
    )

    assert response.status_code == 200
    assert response.json()["id"] == transcript.id
    assert response.json()["segments"][0]["start"] == 12.5

    patched = client.patch(
        f"/api/v1/meetings/{meeting.id}/transcript"
        f"?organizer_id={ORGANIZER_ID}&transcript_id={transcript.id}",
        json={"segments": [{"text": "Corrected source transcript."}]},
    )
    assert patched.status_code == 200
    assert patched.json()["id"] == transcript.id
    assert patched.json()["transcript_text"] == "Corrected source transcript."
    db_session.refresh(other_transcript)
    assert other_transcript.transcript_text == "Different transcript."

    corrections = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript/corrections"
        f"?organizer_id={ORGANIZER_ID}&transcript_id={transcript.id}"
    )
    assert corrections.status_code == 200
    assert all(item["transcript_id"] == transcript.id for item in corrections.json())


def test_zero_item_extraction_returns_completed_empty_list(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(
        db_session,
        meeting,
        transcript,
        with_item=False,
    )
    monkeypatch.setattr(
        "app.meeting.router.generate_meeting_action_items",
        lambda db, transcript_id: extraction,
    )

    triggered = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/extract"
        f"?organizer_id={ORGANIZER_ID}"
    )
    retrieved = client.get(
        f"/api/v1/meetings/{meeting.id}/action-items"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert triggered.status_code == 200
    assert triggered.json()["status"] == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert triggered.json()["action_items"] == []
    assert retrieved.status_code == 200
    assert retrieved.json()["action_items"] == []


def test_missing_meeting_returns_404_for_both_endpoints(client: TestClient):
    meeting_id = str(uuid.uuid4())
    trigger = client.post(
        f"/api/v1/meetings/{meeting_id}/action-items/extract"
        f"?organizer_id={ORGANIZER_ID}"
    )
    retrieve = client.get(
        f"/api/v1/meetings/{meeting_id}/action-items"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert trigger.status_code == 404
    assert trigger.json()["detail"] == "Meeting not found"
    assert retrieve.status_code == 404
    assert retrieve.json()["detail"] == "Meeting not found"


def test_missing_completed_transcript_returns_404_for_both_endpoints(
    client: TestClient,
    db_session: Session,
):
    meeting, _ = _create_meeting_and_transcript(db_session, with_transcript=False)

    trigger = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/extract"
        f"?organizer_id={ORGANIZER_ID}"
    )
    retrieve = client.get(
        f"/api/v1/meetings/{meeting.id}/action-items"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert trigger.status_code == 404
    assert trigger.json()["detail"] == "Completed transcript not found"
    assert retrieve.status_code == 404
    assert retrieve.json()["detail"] == "Completed transcript not found"


def test_pending_transcript_does_not_start_or_fail_action_item_extraction(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    assert transcript is not None
    transcript.status = TRANSCRIPT_STATUS_PENDING
    db_session.commit()

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/extract"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Completed transcript not found"
    assert db_session.query(MeetingActionItemExtraction).filter_by(
        transcript_id=transcript.id,
    ).count() == 0


@pytest.mark.parametrize("method", ["get", "post"])
def test_meeting_organizer_is_required(
    client: TestClient,
    db_session: Session,
    method: str,
):
    meeting, _transcript = _create_meeting_and_transcript(db_session)
    endpoint = f"/api/v1/meetings/{meeting.id}/action-items"
    if method == "post":
        endpoint += "/extract"
    response = getattr(client, method)(
        f"{endpoint}?organizer_id=someone-else"
    )

    assert response.status_code == 403
    assert "organizer" in response.json()["detail"].lower()


def test_extraction_failure_returns_safe_error_and_preserves_summary(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    summary = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        transcript_id=transcript.id,
        status=SUMMARY_STATUS_COMPLETED,
        overview="Existing summary.",
        decisions=[],
        discussion_topics=[],
        open_questions=[],
    )
    db_session.add(summary)
    db_session.commit()
    failed_extraction = _create_extraction(
        db_session,
        meeting,
        transcript,
        status=ACTION_ITEM_EXTRACTION_STATUS_FAILED,
        with_item=False,
    )
    monkeypatch.setattr(
        "app.meeting.router.generate_meeting_action_items",
        lambda db, transcript_id: (_ for _ in ()).throw(
            MeetingActionItemExtractionError("provider secret")
        ),
    )

    response = client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/extract"
        f"?organizer_id={ORGANIZER_ID}"
    )

    db_session.refresh(transcript)
    db_session.refresh(summary)
    stored_failure = db_session.get(MeetingActionItemExtraction, failed_extraction.id)
    assert response.status_code == 500
    assert response.json()["detail"] == "Action item extraction failed"
    assert "provider secret" not in response.text
    assert transcript.status == TRANSCRIPT_STATUS_COMPLETED
    assert summary.overview == "Existing summary."
    assert stored_failure.status == ACTION_ITEM_EXTRACTION_STATUS_FAILED


def test_repeated_trigger_reuses_extraction_without_duplicate_records(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript)
    calls = []

    def fake_generate(db: Session, transcript_id: str):
        calls.append(transcript_id)
        return db.query(MeetingActionItemExtraction).filter_by(
            transcript_id=transcript_id
        ).one()

    monkeypatch.setattr(
        "app.meeting.router.generate_meeting_action_items",
        fake_generate,
    )
    for _ in range(2):
        response = client.post(
            f"/api/v1/meetings/{meeting.id}/action-items/extract"
            f"?organizer_id={ORGANIZER_ID}"
        )
        assert response.status_code == 200
        assert response.json()["id"] == extraction.id

    assert calls == [transcript.id, transcript.id]
    assert (
        db_session.query(MeetingActionItemExtraction)
        .filter_by(transcript_id=transcript.id)
        .count()
        == 1
    )
    assert (
        db_session.query(MeetingActionItem)
        .filter_by(extraction_id=extraction.id)
        .count()
        == 1
    )


def test_retrieving_missing_extraction_returns_404(
    client: TestClient,
    db_session: Session,
):
    meeting, _transcript = _create_meeting_and_transcript(db_session)

    response = client.get(
        f"/api/v1/meetings/{meeting.id}/action-items"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Meeting action items not found"


def test_pending_extraction_status_is_returned(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(
        db_session,
        meeting,
        transcript,
        status=ACTION_ITEM_EXTRACTION_STATUS_PENDING,
        with_item=False,
    )

    response = client.get(
        f"/api/v1/meetings/{meeting.id}/action-items"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 200
    assert response.json()["id"] == extraction.id
    assert response.json()["status"] == ACTION_ITEM_EXTRACTION_STATUS_PENDING
    assert response.json()["action_items"] == []


# ---------------------------------------------------------------------------
# Story 3.3 — Action Item Review (FR-14) Tests
# ---------------------------------------------------------------------------


def test_review_action_item_confirm(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Maya",
        email="maya@example.test",
        is_active=True,
    )
    item.confirmed_employee_id = employee.id
    item.deadline_at = datetime.now(timezone.utc) + timedelta(days=3)
    db_session.add(employee)
    db_session.commit()
    monkeypatch.setattr("app.task_service.send_email", lambda *_args: None)

    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/review"
        f"?organizer_id={ORGANIZER_ID}",
        json={"action": "confirm"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == item.id
    assert data["review_status"] == "confirmed"
    assert data["reviewed_by"] == ORGANIZER_ID
    assert data["reviewed_at"] is not None
    assert data["is_task_eligible"] is True

    # Check persistence in database
    db_session.expire_all()
    persisted = db_session.get(MeetingActionItem, item.id)
    assert persisted is not None
    assert persisted.review_status == "confirmed"
    assert persisted.reviewed_by == ORGANIZER_ID
    assert persisted.reviewed_at is not None


def test_review_action_item_reject(client: TestClient, db_session: Session):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]

    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/review"
        f"?organizer_id={ORGANIZER_ID}",
        json={"action": "reject"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["review_status"] == "rejected"
    assert data["reviewed_by"] == ORGANIZER_ID
    assert data["reviewed_at"] is not None
    assert data["is_task_eligible"] is False

    # Story 3.4 boundary safeguard: rejected item cannot become a task
    from app.meeting.action_item_service import (
        assert_action_item_eligible_for_task,
        get_task_eligible_action_items,
    )

    db_session.expire_all()
    persisted = db_session.get(MeetingActionItem, item.id)
    assert persisted.review_status == "rejected"
    with pytest.raises(ValueError, match="rejected and cannot be converted"):
        assert_action_item_eligible_for_task(persisted)

    eligible_items = get_task_eligible_action_items(db_session, meeting.id)
    assert persisted not in eligible_items


def test_rejected_action_item_cannot_be_confirmed_or_reassigned(
    client: TestClient, db_session: Session
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]
    employee = Employee(
        id=str(uuid.uuid4()),
        name="Ava Patel",
        email="ava.patel@northstar.example",
        is_active=True,
    )
    db_session.add(employee)
    db_session.commit()
    base = f"/api/v1/meetings/{meeting.id}/action-items/{item.id}"

    assert client.patch(
        f"{base}/review?organizer_id={ORGANIZER_ID}", json={"action": "reject"}
    ).status_code == 200

    confirm = client.patch(
        f"{base}/review?organizer_id={ORGANIZER_ID}", json={"action": "confirm"}
    )
    assert confirm.status_code == 400
    assert confirm.json()["detail"] == "Rejected action items cannot be changed."

    reassign = client.post(
        f"{base}/confirm-owner?organizer_id={ORGANIZER_ID}",
        json={"employee_id": employee.id},
    )
    assert reassign.status_code == 400

    db_session.expire_all()
    persisted = db_session.get(MeetingActionItem, item.id)
    assert persisted.review_status == "rejected"
    assert persisted.confirmed_employee_id is None


def test_review_action_item_edit_fields_preserves_source_metadata(
    client: TestClient, db_session: Session
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]

    original_segment_index = item.source_segment_index
    original_start_seconds = item.source_start_seconds
    original_quote = item.source_quote

    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/review"
        f"?organizer_id={ORGANIZER_ID}",
        json={
            "action": "edit",
            "task_description": "Updated task description by organizer.",
            "owner_name": "New Assignee",
            "priority": "urgent",
            "deadline_text": "by Monday",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["task_description"] == "Updated task description by organizer."
    assert data["owner_name"] == "New Assignee"
    assert data["priority"] == "urgent"
    assert data["deadline_text"] == "by Monday"
    assert data["reviewed_by"] == ORGANIZER_ID
    assert data["reviewed_at"] is not None

    # Preserves source information
    assert data["source_segment_index"] == original_segment_index
    assert data["source_start_seconds"] == original_start_seconds
    assert data["source_quote"] == original_quote


def test_review_action_item_unauthorized_organizer(
    client: TestClient, db_session: Session
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]

    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/review"
        f"?organizer_id=unauthorized-user",
        json={"action": "confirm"},
    )

    assert response.status_code == 403
    assert "Only the meeting organizer" in response.json()["detail"]


def test_review_action_item_not_found(client: TestClient, db_session: Session):
    meeting, _ = _create_meeting_and_transcript(db_session)

    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/action-items/nonexistent-item/review"
        f"?organizer_id={ORGANIZER_ID}",
        json={"action": "confirm"},
    )

    assert response.status_code == 404


def test_review_action_item_empty_task_description_rejected(
    client: TestClient, db_session: Session
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]

    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/action-items/{item.id}/review"
        f"?organizer_id={ORGANIZER_ID}",
        json={"task_description": "   "},
    )

    assert response.status_code == 400
    assert "cannot be empty" in response.json()["detail"]


def test_confidence_threshold_requires_approval(
    client: TestClient, db_session: Session, monkeypatch
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript)
    item = extraction.items[0]
    # Set item confidence below 0.85 default threshold
    item.confidence = 0.72
    item.review_status = "approval_required"
    db_session.commit()

    response = client.get(
        f"/api/v1/meetings/{meeting.id}/action-items"
        f"?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 200
    items = response.json()["action_items"]
    assert len(items) == 1
    assert items[0]["review_status"] == "approval_required"
    assert items[0]["requires_approval"] is True
    assert items[0]["is_task_eligible"] is False


def test_retry_empty_query_is_passed_to_extraction(
    client: TestClient,
    db_session: Session,
    monkeypatch,
):
    meeting, transcript = _create_meeting_and_transcript(db_session)
    extraction = _create_extraction(db_session, meeting, transcript, with_item=False)
    calls = []

    def fake_generate(db, transcript_id, **kwargs):
        calls.append(kwargs)
        return extraction

    monkeypatch.setattr("app.meeting.router.generate_meeting_action_items", fake_generate)

    client.post(f"/api/v1/meetings/{meeting.id}/action-items/extract?organizer_id={ORGANIZER_ID}")
    client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/extract"
        f"?organizer_id={ORGANIZER_ID}&retry_empty=true"
    )

    assert calls == [{}, {"retry_empty": True}]
