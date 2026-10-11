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
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_EXTRACTION_STATUS_FAILED,
    ACTION_ITEM_EXTRACTION_STATUS_PENDING,
    Base,
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
                owner_name="Maya",
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
    assert item["source_start_seconds"] == 12.5
    assert item["source_end_seconds"] == 16.75
    assert item["source_quote_start_seconds"] == 13.25
    assert item["source_quote_end_seconds"] == 16.5


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
