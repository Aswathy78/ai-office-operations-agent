from datetime import datetime, timedelta, timezone
import json
import sys
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.meeting.action_item_service import (
    MeetingActionItemExtractionError,
    generate_meeting_action_items,
)
from app.meeting.models import (
    ACTION_ITEM_EXTRACTION_STATUS_COMPLETED,
    ACTION_ITEM_EXTRACTION_STATUS_FAILED,
    Base,
    Meeting,
    MeetingActionItem,
    MeetingActionItemExtraction,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_PENDING,
)


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


def _create_completed_transcript(
    db_session: Session,
    segments: list[dict[str, object]] | None = None,
    status: str = TRANSCRIPT_STATUS_COMPLETED,
) -> MeetingTranscript:
    meeting_id = str(uuid.uuid4())
    recording_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    meeting = Meeting(
        id=meeting_id,
        organizer_id="action-item-organizer",
        title="Action Item Service Test",
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
        status=status,
        language="en",
        transcript_text=" ".join(str(segment["text"]) for segment in segments or []),
        segments=segments
        if segments is not None
        else [
            {
                "start": 4.25,
                "end": 7.5,
                "text": "I will send the draft by Friday.",
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            }
        ],
    )
    db_session.add_all([meeting, recording, transcript])
    db_session.commit()
    return transcript


def _candidate(
    *,
    source_segment_index: int = 0,
    source_quote: str = "I will send the draft by Friday.",
    task_description: str = "Send the draft.",
    owner_name: str | None = "Alex",
    deadline_at: str | None = None,
    deadline_text: str | None = "by Friday",
    priority: str = "medium",
    confidence: float = 0.9,
) -> dict[str, object]:
    return {
        "intent_type": "commitment",
        "owner_name": owner_name,
        "task_description": task_description,
        "deadline_at": deadline_at,
        "deadline_text": deadline_text,
        "priority": priority,
        "confidence": confidence,
        "source_segment_index": source_segment_index,
        "source_quote": source_quote,
    }


def _install_groq(
    monkeypatch,
    response: str | None = None,
    error: Exception | None = None,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if error is not None:
                raise error
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=response),
                    )
                ]
            )

    class FakeGroq:
        def __init__(self, *, api_key):
            calls.append({"api_key": api_key})
            self.chat = SimpleNamespace(
                completions=FakeCompletions(),
            )

    monkeypatch.setattr(
        "app.meeting.action_item_service.load_dotenv",
        lambda **kwargs: False,
    )
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key")
    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=FakeGroq))
    return calls


def _response(items: list[dict[str, object]]) -> str:
    return json.dumps({"action_items": items})


