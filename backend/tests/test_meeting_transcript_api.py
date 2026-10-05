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
    MeetingTranscript,
    MeetingTranscriptCorrection,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
)

ORGANIZER_ID = "transcript-api-organizer"


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


def _create_transcript(db_session: Session) -> tuple[Meeting, MeetingTranscript]:
    meeting_id = str(uuid.uuid4())
    recording_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id=ORGANIZER_ID,
        title="Transcript API test",
        start_at=now,
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(
        id=recording_id,
        meeting_id=meeting_id,
        status=RECORDING_STATUS_COMPLETED,
    )
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording_id,
        status=TRANSCRIPT_STATUS_COMPLETED,
        language="en",
        transcript_text="Original words.",
        segments=[
            {
                "start": 0.25,
                "end": 1.5,
                "text": "Original words.",
                "speaker": "Speaker A",
                "flagged_for_correction": True,
            },
            {
                "start": 1.5,
                "end": 2.75,
                "text": "Next phrase.",
                "speaker": "Speaker B",
                "flagged_for_correction": False,
            },
        ],
    )
    db_session.add_all([meeting, recording, transcript])
    db_session.commit()
    return meeting, transcript


def test_get_transcript_returns_complete_segments_and_requires_organizer(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    response = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript?organizer_id={ORGANIZER_ID}"
    )

    assert response.status_code == 200
    assert response.json()["meeting_id"] == meeting.id
    assert response.json()["recording_id"] == transcript.recording_id
    assert response.json()["segments"] == transcript.segments

    unauthorized = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript?organizer_id=someone-else"
    )
    assert unauthorized.status_code == 403


def test_get_transcript_returns_404_for_missing_meeting_or_transcript(
    client: TestClient,
    db_session: Session,
):
    missing_meeting = client.get(
        f"/api/v1/meetings/{uuid.uuid4()}/transcript?organizer_id={ORGANIZER_ID}"
    )
    assert missing_meeting.status_code == 404
    assert missing_meeting.json()["detail"] == "Meeting not found"

    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db_session.add(
        Meeting(
            id=meeting_id,
            organizer_id=ORGANIZER_ID,
            title="Meeting without transcript",
            start_at=now,
            end_at=now + timedelta(hours=1),
            join_url=f"http://localhost/meetings/{meeting_id}",
            status="completed",
        )
    )
    db_session.commit()

    no_transcript = client.get(
        f"/api/v1/meetings/{meeting_id}/transcript?organizer_id={ORGANIZER_ID}"
    )
    assert no_transcript.status_code == 404
    assert no_transcript.json()["detail"] == "Transcript not found"


