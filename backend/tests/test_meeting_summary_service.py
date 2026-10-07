from datetime import datetime, timedelta, timezone
import sys
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.meeting.models import (
    Base,
    Meeting,
    MeetingRecording,
    MeetingSummary,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    SUMMARY_STATUS_COMPLETED,
    SUMMARY_STATUS_FAILED,
    SUMMARY_STATUS_PROCESSING,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_PENDING,
)
from app.meeting.summary_service import (
    MeetingSummaryGenerationError,
    SummarySections,
    generate_meeting_summary,
)

TRANSCRIPT_TEXT = "The team decided to launch the pilot in May."


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


def _create_transcript(
    db_session: Session,
    *,
    status: str = TRANSCRIPT_STATUS_COMPLETED,
) -> MeetingTranscript:
    meeting_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="summary-organizer",
        title="Summary Service Test",
        start_at=now,
        end_at=now + timedelta(hours=1),
        join_url=f"http://localhost/meetings/{meeting_id}",
        status="completed",
    )
    recording = MeetingRecording(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        status=RECORDING_STATUS_COMPLETED,
    )
    transcript = MeetingTranscript(
        id=str(uuid.uuid4()),
        recording_id=recording.id,
        status=status,
        language="en",
        transcript_text=TRANSCRIPT_TEXT,
    )
    db_session.add_all([meeting, recording, transcript])
    db_session.commit()
    return transcript


def _fake_openai(monkeypatch, output: SummarySections, calls: list):
    class FakeResponses:
        def parse(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_parsed=output)

    class FakeOpenAI:
        def __init__(self, *, api_key):
            calls.append({"api_key": api_key})
            self.responses = FakeResponses()

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=FakeOpenAI),
    )


def _sections() -> SummarySections:
    return SummarySections(
        overview="The team agreed to run a pilot in May.",
        decisions=["Run the pilot in May."],
        discussion_topics=["Pilot schedule"],
        open_questions=[],
    )


def _disable_dotenv(monkeypatch):
    monkeypatch.setattr(
        "app.meeting.summary_service.load_dotenv",
        lambda **kwargs: False,
    )


def test_completed_transcript_text_is_sent_and_structured_summary_persisted(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_transcript(db_session)
    calls = []
    _disable_dotenv(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _fake_openai(monkeypatch, _sections(), calls)

    summary = generate_meeting_summary(db_session, transcript.id)

    assert calls[0] == {"api_key": "test-key"}
    assert calls[1]["input"][1]["content"] == TRANSCRIPT_TEXT
    assert calls[1]["text_format"] is SummarySections
    assert "Do not invent" in calls[1]["input"][0]["content"]
    assert summary.status == SUMMARY_STATUS_COMPLETED
    assert summary.overview == "The team agreed to run a pilot in May."
    assert summary.decisions == ["Run the pilot in May."]
    assert summary.discussion_topics == ["Pilot schedule"]
    assert summary.open_questions == []
    assert summary.completed_at is not None


def test_non_completed_transcript_is_rejected_before_openai_call(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_transcript(db_session, status=TRANSCRIPT_STATUS_PENDING)
    calls = []
    _disable_dotenv(monkeypatch)
    _fake_openai(monkeypatch, _sections(), calls)

    with pytest.raises(ValueError, match="completed meeting transcript"):
        generate_meeting_summary(db_session, transcript.id)

    assert calls == []
    assert db_session.query(MeetingSummary).count() == 0


def test_completed_summary_is_reused_without_openai_call(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_transcript(db_session)
    existing = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=db_session.get(MeetingRecording, transcript.recording_id).meeting_id,
        transcript_id=transcript.id,
        status=SUMMARY_STATUS_COMPLETED,
        overview="Persisted result",
        decisions=[],
        discussion_topics=[],
        open_questions=[],
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(existing)
    db_session.commit()

    def fail_if_called(**kwargs):
        pytest.fail("OpenAI must not be called for a completed summary.")

    monkeypatch.setattr(
        "app.meeting.summary_service.load_dotenv",
        lambda **kwargs: pytest.fail("Completed summary should return before loading .env."),
    )

    class FakeResponses:
        parse = staticmethod(fail_if_called)

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=lambda **kwargs: SimpleNamespace(responses=FakeResponses())),
    )

    reused = generate_meeting_summary(db_session, transcript.id)

    assert reused.id == existing.id
    assert reused.overview == "Persisted result"


def test_openai_failure_is_persisted_as_failed_summary(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_transcript(db_session)
    _disable_dotenv(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeResponses:
        def parse(self, **kwargs):
            raise RuntimeError("Responses API unavailable")

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(
            OpenAI=lambda **kwargs: SimpleNamespace(responses=FakeResponses())
        ),
    )

    with pytest.raises(MeetingSummaryGenerationError, match="Responses API unavailable"):
        generate_meeting_summary(db_session, transcript.id)

    failed = (
        db_session.query(MeetingSummary)
        .filter(MeetingSummary.transcript_id == transcript.id)
        .one()
    )
    assert failed.status == SUMMARY_STATUS_FAILED
    assert failed.error_message == "Responses API unavailable"
    assert failed.completed_at is not None


def test_missing_api_key_fails_clearly_and_persists_failure(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_transcript(db_session)
    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(MeetingSummaryGenerationError, match="OPENAI_API_KEY"):
        generate_meeting_summary(db_session, transcript.id)

    failed = (
        db_session.query(MeetingSummary)
        .filter(MeetingSummary.transcript_id == transcript.id)
        .one()
    )
    assert failed.status == SUMMARY_STATUS_FAILED
    assert failed.error_message == (
        "OPENAI_API_KEY is not set in the environment or backend/.env."
    )
    assert failed.completed_at is not None


def test_existing_processing_summary_is_returned_without_second_call(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_transcript(db_session)
    recording = db_session.get(MeetingRecording, transcript.recording_id)
    processing = MeetingSummary(
        id=str(uuid.uuid4()),
        meeting_id=recording.meeting_id,
        transcript_id=transcript.id,
        status=SUMMARY_STATUS_PROCESSING,
    )
    db_session.add(processing)
    db_session.commit()
    monkeypatch.setattr(
        "app.meeting.summary_service.load_dotenv",
        lambda **kwargs: pytest.fail("Processing summary must not be generated twice."),
    )

    returned = generate_meeting_summary(db_session, transcript.id)

    assert returned.id == processing.id
    assert returned.status == SUMMARY_STATUS_PROCESSING
