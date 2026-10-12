from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import json
import sys
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.meeting.action_item_service import generate_meeting_action_items
from app.meeting.employee_matching import (
    AMBIGUITY_MARGIN,
    MATCH_CONFIDENCE_THRESHOLD,
    match_employee_name,
)
from app.meeting.models import (
    Base,
    Employee,
    Meeting,
    MeetingActionItem,
    MeetingRecording,
    MeetingTranscript,
    RECORDING_STATUS_COMPLETED,
    TRANSCRIPT_STATUS_COMPLETED,
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


def _create_employee(
    db_session: Session,
    *,
    name: str,
    email: str,
    is_active: bool = True,
) -> Employee:
    employee = Employee(
        id=str(uuid.uuid4()),
        name=name,
        email=email,
        is_active=is_active,
    )
    db_session.add(employee)
    db_session.commit()
    db_session.refresh(employee)
    return employee


def test_exact_match_returns_unique_employee_id(db_session: Session):
    employee = _create_employee(db_session, name="Ava Patel", email="ava.patel@northstar.example")

    result = match_employee_name(db_session, "Ava Patel")

    assert result.status == "unique_match"
    assert result.employee_id == employee.id
    assert result.employee_name == employee.name
    assert result.confidence is not None and result.confidence >= 0.88


def test_case_insensitive_match_is_unique(db_session: Session):
    employee = _create_employee(db_session, name="Marcus Chen", email="marcus.chen@northstar.example")

    result = match_employee_name(db_session, "marcus chen")

    assert result.status == "unique_match"
    assert result.employee_id == employee.id


def test_name_normalization_collapses_spaces(db_session: Session):
    employee = _create_employee(db_session, name="Priya Sharma", email="priya.sharma@northstar.example")

    result = match_employee_name(db_session, "  priya    sharma   ")

    assert result.status == "unique_match"
    assert result.employee_id == employee.id


def test_unique_fuzzy_match_uses_confidence_threshold(db_session: Session):
    employee = _create_employee(db_session, name="Ava Patel", email="ava.patel@northstar.example")

    result = match_employee_name(db_session, "Ava Patei")

    assert result.status == "unique_match"
    assert result.employee_id == employee.id
    assert result.confidence is not None and result.confidence >= 0.88


def test_ambiguous_fuzzy_match_requires_approval(db_session: Session):
    employee_one = _create_employee(db_session, name="Ava Patel", email="ava.patel@northstar.example")
    employee_two = _create_employee(db_session, name="Ava Patil", email="ava.patil@northstar.example")

    result = match_employee_name(db_session, "Ava Patei")

    assert result.status == "ambiguous_match"
    assert set(result.candidate_employee_ids) == {employee_one.id, employee_two.id}
    assert set(result.candidate_names) == {employee_one.name, employee_two.name}


def test_multiple_confident_matches_are_ambiguous_even_when_scores_differ(
    db_session: Session,
):
    employee_one = _create_employee(
        db_session,
        name="Ava Patel",
        email="ava.patel@northstar.example",
    )
    employee_two = _create_employee(
        db_session,
        name="Ava Patil",
        email="ava.patil@northstar.example",
    )
    second_score = SequenceMatcher(None, "ava patel", "ava patil").ratio()
    assert second_score >= MATCH_CONFIDENCE_THRESHOLD
    assert 1.0 - second_score > AMBIGUITY_MARGIN

    result = match_employee_name(db_session, "Ava Patel")

    assert result.status == "ambiguous_match"
    assert result.employee_id is None
    assert result.employee_name is None
    assert result.confidence == 1.0
    assert set(result.candidate_employee_ids) == {employee_one.id, employee_two.id}
    assert set(result.candidate_names) == {employee_one.name, employee_two.name}


def test_no_match_when_no_employee_is_close_enough(db_session: Session):
    _create_employee(db_session, name="Daniel Brooks", email="daniel.brooks@northstar.example")

    result = match_employee_name(db_session, "Zoe Lambert")

    assert result.status == "no_match"
    assert result.employee_id is None


def test_inactive_employees_are_ignored(db_session: Session):
    employee = _create_employee(
        db_session,
        name="Noah Martin",
        email="noah.martin@northstar.example",
        is_active=False,
    )
    _create_employee(db_session, name="Chloe Nguyen", email="chloe.nguyen@northstar.example")

    result = match_employee_name(db_session, "Noah Martin")

    assert result.status == "no_match"
    assert result.employee_id is None
    assert employee.id not in result.candidate_employee_ids


def test_missing_owner_names_are_distinct_from_no_match(db_session: Session):
    result_none = match_employee_name(db_session, None)
    result_blank = match_employee_name(db_session, "   ")

    assert result_none.status == "missing_owner"
    assert result_blank.status == "missing_owner"
    assert result_none.employee_id is None
    assert result_blank.employee_id is None


def _create_transcript_for_action_item_match(
    db_session: Session,
    *,
    text: str,
    speaker: str,
    owner_name: str | None,
) -> MeetingTranscript:
    now = datetime.now(timezone.utc)
    meeting_id = str(uuid.uuid4())
    recording_id = str(uuid.uuid4())
    meeting = Meeting(
        id=meeting_id,
        organizer_id="match-organizer",
        title="Action Item Matching Test",
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
        transcript_text=text,
        segments=[
            {
                "start": 1.0,
                "end": 4.0,
                "text": text,
                "speaker": speaker,
                "flagged_for_correction": False,
            }
        ],
    )
    db_session.add_all([meeting, recording, transcript])
    db_session.commit()
    return transcript


def _install_groq(monkeypatch, response: str):
    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=response))]
            )

    class FakeGroq:
        def __init__(self, *, api_key):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.meeting.action_item_service.load_dotenv",
        lambda **kwargs: False,
    )
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key")
    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=FakeGroq))