def test_extracts_explicit_assignment_and_persists_source_timestamps(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(db_session)
    configured_model = "configured-action-item-model"
    monkeypatch.setenv("GROQ_ACTION_ITEM_MODEL", configured_model)
    monkeypatch.setenv("ORGANIZATION_TIMEZONE", "Asia/Kolkata")
    transcript.segments = [
        {**transcript.segments[0], "speaker": "Alex"},
    ]
    db_session.commit()
    calls = _install_groq(monkeypatch, _response([_candidate()]))

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert extraction.meeting_id == db_session.get(
        MeetingRecording,
        transcript.recording_id,
    ).meeting_id
    assert extraction.transcript_id == transcript.id
    assert len(extraction.items) == 1
    item = extraction.items[0]
    assert item.owner_name == "Alex"
    assert item.task_description == "Send the draft."
    assert item.deadline_at is not None

    recording = db_session.get(MeetingRecording, transcript.recording_id)
    meeting = db_session.get(Meeting, recording.meeting_id)

    meeting_start = meeting.start_at
    if meeting_start.tzinfo is None:
        meeting_start = meeting_start.replace(tzinfo=timezone.utc)

    meeting_date = meeting_start.astimezone(
            ZoneInfo("Asia/Kolkata")

    ).date()

    days_until_friday = (4 - meeting_date.weekday()) % 7
    expected_friday = meeting_date + timedelta(days=days_until_friday)

    assert item.deadline_at.date() == expected_friday


    assert item.deadline_text == "by Friday"
    assert item.priority == "medium"
    assert item.confidence == 0.9
    assert item.source_segment_index == 0
    assert item.source_start_seconds == 4.25
    assert item.source_end_seconds == 7.5
    assert item.source_quote == "I will send the draft by Friday."
    assert calls[1]["model"] == configured_model
    sent_segments = json.loads(calls[1]["messages"][1]["content"])
    assert sent_segments == [
        {
            "index": 0,
            "start": 4.25,
            "end": 7.5,
            "speaker": "Alex",
            "text": "I will send the draft by Friday.",
        }
    ]


def test_extracts_multiple_action_items_from_distinct_segments(
    db_session: Session,
    monkeypatch,
):
    segments = [
        {
            "start": 0.5,
            "end": 2.0,
            "text": "Maya, please send the agenda.",
            "speaker": "Manager",
            "flagged_for_correction": False,
        },
        {
            "start": 20.68,
            "end": 24.84,
            "text": "I will book the room tomorrow.",
            "speaker": "Maya",
            "flagged_for_correction": False,
        },
    ]
    transcript = _create_completed_transcript(db_session, segments)
    candidates = [
        _candidate(
            source_segment_index=0,
            source_quote="Maya, please send the agenda.",
            task_description="Send the agenda.",
            owner_name="Maya",
            deadline_text=None,
        ),
        _candidate(
            source_segment_index=1,
            source_quote="I will book the room tomorrow.",
            task_description="Book the room.",
            owner_name="Maya",
            deadline_text="tomorrow",
        ),
    ]
    _install_groq(monkeypatch, _response(candidates))

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert [item.source_segment_index for item in extraction.items] == [0, 1]
    assert [
        (item.source_start_seconds, item.source_end_seconds)
        for item in extraction.items
    ] == [(0.5, 2.0), (20.68, 24.84)]


def test_extracts_commitments_split_across_adjacent_segments(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(
        db_session,
        [
            {
                "start": 0.0,
                "end": 19.0,
                "text": (
                    "Good morning, Alex will complete the implementation of the "
                    "user story by 12 October, Rahul will"
                ),
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            },
            {
                "start": 19.0,
                "end": 27.0,
                "text": (
                    "test the IPI in points and fix existing bugs by 13 October."
                ),
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            },
        ],
    )
    _install_groq(
        monkeypatch,
        _response(
            [
                _candidate(
                    source_segment_index=0,
                    source_quote=(
                        "Alex will complete the implementation of the user story "
                        "by 12 October"
                    ),
                    task_description="Complete the implementation of the user story.",
                    owner_name="Alex",
                    deadline_text="by 12 October",
                ),
                _candidate(
                    source_segment_index=1,
                    source_quote=(
                        "Rahul will test the IPI in points and fix existing bugs "
                        "by 13 October."
                    ),
                    task_description="Test the IPI in points and fix existing bugs.",
                    owner_name="Rahul",
                    deadline_text="by 13 October",
                ),
            ]
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)

    items_by_owner = {item.owner_name: item for item in extraction.items}
    assert set(items_by_owner) == {"Alex", "Rahul"}
    alex_item = items_by_owner["Alex"]
    assert alex_item.source_segment_index == 0
    assert alex_item.source_start_seconds == 0.0
    assert alex_item.deadline_text == "by 12 October"
    rahul_item = items_by_owner["Rahul"]
    assert rahul_item.task_description == "Test the IPI in points and fix existing bugs."
    assert rahul_item.deadline_text == "by 13 October"
    assert rahul_item.source_segment_index == 1
    assert rahul_item.source_start_seconds == 19.0
    assert rahul_item.source_quote_start_seconds is None


def test_extracts_actions_split_at_the_end_of_the_source_segment(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(
        db_session,
        [
            {
                "start": 0.0,
                "end": 23.76,
                "text": (
                    "Good morning. Rahul should complete the meeting workflow by "
                    "Thursday. Alex should do the review"
                ),
                "speaker": "Alex",
                "flagged_for_correction": False,
            },
            {
                "start": 23.76,
                "end": 26.96,
                "text": "of the meeting by Saturday.",
                "speaker": "Alex",
                "flagged_for_correction": False,
            },
        ],
    )
    _install_groq(
        monkeypatch,
        _response(
            [
                _candidate(
                    source_segment_index=0,
                    source_quote=(
                        "Rahul should complete the meeting workflow by Thursday"
                    ),
                    task_description="Complete the meeting workflow.",
                    owner_name="Rahul",
                    deadline_text="by Thursday",
                ),
                _candidate(
                    source_segment_index=0,
                    source_quote=(
                        "Alex should do the review of the meeting by Saturday"
                    ),
                    task_description="Review the meeting.",
                    owner_name="Alex",
                    deadline_text="by Saturday",
                ),
            ]
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)

    items_by_owner = {item.owner_name: item for item in extraction.items}
    assert set(items_by_owner) == {"Rahul", "Alex"}
    assert items_by_owner["Rahul"].source_segment_index == 0
    assert items_by_owner["Rahul"].source_start_seconds == 0.0
    assert items_by_owner["Rahul"].deadline_text == "by Thursday"
    assert items_by_owner["Alex"].source_segment_index == 0
    assert items_by_owner["Alex"].source_start_seconds == 0.0
    assert items_by_owner["Alex"].deadline_text == "by Saturday"
    assert items_by_owner["Alex"].source_quote_start_seconds is None


def test_unsupported_groq_quote_does_not_discard_verified_candidates(
    db_session: Session,
    monkeypatch,
    caplog,
):
    transcript = _create_completed_transcript(
        db_session,
        [
            {
                "start": 0.0,
                "end": 23.76,
                "text": (
                    "Good morning. Rahul should complete the meeting workflow by "
                    "Thursday. Alex should do the review"
                ),
                "speaker": "Alex",
                "flagged_for_correction": False,
            },
            {
                "start": 23.76,
                "end": 26.96,
                "text": "of the meeting by Saturday.",
                "speaker": "Alex",
                "flagged_for_correction": False,
            },
        ],
    )
    _install_groq(
        monkeypatch,
        _response(
            [
                _candidate(
                    source_segment_index=0,
                    source_quote="Rahul should complete the meeting workflow by Thursday.",
                    task_description="Complete the meeting workflow.",
                    owner_name="Rahul",
                    deadline_text="by Thursday",
                ),
                _candidate(
                    source_segment_index=0,
                    source_quote="Alex should complete the review by Saturday.",
                    task_description="Review the meeting.",
                    owner_name="Alex",
                    deadline_text="by Saturday",
                ),
            ]
        ),
    )

    with caplog.at_level("WARNING", logger="app.meeting.action_item_service"):
        extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert len(extraction.items) == 1
    assert extraction.items[0].owner_name == "Rahul"
    assert extraction.items[0].source_segment_index == 0
    assert "candidate_index=1 source_segment_index=0" in caplog.text
    assert "Alex should complete the review" not in caplog.text


def test_non_actionable_statement_split_across_segments_is_discarded(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(
        db_session,
        [
            {
                "start": 1.0,
                "end": 2.0,
                "text": "The product will",
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            },
            {
                "start": 2.0,
                "end": 4.0,
                "text": "be available in stores starting Monday.",
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            },
        ],
    )
    _install_groq(
        monkeypatch,
        _response(
            [
                _candidate(
                    source_segment_index=1,
                    source_quote=(
                        "The product will be available in stores starting Monday."
                    ),
                    task_description="Make the product available in stores.",
                    owner_name=None,
                    deadline_text="starting Monday",
                )
            ]
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert extraction.items == []


def test_extracts_distinct_quote_timestamps_from_one_whisper_segment(
    db_session: Session,
    monkeypatch,
):
    segment = {
        "start": 0.0,
        "end": 16.0,
        "text": "Alex will prepare the project report by Friday. Rahul will review the report.",
        "speaker": "Speaker A",
        "flagged_for_correction": False,
        "words": [
            {"start": 0.5, "end": 0.8, "word": " Alex"},
            {"start": 0.8, "end": 1.1, "word": " will"},
            {"start": 1.1, "end": 1.5, "word": " prepare"},
            {"start": 1.5, "end": 1.7, "word": " the"},
            {"start": 1.7, "end": 2.1, "word": " project"},
            {"start": 2.1, "end": 2.5, "word": " report"},
            {"start": 2.5, "end": 2.7, "word": " by"},
            {"start": 2.7, "end": 3.2, "word": " Friday."},
            {"start": 8.2, "end": 8.5, "word": " Rahul"},
            {"start": 8.5, "end": 8.8, "word": " will"},
            {"start": 8.8, "end": 9.2, "word": " review"},
            {"start": 9.2, "end": 9.4, "word": " the"},
            {"start": 9.4, "end": 9.8, "word": " report."},
        ],
    }
    transcript = _create_completed_transcript(db_session, [segment])
    candidates = [
        _candidate(
            source_quote="Alex will prepare the project report by Friday.",
            task_description="Prepare the project report.",
            owner_name="Alex",
            deadline_text="by Friday",
        ),
        _candidate(
            source_quote="Rahul will review the report.",
            task_description="Review the report.",
            owner_name="Rahul",
            deadline_text=None,
        ),
    ]
    _install_groq(monkeypatch, _response(candidates))

    extraction = generate_meeting_action_items(db_session, transcript.id)

    by_task = {item.task_description: item for item in extraction.items}
    assert by_task["Prepare the project report."].source_segment_index == 0
    assert by_task["Prepare the project report."].source_start_seconds == 0.0
    assert by_task["Prepare the project report."].source_quote_start_seconds == 0.5
    assert by_task["Prepare the project report."].source_quote_end_seconds == 3.2
    assert by_task["Review the report."].source_segment_index == 0
    assert by_task["Review the report."].source_start_seconds == 0.0
    assert by_task["Review the report."].source_quote_start_seconds == 8.2
    assert by_task["Review the report."].source_quote_end_seconds == 9.8


def test_quote_timestamp_is_unavailable_when_word_alignment_is_missing(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(db_session)
    _install_groq(monkeypatch, _response([_candidate()]))

    extraction = generate_meeting_action_items(db_session, transcript.id)

    item = extraction.items[0]
    assert item.source_start_seconds == 4.25
    assert item.source_quote_start_seconds is None
    assert item.source_quote_end_seconds is None


def test_zero_quote_timestamp_is_kept_when_the_action_starts_at_zero(
    db_session: Session,
    monkeypatch,
):
    segment = {
        "start": 0.0,
        "end": 4.0,
        "text": "I will send the draft.",
        "speaker": "Alex",
        "flagged_for_correction": False,
        "words": [
            {"start": 0.0, "end": 0.2, "word": " I"},
            {"start": 0.2, "end": 0.4, "word": " will"},
            {"start": 0.4, "end": 0.8, "word": " send"},
            {"start": 0.8, "end": 0.95, "word": " the"},
            {"start": 0.95, "end": 1.3, "word": " draft."},
        ],
    }
    transcript = _create_completed_transcript(db_session, [segment])
    candidate = _candidate(
        source_quote="I will send the draft.",
        owner_name="Alex",
        deadline_text=None,
    )
    _install_groq(monkeypatch, _response([candidate]))

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.items[0].source_quote_start_seconds == 0.0
    assert extraction.items[0].source_quote_end_seconds == 1.3


def test_non_actionable_general_discussion_completes_with_no_items(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(
        db_session,
        [
            {
                "start": 1.0,
                "end": 4.0,
                "text": "The office is usually busy on Fridays.",
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            }
        ],
    )
    calls = _install_groq(monkeypatch, _response([]))

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert extraction.items == []
    assert db_session.query(MeetingActionItem).count() == 0
    assert len(calls) == 2


def test_candidate_from_general_discussion_is_discarded(
    db_session: Session,
    monkeypatch,
):
    statement = "The office is usually busy on Fridays."
    transcript = _create_completed_transcript(
        db_session,
        [
            {
                "start": 1.0,
                "end": 4.0,
                "text": statement,
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            }
        ],
    )
    _install_groq(
        monkeypatch,
        _response(
            [
                _candidate(
                    source_quote=statement,
                    task_description="Make the office less busy.",
                )
            ]
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert extraction.items == []


def test_candidate_from_hypothetical_commitment_is_discarded(
    db_session: Session,
    monkeypatch,
):
    statement = "If the proposal is approved, Maya will send the final document."
    transcript = _create_completed_transcript(
        db_session,
        [
            {
                "start": 1.0,
                "end": 4.0,
                "text": statement,
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            }
        ],
    )
    _install_groq(
        monkeypatch,
        _response(
            [
                _candidate(
                    source_quote=statement,
                    task_description="Send the final document.",
                    owner_name="Maya",
                    deadline_text=None,
                )
            ]
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert extraction.items == []


def test_hypothetical_should_statement_remains_non_actionable(
    db_session: Session,
    monkeypatch,
):
    statement = "If approved, Alex should send the final document."
    transcript = _create_completed_transcript(
        db_session,
        [
            {
                "start": 1.0,
                "end": 4.0,
                "text": statement,
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            }
        ],
    )
    _install_groq(
        monkeypatch,
        _response(
            [
                _candidate(
                    source_quote=statement,
                    task_description="Send the final document.",
                    owner_name="Alex",
                    deadline_text=None,
                )
            ]
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert extraction.items == []


def test_candidate_from_declarative_product_statement_is_discarded(
    db_session: Session,
    monkeypatch,
):
    statement = "The product will be available in stores starting Monday."
    transcript = _create_completed_transcript(
        db_session,
        [
            {
                "start": 1.0,
                "end": 4.0,
                "text": statement,
                "speaker": "Speaker A",
                "flagged_for_correction": False,
            }
        ],
    )
    _install_groq(
        monkeypatch,
        _response([
            _candidate(
                source_quote=statement,
                task_description="Make the product available in stores.",
                owner_name=None,
                deadline_text="starting Monday",
            )
        ]),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert extraction.items == []


def test_missing_owner_and_deadline_remain_null(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(db_session)
    candidate = _candidate(
        owner_name=None,
        deadline_at=None,
        deadline_text=None,
    )
    _install_groq(monkeypatch, _response([candidate]))

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.items[0].owner_name is None
    assert extraction.items[0].deadline_at is None
    assert extraction.items[0].deadline_text is None


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (
            _candidate(source_segment_index=9),
            "missing transcript segment",
        ),
        (
            _candidate(source_quote="A quote from another meeting."),
            "verifiable source transcript quotes",
        ),
        (
            _candidate(priority="critical"),
            "malformed action-item data",
        ),
        (
            _candidate(confidence=1.1),
            "malformed action-item data",
        ),
        (
            _candidate(confidence=-0.1),
            "malformed action-item data",
        ),
    ],
)
def test_invalid_candidate_is_rejected_and_failure_persisted(
    db_session: Session,
    monkeypatch,
    candidate: dict[str, object],
    message: str,
):
    transcript = _create_completed_transcript(db_session)
    _install_groq(monkeypatch, _response([candidate]))

    with pytest.raises(MeetingActionItemExtractionError):
        generate_meeting_action_items(db_session, transcript.id)

    extraction = (
        db_session.query(MeetingActionItemExtraction)
        .filter_by(transcript_id=transcript.id)
        .one()
    )
    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_FAILED
    assert message in extraction.error_message.lower()
    assert extraction.completed_at is not None
    assert extraction.items == []


@pytest.mark.parametrize(
    "response",
    [
        "{not json",
        json.dumps({"unexpected": []}),
        json.dumps({"action_items": [{"task_description": "missing required fields"}]}),
    ],
)
def test_malformed_groq_response_persists_failed_extraction(
    db_session: Session,
    monkeypatch,
    response: str,
):
    transcript = _create_completed_transcript(db_session)
    _install_groq(monkeypatch, response)

    with pytest.raises(MeetingActionItemExtractionError):
        generate_meeting_action_items(db_session, transcript.id)

    extraction = (
        db_session.query(MeetingActionItemExtraction)
        .filter_by(transcript_id=transcript.id)
        .one()
    )
    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_FAILED
    assert extraction.error_message == "Groq returned malformed action-item data."
    assert extraction.items == []


def test_groq_failure_persists_safe_error_without_exposing_api_key(
    db_session: Session,
    monkeypatch,
    caplog,
):
    transcript = _create_completed_transcript(db_session)
    _install_groq(
        monkeypatch,
        error=RuntimeError("Request failed with unit-test-key"),
    )

    with caplog.at_level("ERROR", logger="app.meeting.action_item_service"):
        with pytest.raises(MeetingActionItemExtractionError) as raised:
            generate_meeting_action_items(db_session, transcript.id)

    extraction = (
        db_session.query(MeetingActionItemExtraction)
        .filter_by(transcript_id=transcript.id)
        .one()
    )
    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_FAILED
    assert extraction.error_message == "Action item extraction failed."
    assert "unit-test-key" not in extraction.error_message
    assert "unit-test-key" not in str(raised.value)
    assert "exception_type=RuntimeError" in caplog.text
    assert "provider_status=None" in caplog.text
    assert "unit-test-key" not in caplog.text


def test_groq_http_failure_logs_status_without_provider_error_text(
    db_session: Session,
    monkeypatch,
    caplog,
):
    transcript = _create_completed_transcript(db_session)

    class FakeProviderError(Exception):
        status_code = 401

    _install_groq(
        monkeypatch,
        error=FakeProviderError("Invalid API credential detail"),
    )

    with caplog.at_level("ERROR", logger="app.meeting.action_item_service"):
        with pytest.raises(MeetingActionItemExtractionError):
            generate_meeting_action_items(db_session, transcript.id)

    assert "exception_type=FakeProviderError" in caplog.text
    assert "provider_status=401" in caplog.text
    assert "Invalid API credential detail" not in caplog.text


def test_completed_extraction_is_reused_without_duplicates_or_groq_call(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(db_session)
    calls = _install_groq(monkeypatch, _response([_candidate()]))
    first = generate_meeting_action_items(db_session, transcript.id)
    second = generate_meeting_action_items(db_session, transcript.id)

    assert second.id == first.id
    assert len(calls) == 2
    assert (
        db_session.query(MeetingActionItemExtraction)
        .filter_by(transcript_id=transcript.id)
        .count()
        == 1
    )
    assert (
        db_session.query(MeetingActionItem)
        .filter_by(extraction_id=first.id)
        .count()
        == 1
    )


def test_completed_extraction_refreshes_timing_in_place_after_word_alignment_arrives(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(db_session)
    calls = _install_groq(monkeypatch, _response([_candidate()]))
    extraction = generate_meeting_action_items(db_session, transcript.id)
    original_item_id = extraction.items[0].id
    assert extraction.items[0].source_quote_start_seconds is None

    transcript.segments = [
        {
            **transcript.segments[0],
            "words": [
                {"start": 4.5, "end": 4.7, "word": " I"},
                {"start": 4.7, "end": 4.9, "word": " will"},
                {"start": 4.9, "end": 5.2, "word": " send"},
                {"start": 5.2, "end": 5.35, "word": " the"},
                {"start": 5.35, "end": 5.7, "word": " draft"},
                {"start": 5.7, "end": 5.85, "word": " by"},
                {"start": 5.85, "end": 6.2, "word": " Friday."},
            ],
        }
    ]
    db_session.commit()

    refreshed = generate_meeting_action_items(db_session, transcript.id)

    assert refreshed.id == extraction.id
    assert refreshed.items[0].id == original_item_id
    assert refreshed.items[0].source_quote_start_seconds == 4.5
    assert refreshed.items[0].source_quote_end_seconds == 6.2
    assert len(calls) == 2
    assert db_session.query(MeetingActionItemExtraction).count() == 1
    assert db_session.query(MeetingActionItem).count() == 1


def test_failed_extraction_retry_reuses_record_and_replaces_items_atomically(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(db_session)
    _install_groq(
        monkeypatch,
        error=RuntimeError("temporary provider failure"),
    )
    with pytest.raises(MeetingActionItemExtractionError):
        generate_meeting_action_items(db_session, transcript.id)
    failed = (
        db_session.query(MeetingActionItemExtraction)
        .filter_by(transcript_id=transcript.id)
        .one()
    )

    _install_groq(monkeypatch, _response([_candidate(), _candidate()]))
    retried = generate_meeting_action_items(db_session, transcript.id)

    assert retried.id == failed.id
    assert retried.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert len(retried.items) == 1
    assert (
        db_session.query(MeetingActionItemExtraction)
        .filter_by(transcript_id=transcript.id)
        .count()
        == 1
    )
    assert (
        db_session.query(MeetingActionItem)
        .filter_by(extraction_id=failed.id)
        .count()
        == 1
    )


def test_non_completed_transcript_is_rejected_without_creating_extraction(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(
        db_session,
        status=TRANSCRIPT_STATUS_PENDING,
    )
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key")

    with pytest.raises(ValueError, match="completed meeting transcript"):
        generate_meeting_action_items(db_session, transcript.id)

    assert db_session.query(MeetingActionItemExtraction).count() == 0


def test_empty_timestamped_segment_list_completes_with_zero_items(
    db_session: Session,
    monkeypatch,
):
    transcript = _create_completed_transcript(db_session, segments=[])
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    extraction = generate_meeting_action_items(db_session, transcript.id)

    assert extraction.status == ACTION_ITEM_EXTRACTION_STATUS_COMPLETED
    assert extraction.items == []
