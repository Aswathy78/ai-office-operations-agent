from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.meeting.models import (
    Base,
    Meeting,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    SUMMARY_STATUSES,
    TRANSCRIPT_STATUS_COMPLETED,
)
from app.meeting.schemas import MeetingSummaryResponse


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _create_transcript(
    db_session: Session,
) -> tuple[Meeting, MeetingTranscript]:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="summary-organizer",
        title="Summary Model Test",
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
        transcript_text="Persisted meeting transcript.",
    )
    db_session.add_all([meeting, recording, transcript])
    db_session.flush()
    return meeting, transcript


def test_create_tables_includes_meeting_summaries(db_session: Session):
    assert "meeting_summaries" in set(
        inspect(db_session.get_bind()).get_table_names()
    )

    summary_columns = {
        column["name"]: column
        for column in inspect(db_session.get_bind()).get_columns("meeting_summaries")
    }
    assert summary_columns["meeting_id"]["nullable"] is False
    assert summary_columns["transcript_id"]["nullable"] is False
    foreign_keys = inspect(db_session.get_bind()).get_foreign_keys(
        "meeting_summaries"
    )
    assert {
        (foreign_key["constrained_columns"][0], foreign_key["referred_table"])
        for foreign_key in foreign_keys
    } == {("meeting_id", "meetings"), ("transcript_id", "meeting_transcripts")}


def test_summary_references_required_meeting_and_transcript(
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    summary = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        transcript_id=transcript.id,
    )
    db_session.add(summary)
    db_session.commit()
    db_session.expire_all()

    saved = db_session.get(MeetingSummary, summary.id)
    assert saved is not None
    assert saved.meeting.id == meeting.id
    assert saved.transcript.id == transcript.id
    assert saved.status == "pending"


def test_summary_accepts_defined_statuses_and_rejects_unknown_status(
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    summaries = []
    for index, status in enumerate(SUMMARY_STATUSES):
        if index == 0:
            source_transcript = transcript
        else:
            recording = MeetingRecording(
                id=str(uuid.uuid4()),
                meeting_id=meeting.id,
                status=RECORDING_STATUS_COMPLETED,
            )
            source_transcript = MeetingTranscript(
                id=str(uuid.uuid4()),
                recording_id=recording.id,
                status=TRANSCRIPT_STATUS_COMPLETED,
            )
            db_session.add_all([recording, source_transcript])
        summary = MeetingSummary(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            transcript_id=source_transcript.id,
            status=status,
        )
        summaries.append(summary)
        db_session.add(summary)
    db_session.commit()

    assert {
        summary.status
        for summary in db_session.query(MeetingSummary).all()
    } == set(SUMMARY_STATUSES)

    invalid_recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=RECORDING_STATUS_COMPLETED,
    )
    extra_transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=invalid_recording.id,
        status=TRANSCRIPT_STATUS_COMPLETED,
    )
    db_session.add_all(
        [
            invalid_recording,
            extra_transcript,
            MeetingSummary(
                id=str(uuid.uuid4()),
                meeting_id=meeting.id,
                transcript_id=extra_transcript.id,
                status="unknown",
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_summary_persists_all_sections_and_serializes_response(
    db_session: Session,
):
    meeting, transcript = _create_transcript(db_session)
    completed_at = datetime.now(timezone.utc)
    summary = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        transcript_id=transcript.id,
        status="completed",
        overview="The team agreed on the rollout plan.",
        decisions=["Launch the pilot next month."],
        discussion_topics=["Timeline", "Staffing"],
        open_questions=["Who will own customer training?"],
        completed_at=completed_at,
    )
    db_session.add(summary)
    db_session.commit()
    db_session.expire_all()

    saved = db_session.get(MeetingSummary, summary.id)
    assert saved is not None
    response = MeetingSummaryResponse.model_validate(saved)
    assert response.overview == "The team agreed on the rollout plan."
    assert response.decisions == ["Launch the pilot next month."]
    assert response.discussion_topics == ["Timeline", "Staffing"]
    assert response.open_questions == ["Who will own customer training?"]
    assert response.status == "completed"
    assert response.completed_at is not None
    assert response.error_message is None
    assert saved.created_at is not None
    assert saved.updated_at is not None


def test_failed_summary_persists_error_message(db_session: Session):
    meeting, transcript = _create_transcript(db_session)
    summary = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        transcript_id=transcript.id,
        status="failed",
        error_message="Summary generation failed.",
    )
    db_session.add(summary)
    db_session.commit()
    db_session.expire_all()

    saved = db_session.get(MeetingSummary, summary.id)
    assert saved is not None
    assert saved.status == "failed"
    assert saved.error_message == "Summary generation failed."
    assert MeetingSummaryResponse.model_validate(saved).error_message == (
        "Summary generation failed."
    )


def test_only_one_summary_is_allowed_per_transcript(db_session: Session):
    meeting, transcript = _create_transcript(db_session)
    db_session.add(
        MeetingSummary(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            transcript_id=transcript.id,
        )
    )
    db_session.commit()
    db_session.add(
        MeetingSummary(
            id=str(uuid.uuid4()),
            meeting_id=meeting.id,
            transcript_id=transcript.id,
            status="failed",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