@pytest.mark.parametrize(
    ("owner_name", "email"),
    [
        ("Priya Sharma", "priya.sharma@northstar.example"),
        ("Daniel Brooks", "daniel.brooks@northstar.example"),
    ],
)
def test_generate_meeting_action_items_retains_unique_employee_match(
    db_session: Session,
    monkeypatch,
    owner_name: str,
    email: str,
):
    employee = _create_employee(
        db_session,
        name=owner_name,
        email=email,
    )
    transcript = _create_transcript_for_action_item_match(
        db_session,
        text=f"{owner_name} will send the draft by Friday.",
        speaker="Manager",
        owner_name=owner_name,
    )
    _install_groq(
        monkeypatch,
        json.dumps(
            {
                "action_items": [
                    {
                        "intent_type": "commitment",
                        "owner_name": owner_name,
                        "task_description": "Send the draft.",
                        "deadline_text": "by Friday",
                        "priority": "medium",
                        "confidence": 0.92,
                        "source_segment_index": 0,
                        "source_quote": f"{owner_name} will send the draft by Friday.",
                    }
                ]
            }
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)
    item = extraction.items[0]

    assert item.owner_name == owner_name
    assert item.employee_match_status == "unique_match"
    assert item.proposed_employee_id == employee.id
    assert item.confirmed_employee_id is None
    assert item.ownership_status == "proposed"
    assert item.proposed_employee_name == owner_name
    assert item.employee_match_result.status == "unique_match"
    db_session.refresh(item)
    assert item.employee_match_status == "unique_match"
    assert item.proposed_employee_id == employee.id
    assert item.confirmed_employee_id is None
    assert item.ownership_status == "proposed"


def test_generate_meeting_action_items_requires_approval_for_no_match(
    db_session: Session,
    monkeypatch,
):
    _create_employee(
        db_session,
        name="Daniel Brooks",
        email="daniel.brooks@northstar.example",
    )
    transcript = _create_transcript_for_action_item_match(
        db_session,
        text="Zoe Lambert will send the draft by Friday.",
        speaker="Manager",
        owner_name="Zoe Lambert",
    )
    _install_groq(
        monkeypatch,
        json.dumps(
            {
                "action_items": [
                    {
                        "intent_type": "commitment",
                        "owner_name": "Zoe Lambert",
                        "task_description": "Send the draft.",
                        "deadline_text": "by Friday",
                        "priority": "medium",
                        "confidence": 0.92,
                        "source_segment_index": 0,
                        "source_quote": "Zoe Lambert will send the draft by Friday.",
                    }
                ]
            }
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)
    item = extraction.items[0]
    persisted_item = db_session.get(MeetingActionItem, item.id)

    assert persisted_item is not None
    assert persisted_item.employee_match_status == "no_match"
    assert persisted_item.proposed_employee_id is None
    assert persisted_item.confirmed_employee_id is None
    assert persisted_item.ownership_status == "approval_required"


@pytest.mark.parametrize("owner_name", [None, "   "])
def test_generate_meeting_action_items_keeps_missing_owner_unresolved(
    db_session: Session,
    monkeypatch,
    owner_name: str | None,
):
    transcript = _create_transcript_for_action_item_match(
        db_session,
        text="We will send the draft by Friday.",
        speaker="Manager",
        owner_name=owner_name,
    )
    _install_groq(
        monkeypatch,
        json.dumps(
            {
                "action_items": [
                    {
                        "intent_type": "commitment",
                        "owner_name": owner_name,
                        "task_description": "Send the draft.",
                        "deadline_text": "by Friday",
                        "priority": "medium",
                        "confidence": 0.92,
                        "source_segment_index": 0,
                        "source_quote": "We will send the draft by Friday.",
                    }
                ]
            }
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)
    item = extraction.items[0]
    persisted_item = db_session.get(MeetingActionItem, item.id)

    assert persisted_item is not None
    assert persisted_item.employee_match_status == "missing_owner"
    assert persisted_item.proposed_employee_id is None
    assert persisted_item.confirmed_employee_id is None
    assert persisted_item.ownership_status == "unresolved"


def test_generate_meeting_action_items_marks_ambiguous_owner_matches(db_session: Session, monkeypatch):
    _create_employee(db_session, name="Ava Patel", email="ava.patel@northstar.example")
    _create_employee(db_session, name="Ava Patil", email="ava.patil@northstar.example")
    transcript = _create_transcript_for_action_item_match(
        db_session,
        text="Ava Patei will send the draft by Friday.",
        speaker="Manager",
        owner_name="Ava Patei",
    )
    _install_groq(
        monkeypatch,
        json.dumps(
            {
                "action_items": [
                    {
                        "intent_type": "commitment",
                        "owner_name": "Ava Patei",
                        "task_description": "Send the draft.",
                        "deadline_text": "by Friday",
                        "priority": "medium",
                        "confidence": 0.92,
                        "source_segment_index": 0,
                        "source_quote": "Ava Patei will send the draft by Friday.",
                    }
                ]
            }
        ),
    )

    extraction = generate_meeting_action_items(db_session, transcript.id)
    item = extraction.items[0]
    persisted_item = db_session.get(MeetingActionItem, item.id)

    assert item.owner_name == "Ava Patei"
    assert item.employee_match_status == "ambiguous_match"
    assert item.employee_match_result.status == "ambiguous_match"
    assert len(item.employee_match_result.candidate_names) >= 2
    assert persisted_item is not None
    assert persisted_item.employee_match_status == "ambiguous_match"
    assert persisted_item.ownership_status == "approval_required"
    assert persisted_item.proposed_employee_id is None
    assert persisted_item.confirmed_employee_id is None