def test_patch_transcript_records_text_and_speaker_edits_and_rejects_count_mismatch(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    original_created_at = transcript.created_at
    original_completed_at = transcript.completed_at
    original_segments = transcript.segments
    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/transcript?organizer_id={ORGANIZER_ID}",
        json={
            "segments": [
                {"text": "Edited words.", "speaker": "Speaker A"},
                {"text": "Next phrase.", "speaker": "Speaker C"},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["transcript_text"] == "Edited words. Next phrase."
    assert response.json()["segments"] == [
        {
            "start": 0.25,
            "end": 1.5,
            "text": "Edited words.",
            "speaker": "Speaker A",
            "flagged_for_correction": True,
        },
        {
            "start": 1.5,
            "end": 2.75,
            "text": "Next phrase.",
            "speaker": "Speaker C",
            "flagged_for_correction": False,
        },
    ]

    db_session.expire_all()
    saved = db_session.get(MeetingTranscript, transcript.id)
    assert saved.segments == response.json()["segments"]
    corrections = (
        db_session.query(MeetingTranscriptCorrection)
        .filter_by(transcript_id=transcript.id)
        .order_by(MeetingTranscriptCorrection.segment_index)
        .all()
    )
    assert len(corrections) == 2
    assert corrections[0].segment_index == 0
    assert corrections[0].original_text == "Original words."
    assert corrections[0].corrected_text == "Edited words."
    assert corrections[0].original_speaker == "Speaker A"
    assert corrections[0].corrected_speaker == "Speaker A"
    assert corrections[0].editor_id == ORGANIZER_ID
    assert corrections[1].segment_index == 1
    assert corrections[1].original_text == "Next phrase."
    assert corrections[1].corrected_text == "Next phrase."
    assert corrections[1].original_speaker == "Speaker B"
    assert corrections[1].corrected_speaker == "Speaker C"
    assert corrections[0].created_at is not None
    assert saved.created_at == original_created_at
    assert saved.completed_at == original_completed_at
    assert [
        {
            "start": segment["start"],
            "end": segment["end"],
            "flagged_for_correction": segment["flagged_for_correction"],
        }
        for segment in saved.segments
    ] == [
        {
            "start": segment["start"],
            "end": segment["end"],
            "flagged_for_correction": segment["flagged_for_correction"],
        }
        for segment in original_segments
    ]

    history = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript/corrections"
        f"?organizer_id={ORGANIZER_ID}"
    )
    assert history.status_code == 200
    assert len(history.json()) == 2
    assert history.json()[0]["segment_index"] == 0
    assert history.json()[0]["original_text"] == "Original words."
    assert history.json()[0]["corrected_text"] == "Edited words."
    assert history.json()[0]["original_speaker"] == "Speaker A"
    assert history.json()[0]["corrected_speaker"] == "Speaker A"
    assert history.json()[0]["editor_id"] == ORGANIZER_ID
    assert history.json()[0]["created_at"] is not None
    assert history.json()[1]["editor_id"] == ORGANIZER_ID
    assert history.json()[1]["segment_index"] == 1
    assert history.json()[1]["original_speaker"] == "Speaker B"
    assert history.json()[1]["corrected_speaker"] == "Speaker C"


def test_patch_records_original_and_corrected_text_and_speaker(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/transcript?organizer_id={ORGANIZER_ID}",
        json={
            "segments": [
                {"text": "Corrected words.", "speaker": "Speaker C"},
                {"text": "Next phrase."},
            ]
        },
    )

    assert response.status_code == 200
    correction = (
        db_session.query(MeetingTranscriptCorrection)
        .filter_by(transcript_id=transcript.id)
        .one()
    )
    assert correction.segment_index == 0
    assert correction.original_text == "Original words."
    assert correction.corrected_text == "Corrected words."
    assert correction.original_speaker == "Speaker A"
    assert correction.corrected_speaker == "Speaker C"
    assert correction.editor_id == ORGANIZER_ID

    unauthorized_history = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript/corrections"
        "?organizer_id=someone-else"
    )
    assert unauthorized_history.status_code == 403

    second_edit = client.patch(
        f"/api/v1/meetings/{meeting.id}/transcript?organizer_id={ORGANIZER_ID}",
        json={
            "segments": [
                {"text": "Revised words.", "speaker": "Speaker C"},
                {"text": "Next phrase."},
            ]
        },
    )
    assert second_edit.status_code == 200
    corrections = (
        db_session.query(MeetingTranscriptCorrection)
        .filter_by(transcript_id=transcript.id)
        .all()
    )
    assert len(corrections) == 2
    assert {
        (entry.original_text, entry.corrected_text)
        for entry in corrections
    } == {
        ("Original words.", "Corrected words."),
        ("Corrected words.", "Revised words."),
    }

    mismatch = client.patch(
        f"/api/v1/meetings/{meeting.id}/transcript?organizer_id={ORGANIZER_ID}",
        json={"segments": [{"text": "Only one segment."}]},
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"] == (
        "Segment count must match the existing transcript"
    )
    assert (
        db_session.get(MeetingTranscript, transcript.id).segments
        == second_edit.json()["segments"]
    )


def test_unchanged_segments_do_not_create_correction_records(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    original_segments = transcript.segments
    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/transcript?organizer_id={ORGANIZER_ID}",
        json={
            "segments": [
                {"text": "Original words."},
                {"text": "Next phrase."},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["segments"] == original_segments
    assert (
        db_session.query(MeetingTranscriptCorrection)
        .filter_by(transcript_id=transcript.id)
        .count()
        == 0
    )


def test_patch_rejects_client_controlled_segment_fields(
    client: TestClient,
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    response = client.patch(
        f"/api/v1/meetings/{meeting.id}/transcript?organizer_id={ORGANIZER_ID}",
        json={
            "segments": [
                {
                    "text": "Edited.",
                    "start": 999,
                    "end": 1000,
                    "speaker": "Speaker A",
                    "flagged_for_correction": False,
                },
                {"text": "Next phrase."},
            ]
        },
    )

    assert response.status_code == 422
    db_session.expire_all()
    assert db_session.get(MeetingTranscript, transcript.id).segments == [
        {
            "start": 0.25,
            "end": 1.5,
            "text": "Original words.",
            "speaker": "Speaker A",
            "flagged_for_correction": True,
        },
        {
            "start": 1.5,
            "end": 2.75,
            "text": "Next phrase.",
            "speaker": "Speaker B",
            "flagged_for_correction": False,
        },
    ]


def test_patch_transcript_enforces_organizer_and_missing_transcript(
    client: TestClient,
    db_session: Session,
):
    meeting, _ = _create_transcript(db_session)
    unauthorized = client.patch(
        f"/api/v1/meetings/{meeting.id}/transcript?organizer_id=someone-else",
        json={"segments": [{"text": "Edited."}, {"text": "Text."}]},
    )
    assert unauthorized.status_code == 403

    missing = client.patch(
        f"/api/v1/meetings/{uuid.uuid4()}/transcript?organizer_id={ORGANIZER_ID}",
        json={"segments": []},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Meeting not found"

    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db_session.add(
        Meeting(
            id=meeting_id,
            organizer_id=ORGANIZER_ID,
            title="Meeting without transcript",
            start_at=now,
            end_at=now + timedelta(hours=1),
            join_url=f"http://localhost/meetings/{meeting_id}",
            status="completed",
        )
    )
    db_session.commit()

    no_transcript = client.patch(
        f"/api/v1/meetings/{meeting_id}/transcript?organizer_id={ORGANIZER_ID}",
        json={"segments": []},
    )
    assert no_transcript.status_code == 404
    assert no_transcript.json()["detail"] == "Transcript not found"

    unauthorized_history = client.get(
        f"/api/v1/meetings/{meeting.id}/transcript/corrections"
        "?organizer_id=someone-else"
    )
    assert unauthorized_history.status_code == 403
